//! Core dataplane engine.
//!
//! The engine owns userland UDP service sockets and applies the Gatherlink
//! frame boundary before emitting virtual UDP payloads. Business policy stays in
//! Python; this module executes already-compiled runtime state.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::net::SocketAddr;
use std::time::{Duration, Instant};

use gatherlink_crypto::errors::CryptoError;
use gatherlink_protocol::control::SequenceObservation;
use gatherlink_protocol::errors::ProtocolError;
use gatherlink_protocol::frame::{Frame, FrameKind, V1_HEADER_LEN};
use gatherlink_protocol::ids::{is_reserved_service_id, ServiceId};

use crate::dedupe::{DedupeObservation, DedupeWindow};
use crate::fragmentation::{fragment_datagram, FragmentReassembly};
use crate::metrics::{DataTrafficTimingSample, DataplaneMetrics, MetricsSnapshot};
use crate::runtime_config::{CorePathConfig, CoreRuntimeConfig, SchedulerConfig};
use crate::scheduler::all_paths::AllPathSelector;
use crate::scheduler::compiled::CompiledScheduler;
use crate::scheduler::weighted_rr::WeightedRoundRobin;
use crate::security::{ReservedFrameProtection, TransportSecurity};
use crate::sockets::PathTransportSet;
use crate::udp_service::{
    ServicePathPolicy, ServiceReturnMode, ServiceSchedulerConfig, UdpServiceError, UserlandUdpService,
};

/// More than one queued datagram means the app-facing socket has pressure.
///
/// Rust does not decide policy from that pressure; it only preserves the path
/// choice long enough for the frame coalescer to fill one bounded carrier frame
/// before asking the Python-compiled scheduler for the next path.
const BURST_BATCH_MIN_DATAGRAMS: usize = 2;
const BURST_AGGREGATION_MAX_DELAY: Duration = Duration::from_micros(250);
// TODO(perf): Keep this as an execution primitive, not scheduler policy. Python
// can later compile a per-service run length once flowlet/reorder telemetry is
// good enough; for now a larger run avoids packet-by-packet striping of one UDP
// flow, which is especially harsh for WireGuard replay/TCP behavior.
const BURST_PATH_SEND_RUN_DATAGRAMS: usize = 4096;
const REMOTE_EMIT_BATCH_LIMIT: usize = 64;
const REMOTE_REORDER_BUFFER_LIMIT: usize = 8192;
// Scoped worker creation is only worth it for unusually large Rust-side batches.
// The two-vCPU Hyper-V performance lab showed that parallelizing ordinary
// 2048-frame batches added more scheduler/thread churn than AEAD throughput.
const PARALLEL_PROTECT_MIN_FRAMES: usize = 4096;

/// Core userland UDP dataplane.
#[derive(Debug)]
pub struct CoreDataplane {
    services: Vec<UserlandUdpService>,
    paths: Vec<CorePathConfig>,
    path_transports: PathTransportSet,
    transport_security: TransportSecurity,
    scheduler: CompiledScheduler,
    next_sequences: HashMap<Option<u32>, u64>,
    next_datagram_id: u32,
    metrics: DataplaneMetrics,
    remote_fragments: FragmentReassembly,
    remote_dedupe: DedupeWindow,
    remote_reorder: HashMap<RemoteReorderKey, RemoteReorderBuffer>,
    reserved_events: Vec<ReservedServiceEvent>,
    learned_sources: HashMap<ServiceId, SocketAddr>,
    learned_path_remotes: HashMap<(u32, u16), SocketAddr>,
    disabled_services: HashMap<ServiceId, String>,
    service_schedulers: HashMap<ServiceId, ServiceSchedulerConfig>,
    service_weighted_schedulers: HashMap<ServiceId, WeightedRoundRobin>,
    service_flowlets: HashMap<ServiceFlowletKey, ServiceFlowletState>,
}

impl CoreDataplane {
    /// Bind all services from compiled runtime config.
    pub fn bind(config: CoreRuntimeConfig) -> Result<Self, DataplaneError> {
        let services = config
            .services()
            .iter()
            .cloned()
            .map(UserlandUdpService::bind)
            .collect::<Result<Vec<_>, _>>()?;

        let service_schedulers = config
            .services()
            .iter()
            .map(|service| (service.service_id(), service.scheduler()))
            .collect();
        let service_weighted_schedulers = service_weighted_schedulers(config.services(), config.paths());

        Ok(Self {
            services,
            paths: config.paths().to_vec(),
            path_transports: PathTransportSet::bind(config.paths())?,
            transport_security: TransportSecurity::from_config(config.security()),
            scheduler: CompiledScheduler::compile(config.scheduler().mode(), config.paths()),
            next_sequences: HashMap::new(),
            next_datagram_id: 1,
            metrics: DataplaneMetrics::new(config.paths()),
            remote_fragments: FragmentReassembly::default(),
            remote_dedupe: DedupeWindow::default(),
            remote_reorder: HashMap::new(),
            reserved_events: Vec::new(),
            learned_sources: HashMap::new(),
            learned_path_remotes: HashMap::new(),
            disabled_services: HashMap::new(),
            service_schedulers,
            service_weighted_schedulers,
            service_flowlets: HashMap::new(),
        })
    }

    /// Atomically reapply already-compiled runtime config from the Python control plane.
    ///
    /// This is intentionally a runtime-state operation, not config interpretation.
    /// Rust preserves compatible sockets, binds new sockets before swapping the
    /// active service set, and reports what changed so Python can publish clean
    /// diagnostics or decide whether a more disruptive restart is needed.
    pub fn reapply_config(&mut self, config: CoreRuntimeConfig) -> Result<ReapplyOutcome, DataplaneError> {
        let mut outcome = ReapplyOutcome::default();
        let mut services = Vec::with_capacity(config.services().len());
        let retained_path_ids: HashSet<u16> = config.paths().iter().map(|path| path.path_id()).collect();
        let retained_peer_scopes: HashSet<u32> = config.security().local_receiver_indexes().into_iter().collect();

        for desired in config.services().iter().cloned() {
            if let Some(current) = self.service(desired.name()) {
                if current.config() == &desired {
                    outcome.unchanged += 1;
                    services.push(current.clone_with_config(desired)?);
                } else if current.can_preserve_socket_for(&desired)? {
                    outcome.updated += 1;
                    services.push(current.clone_with_config(desired)?);
                } else {
                    // Listener changes are possible, but they are disruptive to
                    // that service. Bind first and swap only after every service
                    // in the requested config has been prepared.
                    outcome.rebound += 1;
                    services.push(UserlandUdpService::bind(desired)?);
                }
            } else {
                outcome.added += 1;
                services.push(UserlandUdpService::bind(desired)?);
            }
        }

        outcome.removed = self
            .services
            .iter()
            .filter(|service| {
                !config
                    .services()
                    .iter()
                    .any(|desired| desired.name() == service.config().name())
            })
            .count();

        self.services = services;
        self.service_schedulers = config
            .services()
            .iter()
            .map(|service| (service.service_id(), service.scheduler()))
            .collect();
        self.service_weighted_schedulers = service_weighted_schedulers(config.services(), config.paths());
        self.disabled_services.retain(|service_id, _reason| {
            self.services
                .iter()
                .any(|service| service.config().service_id() == *service_id)
        });
        self.paths = config.paths().to_vec();
        self.path_transports = self.path_transports.rebind_preserving(config.paths())?;
        self.transport_security = TransportSecurity::from_config(config.security());
        self.learned_path_remotes.retain(|(peer_scope, path_id), _remote| {
            retained_peer_scopes.contains(peer_scope) && retained_path_ids.contains(path_id)
        });
        self.scheduler = CompiledScheduler::compile(config.scheduler().mode(), config.paths());
        self.metrics.reconcile_paths(config.paths());
        Ok(outcome)
    }

    /// Hot-apply Python-compiled scheduler primitives without touching sockets.
    ///
    /// Full config reapply is allowed to add/remove UDP services and rebuild path
    /// transport sockets. Live telemetry refreshes must be much narrower: Python
    /// may update weights, path state, MTU, and primitive scheduler facts, while
    /// Rust keeps the already-bound userland and carrier sockets in place.
    pub fn reapply_scheduler(
        &mut self,
        paths: Vec<CorePathConfig>,
        scheduler: SchedulerConfig,
    ) -> Result<ReapplyOutcome, DataplaneError> {
        if paths.is_empty() {
            return Err(DataplaneError::UdpService(UdpServiceError::MissingPath));
        }
        let mut path_ids = HashSet::with_capacity(paths.len());
        for path in &paths {
            if !path_ids.insert(path.path_id()) {
                return Err(DataplaneError::UdpService(UdpServiceError::DuplicatePathId(
                    path.path_id(),
                )));
            }
        }

        self.paths = paths;
        self.scheduler = CompiledScheduler::compile(scheduler.mode(), &self.paths);
        self.service_weighted_schedulers =
            service_weighted_schedulers_from_primitives(&self.service_schedulers, &self.paths);
        self.metrics.reconcile_paths(&self.paths);
        Ok(ReapplyOutcome {
            unchanged: self.services.len(),
            updated: self.paths.len(),
            rebound: 0,
            added: 0,
            removed: 0,
        })
    }

    /// Return a bound service by name.
    pub fn service(&self, name: &str) -> Option<&UserlandUdpService> {
        self.services.iter().find(|service| service.config().name() == name)
    }

    /// Return the compiled paths Rust can use for frame production.
    pub fn paths(&self) -> &[CorePathConfig] {
        &self.paths
    }

    /// Return Rust-owned telemetry counters for service status and monitoring.
    pub fn metrics_snapshot(&self) -> MetricsSnapshot {
        let mut snapshot = self.metrics.snapshot();
        self.attach_scheduler_runtime_facts(&mut snapshot);
        self.attach_reorder_buffer_facts(&mut snapshot);
        self.attach_socket_facts(&mut snapshot);
        snapshot
    }

    /// Drain sparse real-data timing samples for Python-owned latency estimation.
    pub fn drain_data_timing_samples(&mut self) -> (Vec<DataTrafficTimingSample>, Vec<DataTrafficTimingSample>) {
        self.metrics.drain_data_timing_samples()
    }

    /// Return service ids stopped by Python-applied runtime policy.
    pub fn disabled_services_snapshot(&self) -> HashMap<ServiceId, String> {
        self.disabled_services.clone()
    }

    /// Apply a Python-decided service stop to the Rust hot path.
    pub fn disable_service(&mut self, service_id: ServiceId, reason: impl Into<String>) {
        self.disabled_services.insert(service_id, reason.into());
    }

    /// Clear a Python-decided service stop from the Rust hot path.
    pub fn enable_service(&mut self, service_id: ServiceId) {
        self.disabled_services.remove(&service_id);
    }

    /// Apply Python-decided service fanout primitives to any service id.
    pub fn set_service_scheduler(&mut self, service_id: ServiceId, scheduler: ServiceSchedulerConfig) {
        self.service_schedulers.insert(service_id, scheduler);
        self.service_weighted_schedulers =
            service_weighted_schedulers_from_primitives(&self.service_schedulers, &self.paths);
    }

    /// Return actual local path transport socket addresses.
    pub fn path_transport_local_addrs(&self) -> Result<HashMap<u16, SocketAddr>, DataplaneError> {
        self.path_transports.local_addrs().map_err(DataplaneError::from)
    }

    fn attach_scheduler_runtime_facts(&self, snapshot: &mut MetricsSnapshot) {
        for (path_id, runtime) in self.scheduler.path_runtime_snapshots(&self.paths) {
            let path = snapshot.paths.entry(path_id).or_default();
            path.scheduler_in_flight_packets = runtime.in_flight_packets;
            path.scheduler_in_flight_bytes = runtime.in_flight_bytes;
            path.scheduler_predicted_delivery_us = runtime.predicted_delivery_us;
        }
    }

    fn attach_reorder_buffer_facts(&self, snapshot: &mut MetricsSnapshot) {
        let now = Instant::now();
        for buffer in self.remote_reorder.values() {
            for entry in buffer.entries.values() {
                let path = snapshot.paths.entry(entry.path_id).or_default();
                path.reorder_buffer_packets = path.reorder_buffer_packets.saturating_add(1);
                path.reorder_buffer_oldest_age_us = path
                    .reorder_buffer_oldest_age_us
                    .max(duration_us(now.duration_since(entry.received_at)));
            }
        }
    }

    fn attach_socket_facts(&self, snapshot: &mut MetricsSnapshot) {
        for (path_id, socket) in self.path_transports.socket_snapshots() {
            let path = snapshot.paths.entry(path_id).or_default();
            path.socket_receive_buffer_bytes = socket.receive_buffer_bytes;
            path.socket_send_buffer_bytes = socket.send_buffer_bytes;
            path.socket_drain_quantum = socket.drain_quantum;
        }
    }

    /// Record telemetry for a received remote data frame.
    ///
    /// Python still owns what to do with this fact. Rust only updates the same counters the production receive loop
    /// will eventually maintain directly.
    pub fn observe_received_data_frame(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload_len: usize,
    ) {
        self.metrics
            .record_receive(service_id, path_id, sequence, payload_len, None);
    }

    /// Record a received reserved-service payload without interpreting it.
    pub fn observe_received_reserved_service_payload(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload: Vec<u8>,
        frame_bytes: usize,
        peer_scope: Option<u32>,
    ) {
        self.metrics
            .record_reserved_received(path_id, frame_bytes, sequence, peer_scope);
        self.reserved_events.push(ReservedServiceEvent {
            service_id,
            path_id,
            sequence,
            payload,
            frame_bytes,
            peer_scope,
        });
    }

    /// Drain reserved-service events for Python-owned decoding and policy.
    pub fn drain_reserved_service_events(&mut self) -> Vec<ReservedServiceEvent> {
        std::mem::take(&mut self.reserved_events)
    }

    /// Send one Python-composed service payload through the normal path scheduler.
    ///
    /// This lets Python inject internal service traffic, diagnostics, or future
    /// helper payloads without pretending they came from an app-facing UDP
    /// socket. Rust does not decode the payload or decide whether the service id
    /// is meaningful; it only applies the compiled scheduler/path primitives and
    /// frames the bytes. Service-level scheduler policy is still Python-owned
    /// and can later compile to per-service execution hints.
    pub fn transmit_service_payload(
        &mut self,
        service_id: ServiceId,
        payload: Vec<u8>,
    ) -> Result<Vec<ControlTransmitPlan>, DataplaneError> {
        let fanout = self
            .service_schedulers
            .get(&service_id)
            .cloned()
            .unwrap_or_else(ServiceSchedulerConfig::normal)
            .effective_fanout(payload.len());
        if fanout != 1 {
            return self.transmit_service_payload_fanout(service_id, payload, fanout);
        }
        let planned = self.plan_injected_datagram(service_id, payload)?;
        let frames = self.coalesce_planned_frames(service_id, planned)?;
        let mut plans = Vec::with_capacity(frames.len());
        for frame_plan in frames {
            let encoded = frame_plan.frame.encode()?;
            if encoded.len() > frame_plan.path.mtu() {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id: frame_plan.path.path_id(),
                    frame_len: encoded.len(),
                    mtu: frame_plan.path.mtu(),
                });
            }
            if self.try_send_path_frame(frame_plan.path.path_id(), &frame_plan.frame, None, encoded.len(), false)? {
                self.metrics
                    .record_reserved_sent(frame_plan.path.path_id(), encoded.len());
                plans.push(ControlTransmitPlan {
                    path_id: frame_plan.path.path_id(),
                    sequence: frame_plan.datagrams[0].sequence,
                    frame_len: encoded.len(),
                    payload_len: frame_plan.datagrams.iter().map(|datagram| datagram.payload_len).sum(),
                });
            }
        }
        Ok(plans)
    }

    /// Send one Python-composed service payload through one exact path.
    ///
    /// Python owns the reason this is needed. The Rust side only provides the
    /// compiled execution primitive: frame these bytes for this service id and
    /// put the frame on this already-configured path without interpreting the
    /// reserved payload semantics.
    pub fn transmit_service_payload_on_path(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        payload: Vec<u8>,
    ) -> Result<Vec<ControlTransmitPlan>, DataplaneError> {
        let Some(path) = self
            .paths
            .iter()
            .find(|candidate| candidate.path_id() == path_id && candidate.enabled())
        else {
            return Err(DataplaneError::NoPathAvailable);
        };
        let mtu = path.mtu();
        let sequence = self.next_sequence_for(None);
        let frame = Frame::data(service_id, path_id, sequence, payload)?;
        let encoded = frame.encode()?;
        if encoded.len() > mtu {
            return Err(DataplaneError::FrameExceedsPathMtu {
                path_id,
                frame_len: encoded.len(),
                mtu,
            });
        }
        let payload_len = frame.payload.len();
        if self.try_send_path_frame(path_id, &frame, None, encoded.len(), false)? {
            self.metrics.record_reserved_sent(path_id, encoded.len());
            return Ok(vec![ControlTransmitPlan {
                path_id,
                sequence,
                frame_len: encoded.len(),
                payload_len,
            }]);
        }
        Ok(vec![])
    }

    /// Send one Python-composed service payload back to an authenticated peer scope.
    pub fn transmit_service_payload_to_peer(
        &mut self,
        service_id: ServiceId,
        payload: Vec<u8>,
        peer_scope: u32,
    ) -> Result<Vec<ControlTransmitPlan>, DataplaneError> {
        let source = "127.0.0.1:0"
            .parse()
            .expect("injected service source placeholder must parse");
        let planned = self.plan_datagrams(
            service_id,
            vec![ReceivedDatagram {
                payload,
                source,
                peer_scope: Some(peer_scope),
            }],
        )?;
        let frames = self.coalesce_planned_frames(service_id, planned)?;
        let mut plans = Vec::with_capacity(frames.len());
        for frame_plan in frames {
            let encoded = frame_plan.frame.encode()?;
            if encoded.len() > frame_plan.path.mtu() {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id: frame_plan.path.path_id(),
                    frame_len: encoded.len(),
                    mtu: frame_plan.path.mtu(),
                });
            }
            if self.try_send_path_frame(
                frame_plan.path.path_id(),
                &frame_plan.frame,
                Some(peer_scope),
                encoded.len(),
                false,
            )? {
                self.metrics
                    .record_reserved_sent(frame_plan.path.path_id(), encoded.len());
                plans.push(ControlTransmitPlan {
                    path_id: frame_plan.path.path_id(),
                    sequence: frame_plan.datagrams[0].sequence,
                    frame_len: encoded.len(),
                    payload_len: frame_plan.datagrams.iter().map(|datagram| datagram.payload_len).sum(),
                });
            }
        }
        Ok(plans)
    }

    fn transmit_service_payload_fanout(
        &mut self,
        service_id: ServiceId,
        payload: Vec<u8>,
        fanout: u16,
    ) -> Result<Vec<ControlTransmitPlan>, DataplaneError> {
        let mut path_indices = AllPathSelector::select_path_indices(&self.paths, payload.len());
        if fanout > 0 {
            path_indices.truncate(usize::from(fanout));
        }
        if path_indices.is_empty() {
            return Err(DataplaneError::NoPathAvailable);
        }

        let sequence = self.next_sequence_for(None);
        let mut plans = Vec::with_capacity(path_indices.len());
        for path_index in path_indices {
            let path = &self.paths[path_index];
            let path_id = path.path_id();
            let mtu = path.mtu();
            let frame = Frame::data(service_id, path_id, sequence, payload.clone())?;
            let encoded = frame.encode()?;
            if encoded.len() > mtu {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id,
                    frame_len: encoded.len(),
                    mtu,
                });
            }
            if self.try_send_path_frame(path_id, &frame, None, encoded.len(), false)? {
                self.metrics.record_reserved_sent(path_id, encoded.len());
                plans.push(ControlTransmitPlan {
                    path_id,
                    sequence,
                    frame_len: encoded.len(),
                    payload_len: payload.len(),
                });
            }
        }
        Ok(plans)
    }

    /// Receive one local UDP datagram, pass it through the v1 data-frame
    /// boundary, and emit the original virtual payload to the service target.
    pub fn forward_one_for_service(&mut self, name: &str) -> Result<ForwardOutcome, DataplaneError> {
        let outcomes = self.forward_available_for_service(name, 1)?;
        outcomes.into_iter().next().ok_or(DataplaneError::NoDatagramForwarded)
    }

    /// Receive one or more queued UDP datagrams and forward them through Gatherlink framing.
    ///
    /// The first receive blocks like `forward_one_for_service`; the remaining reads drain immediately available
    /// packets. Tiny same-path datagrams can become one batch frame, while oversized datagrams are fragmented only
    /// when no non-busy path can carry them whole.
    pub fn forward_available_for_service(
        &mut self,
        name: &str,
        max_datagrams: usize,
    ) -> Result<Vec<ForwardOutcome>, DataplaneError> {
        if max_datagrams == 0 {
            return Ok(Vec::new());
        }

        let service_index = self
            .services
            .iter()
            .position(|service| service.config().name() == name)
            .ok_or_else(|| DataplaneError::UnknownService(name.to_owned()))?;

        let datagrams = self.receive_datagrams(service_index, max_datagrams)?;
        self.forward_datagrams(service_index, name, datagrams)
    }

    /// Drain queued local UDP datagrams without blocking and forward them through Gatherlink framing.
    ///
    /// Long-running supervisors need this shape so one quiet app-facing service cannot stall path receives,
    /// IPC commands, or shutdown checks. The blocking `forward_available_for_service` remains useful for focused
    /// smoke tests that want to wait for a single local datagram.
    pub fn forward_available_for_service_nonblocking(
        &mut self,
        name: &str,
        max_datagrams: usize,
    ) -> Result<Vec<ForwardOutcome>, DataplaneError> {
        if max_datagrams == 0 {
            return Ok(Vec::new());
        }

        let service_index = self
            .services
            .iter()
            .position(|service| service.config().name() == name)
            .ok_or_else(|| DataplaneError::UnknownService(name.to_owned()))?;

        let datagrams = self.try_receive_datagrams(service_index, max_datagrams)?;
        if datagrams.is_empty() {
            return Ok(Vec::new());
        }
        self.forward_datagrams(service_index, name, datagrams)
    }

    /// Drain queued local UDP datagrams and return aggregate counters only.
    ///
    /// The production Python runner uses this to keep per-packet outcome objects
    /// out of the hot path. Detailed outcome-producing methods remain available
    /// for focused tests and diagnostics.
    pub fn forward_available_for_service_nonblocking_summary(
        &mut self,
        name: &str,
        max_datagrams: usize,
    ) -> Result<PacketBatchSummary, DataplaneError> {
        if max_datagrams == 0 {
            return Ok(PacketBatchSummary::default());
        }

        let service_index = self
            .services
            .iter()
            .position(|service| service.config().name() == name)
            .ok_or_else(|| DataplaneError::UnknownService(name.to_owned()))?;

        let datagrams = self.try_receive_datagrams(service_index, max_datagrams)?;
        if datagrams.is_empty() {
            return Ok(PacketBatchSummary::default());
        }
        self.forward_datagrams_summary(service_index, name, datagrams)
    }

    /// Drain queued local UDP datagrams with a Python-compiled byte budget.
    ///
    /// This is an execution primitive for mixed-service fairness. Python owns
    /// the policy and decides when a service should receive a byte cap; Rust
    /// only stops draining that service slot after the cap is reached. A single
    /// datagram may exceed the cap because UDP payloads are indivisible.
    pub fn forward_available_for_service_budget_summary(
        &mut self,
        name: &str,
        max_datagrams: usize,
        max_payload_bytes: usize,
    ) -> Result<PacketBatchSummary, DataplaneError> {
        if max_payload_bytes == 0 {
            return self.forward_available_for_service_nonblocking_summary(name, max_datagrams);
        }
        if max_datagrams == 0 {
            return Ok(PacketBatchSummary::default());
        }

        let service_index = self
            .services
            .iter()
            .position(|service| service.config().name() == name)
            .ok_or_else(|| DataplaneError::UnknownService(name.to_owned()))?;

        let datagrams = self.try_receive_datagrams_with_byte_budget(service_index, max_datagrams, max_payload_bytes)?;
        if datagrams.is_empty() {
            return Ok(PacketBatchSummary::default());
        }
        self.forward_datagrams_summary(service_index, name, datagrams)
    }

    /// Receive encoded Gatherlink frames from path sockets and emit virtual UDP payloads.
    pub fn receive_available_from_paths(
        &mut self,
        max_frames: usize,
    ) -> Result<Vec<RemoteDeliverOutcome>, DataplaneError> {
        let frames = self.path_transports.receive_available(max_frames)?;
        let mut outcomes = Vec::new();
        for received in frames {
            let unprotected = match self.transport_security.unprotect_packet_with_session(&received.bytes) {
                Ok(unprotected) => unprotected,
                Err(CryptoError::SilentDrop) => {
                    self.metrics
                        .record_security_drop(received.path_id, received.bytes.len());
                    continue;
                }
                Err(error) => return Err(DataplaneError::Crypto(error)),
            };
            let peer_scope = unprotected.local_receiver_index;
            if let Some(local_receiver_index) = peer_scope {
                self.learned_path_remotes
                    .insert((local_receiver_index, received.path_id), received.source);
            }
            let decoded = unprotected.frame;
            let frame_bytes_len = received.bytes.len();
            match decoded.kind {
                FrameKind::Data => {
                    if let Some(payload) = self.remote_fragments.push_or_payload(&decoded)? {
                        if is_reserved_service_id(decoded.service_id) {
                            self.observe_received_reserved_service_payload(
                                decoded.service_id,
                                decoded.path_id,
                                decoded.sequence,
                                payload,
                                frame_bytes_len,
                                peer_scope,
                            );
                        } else {
                            if let Some(observation) = self.observe_user_payload_first_seen(
                                decoded.service_id,
                                decoded.path_id,
                                decoded.sequence,
                                payload.len(),
                                peer_scope,
                            ) {
                                outcomes.push(self.emit_remote_payload(
                                    decoded.service_id,
                                    decoded.path_id,
                                    decoded.sequence,
                                    &payload,
                                    peer_scope,
                                    observation,
                                )?);
                            }
                        }
                    }
                }
                FrameKind::Batch => {
                    for (index, payload) in decoded.batch_payloads()?.iter().enumerate() {
                        let sequence = decoded.sequence + index as u64;
                        if is_reserved_service_id(decoded.service_id) {
                            self.observe_received_reserved_service_payload(
                                decoded.service_id,
                                decoded.path_id,
                                sequence,
                                payload.clone(),
                                frame_bytes_len,
                                peer_scope,
                            );
                        } else if let Some(observation) = self.observe_user_payload_first_seen(
                            decoded.service_id,
                            decoded.path_id,
                            sequence,
                            payload.len(),
                            peer_scope,
                        ) {
                            outcomes.push(self.emit_remote_payload(
                                decoded.service_id,
                                decoded.path_id,
                                sequence,
                                payload,
                                peer_scope,
                                observation,
                            )?);
                        }
                    }
                }
                FrameKind::Control => {
                    self.observe_received_reserved_service_payload(
                        decoded.service_id,
                        decoded.path_id,
                        decoded.sequence,
                        decoded.payload,
                        frame_bytes_len,
                        peer_scope,
                    );
                }
            }
        }
        Ok(outcomes)
    }

    /// Receive encoded Gatherlink frames and return aggregate counters only.
    pub fn receive_available_from_paths_summary(
        &mut self,
        max_frames: usize,
    ) -> Result<PacketBatchSummary, DataplaneError> {
        let mut summary = PacketBatchSummary::default();
        let mut pending_emit = PendingRemoteEmitBatch::default();
        let mut path_transports = std::mem::take(&mut self.path_transports);
        let drain_result = path_transports.drain_available(max_frames, |path_id, source, bytes| {
            self.process_received_path_frame_summary(path_id, source, bytes, &mut summary, &mut pending_emit)
        });
        self.path_transports = path_transports;
        match drain_result? {
            Ok(()) => {
                self.flush_expired_reorder_buffers(&mut pending_emit, &mut summary)?;
                self.flush_remote_emit_batch(&mut pending_emit, &mut summary)?;
                Ok(summary)
            }
            Err(error) => Err(error),
        }
    }

    fn process_received_path_frame_summary(
        &mut self,
        path_id: u16,
        source: SocketAddr,
        bytes: &[u8],
        summary: &mut PacketBatchSummary,
        pending_emit: &mut PendingRemoteEmitBatch,
    ) -> Result<(), DataplaneError> {
        let unprotected = match self.transport_security.unprotect_packet_with_session(bytes) {
            Ok(unprotected) => unprotected,
            Err(CryptoError::SilentDrop) => {
                self.metrics.record_security_drop(path_id, bytes.len());
                return Ok(());
            }
            Err(error) => return Err(DataplaneError::Crypto(error)),
        };
        let peer_scope = unprotected.local_receiver_index;
        if let Some(local_receiver_index) = peer_scope {
            self.learned_path_remotes
                .insert((local_receiver_index, path_id), source);
        }
        let decoded = unprotected.frame;
        let frame_bytes_len = bytes.len();
        match decoded.kind {
            FrameKind::Data => {
                if let Some(payload) = self.remote_fragments.push_or_payload(&decoded)? {
                    if is_reserved_service_id(decoded.service_id) {
                        self.flush_remote_emit_batch(pending_emit, summary)?;
                        self.observe_received_reserved_service_payload(
                            decoded.service_id,
                            decoded.path_id,
                            decoded.sequence,
                            payload,
                            frame_bytes_len,
                            peer_scope,
                        );
                    } else if let Some(observation) = self.observe_user_payload_first_seen(
                        decoded.service_id,
                        decoded.path_id,
                        decoded.sequence,
                        payload.len(),
                        peer_scope,
                    ) {
                        self.queue_remote_payload_with_reorder(
                            decoded.service_id,
                            decoded.path_id,
                            decoded.sequence,
                            payload,
                            peer_scope,
                            observation,
                            pending_emit,
                            summary,
                        )?;
                    }
                }
            }
            FrameKind::Batch => {
                self.flush_remote_emit_batch(pending_emit, summary)?;
                let payloads = decoded.batch_payloads()?;
                if is_reserved_service_id(decoded.service_id) {
                    for (index, payload) in payloads.iter().enumerate() {
                        let sequence = decoded.sequence + index as u64;
                        self.observe_received_reserved_service_payload(
                            decoded.service_id,
                            decoded.path_id,
                            sequence,
                            payload.clone(),
                            frame_bytes_len,
                            peer_scope,
                        );
                    }
                } else {
                    self.emit_remote_batch_payloads_summary(
                        decoded.service_id,
                        decoded.path_id,
                        decoded.sequence,
                        &payloads,
                        peer_scope,
                        pending_emit,
                        summary,
                    )?;
                }
            }
            FrameKind::Control => {
                self.flush_remote_emit_batch(pending_emit, summary)?;
                self.observe_received_reserved_service_payload(
                    decoded.service_id,
                    decoded.path_id,
                    decoded.sequence,
                    decoded.payload,
                    frame_bytes_len,
                    peer_scope,
                );
            }
        }
        Ok(())
    }

    fn emit_remote_batch_payloads_summary(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        base_sequence: u64,
        payloads: &[Vec<u8>],
        peer_scope: Option<u32>,
        pending: &mut PendingRemoteEmitBatch,
        summary: &mut PacketBatchSummary,
    ) -> Result<(), DataplaneError> {
        self.ensure_service_enabled(service_id)?;
        for (index, payload) in payloads.iter().enumerate() {
            let sequence = base_sequence + index as u64;
            if let Some(observation) =
                self.observe_user_payload_first_seen(service_id, path_id, sequence, payload.len(), peer_scope)
            {
                self.queue_remote_payload_with_reorder(
                    service_id,
                    path_id,
                    sequence,
                    payload.clone(),
                    peer_scope,
                    observation,
                    pending,
                    summary,
                )?;
            }
        }
        Ok(())
    }

    fn queue_remote_payload_with_reorder(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload: Vec<u8>,
        peer_scope: Option<u32>,
        observation: SequenceObservation,
        pending: &mut PendingRemoteEmitBatch,
        summary: &mut PacketBatchSummary,
    ) -> Result<(), DataplaneError> {
        let hold = self.reorder_hold_for_path(path_id);
        if hold.is_zero() {
            return self.queue_remote_payload_summary(
                service_id,
                path_id,
                sequence,
                payload,
                peer_scope,
                observation,
                pending,
                summary,
            );
        }

        let key = RemoteReorderKey { service_id, peer_scope };
        if !self.remote_reorder.contains_key(&key) && !observation.out_of_order && observation.missing_packets == 0 {
            return self.queue_remote_payload_summary(
                service_id,
                path_id,
                sequence,
                payload,
                peer_scope,
                observation,
                pending,
                summary,
            );
        }

        let now = Instant::now();
        let first_expected_sequence = if observation.missing_packets > 0 {
            sequence.saturating_sub(observation.missing_packets)
        } else {
            sequence
        };
        let buffer = self
            .remote_reorder
            .entry(key)
            .or_insert_with(|| RemoteReorderBuffer::new(first_expected_sequence));
        buffer.entries.entry(sequence).or_insert(RemoteReorderEntry {
            path_id,
            payload,
            observation,
            received_at: now,
        });

        let ready = self.drain_reorder_ready(key, now, hold);
        for entry in ready {
            self.queue_remote_payload_summary(
                service_id,
                entry.path_id,
                entry.sequence,
                entry.payload,
                peer_scope,
                entry.observation,
                pending,
                summary,
            )?;
        }
        Ok(())
    }

    fn flush_expired_reorder_buffers(
        &mut self,
        pending: &mut PendingRemoteEmitBatch,
        summary: &mut PacketBatchSummary,
    ) -> Result<(), DataplaneError> {
        let keys = self.remote_reorder.keys().copied().collect::<Vec<_>>();
        for key in keys {
            let ready = self.drain_expired_reorder_ready(key, Instant::now());
            for entry in ready {
                self.queue_remote_payload_summary(
                    key.service_id,
                    entry.path_id,
                    entry.sequence,
                    entry.payload,
                    key.peer_scope,
                    entry.observation,
                    pending,
                    summary,
                )?;
            }
        }
        Ok(())
    }

    fn drain_expired_reorder_ready(&mut self, key: RemoteReorderKey, now: Instant) -> Vec<ReadyRemoteReorderEntry> {
        let Some(buffer) = self.remote_reorder.get(&key) else {
            return Vec::new();
        };
        let Some((_oldest_sequence, oldest_entry)) = buffer.entries.iter().next() else {
            return Vec::new();
        };
        let hold = self.reorder_hold_for_path(oldest_entry.path_id);
        if now.duration_since(oldest_entry.received_at) < hold {
            return Vec::new();
        }
        self.drain_reorder_ready(key, now, hold)
    }

    fn drain_reorder_ready(
        &mut self,
        key: RemoteReorderKey,
        now: Instant,
        hold: Duration,
    ) -> Vec<ReadyRemoteReorderEntry> {
        let Some(buffer) = self.remote_reorder.get_mut(&key) else {
            return Vec::new();
        };
        let mut ready = Vec::new();
        loop {
            if let Some(entry) = buffer.entries.remove(&buffer.next_sequence) {
                let sequence = buffer.next_sequence;
                buffer.next_sequence = buffer.next_sequence.wrapping_add(1).max(1);
                ready.push(ReadyRemoteReorderEntry {
                    sequence,
                    path_id: entry.path_id,
                    payload: entry.payload,
                    observation: entry.observation,
                });
                continue;
            }

            let Some((&oldest_sequence, oldest_entry)) = buffer.entries.iter().next() else {
                break;
            };
            if now.duration_since(oldest_entry.received_at) < hold
                && buffer.entries.len() <= REMOTE_REORDER_BUFFER_LIMIT
            {
                break;
            }
            buffer.next_sequence = oldest_sequence;
        }
        if buffer.entries.is_empty() {
            self.remote_reorder.remove(&key);
        }
        ready
    }

    fn reorder_hold_for_path(&self, path_id: u16) -> Duration {
        self.paths
            .iter()
            .find(|path| path.path_id() == path_id)
            .map(|path| Duration::from_micros(u64::from(path.primitives().reorder_hold_us())))
            .unwrap_or_default()
    }

    fn queue_remote_payload_summary(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload: Vec<u8>,
        peer_scope: Option<u32>,
        observation: SequenceObservation,
        pending: &mut PendingRemoteEmitBatch,
        summary: &mut PacketBatchSummary,
    ) -> Result<(), DataplaneError> {
        let service_index = self
            .services
            .iter()
            .position(|service| service.config().service_id() == service_id)
            .ok_or(DataplaneError::UnknownServiceId(service_id))?;
        self.ensure_service_enabled(service_id)?;
        let return_mode = self.services[service_index].config().return_mode();
        let target = match return_mode {
            ServiceReturnMode::Fixed | ServiceReturnMode::PeerScopedSource => {
                self.services[service_index].config().target()
            }
            ServiceReturnMode::LearnedSingleSource => self
                .learned_sources
                .get(&service_id)
                .copied()
                .unwrap_or_else(|| self.services[service_index].config().target()),
        };

        if matches!(return_mode, ServiceReturnMode::PeerScopedSource) && peer_scope.is_some() {
            let emitted =
                self.emit_remote_payload_summary(service_id, path_id, sequence, &payload, peer_scope, observation)?;
            summary.record(emitted);
            return Ok(());
        }

        let key = PendingRemoteEmitKey {
            service_index,
            service_id,
            path_id,
            peer_scope,
            target,
        };
        if pending.key.as_ref().is_some_and(|current| current != &key) || pending.items.len() >= REMOTE_EMIT_BATCH_LIMIT
        {
            self.flush_remote_emit_batch(pending, summary)?;
        }
        pending.key = Some(key);
        pending.items.push(PendingRemoteEmit {
            payload,
            sequence,
            observation,
        });
        if pending.items.len() >= REMOTE_EMIT_BATCH_LIMIT {
            self.flush_remote_emit_batch(pending, summary)?;
        }
        Ok(())
    }

    fn flush_remote_emit_batch(
        &mut self,
        pending: &mut PendingRemoteEmitBatch,
        summary: &mut PacketBatchSummary,
    ) -> Result<(), DataplaneError> {
        let Some(key) = pending.key else {
            return Ok(());
        };
        if pending.items.is_empty() {
            pending.key = None;
            return Ok(());
        }

        let slices = pending
            .items
            .iter()
            .map(|item| item.payload.as_slice())
            .collect::<Vec<_>>();
        let emitted_count = self.services[key.service_index].emit_many_to(&slices, key.target)?;
        let service_name = self.services[key.service_index].config().name().to_owned();
        for item in pending.items.iter().take(emitted_count) {
            let emitted = item.payload.len();
            self.metrics.record_receive_for_service(
                &service_name,
                key.service_id,
                key.path_id,
                item.sequence,
                emitted,
                item.observation,
            );
            summary.record(emitted);
        }
        pending.items.clear();
        pending.key = None;
        Ok(())
    }

    fn observe_user_payload_first_seen(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload_len: usize,
        peer_scope: Option<u32>,
    ) -> Option<SequenceObservation> {
        match self.remote_dedupe.observe_in_scope(service_id, peer_scope, sequence) {
            DedupeObservation::FirstSeen => {
                let observation = self
                    .metrics
                    .record_receive(service_id, path_id, sequence, payload_len, peer_scope);
                Some(observation)
            }
            DedupeObservation::Duplicate => {
                let service_name = self
                    .services
                    .iter()
                    .find(|service| service.config().service_id() == service_id)
                    .map(|service| service.config().name().to_owned());
                let expected = self.expected_fanout_duplicate(service_id, payload_len);
                self.metrics.record_duplicate_receive(
                    service_name.as_deref(),
                    service_id,
                    path_id,
                    payload_len,
                    expected,
                );
                None
            }
        }
    }

    fn expected_fanout_duplicate(&self, service_id: ServiceId, payload_len: usize) -> bool {
        self.service_schedulers
            .get(&service_id)
            .cloned()
            .unwrap_or_else(ServiceSchedulerConfig::normal)
            .effective_fanout(payload_len)
            != 1
    }

    fn receive_datagrams(
        &self,
        service_index: usize,
        max_datagrams: usize,
    ) -> Result<Vec<ReceivedDatagram>, DataplaneError> {
        let mut datagrams = Vec::with_capacity(max_datagrams);
        let mut buffer = vec![0_u8; u16::MAX as usize];
        let (length, source) = self.services[service_index].recv_from(&mut buffer)?;
        datagrams.push(ReceivedDatagram {
            payload: buffer[..length].to_vec(),
            source,
            peer_scope: None,
        });

        for _ in 1..max_datagrams {
            let Some((length, source)) = self.services[service_index].try_recv_from(&mut buffer)? else {
                break;
            };
            datagrams.push(ReceivedDatagram {
                payload: buffer[..length].to_vec(),
                source,
                peer_scope: None,
            });
        }

        Ok(datagrams)
    }

    fn try_receive_datagrams(
        &self,
        service_index: usize,
        max_datagrams: usize,
    ) -> Result<Vec<ReceivedDatagram>, DataplaneError> {
        let mut datagrams = Vec::with_capacity(max_datagrams);
        self.services[service_index].drain_datagrams(max_datagrams, |payload, source| {
            datagrams.push(ReceivedDatagram {
                payload: payload.to_vec(),
                source,
                peer_scope: None,
            });
            Ok::<(), DataplaneError>(())
        })??;

        if datagrams.len() >= max_datagrams {
            return Ok(datagrams);
        }

        let service_id = self.services[service_index].config().service_id();
        if let Some(target_datagrams) = self.burst_aggregation_target(service_id, &datagrams, max_datagrams) {
            let deadline = Instant::now() + BURST_AGGREGATION_MAX_DELAY;
            while datagrams.len() < target_datagrams && Instant::now() < deadline {
                let before = datagrams.len();
                self.services[service_index].drain_datagrams(target_datagrams - before, |payload, source| {
                    datagrams.push(ReceivedDatagram {
                        payload: payload.to_vec(),
                        source,
                        peer_scope: None,
                    });
                    Ok::<(), DataplaneError>(())
                })??;
                if datagrams.len() == before {
                    std::hint::spin_loop();
                }
            }
        }

        let mut buffer = vec![0_u8; u16::MAX as usize];
        while datagrams.len() < max_datagrams {
            if let Some((peer_scope, length, source)) =
                self.services[service_index].try_recv_peer_scoped(&mut buffer)?
            {
                datagrams.push(ReceivedDatagram {
                    payload: buffer[..length].to_vec(),
                    source,
                    peer_scope: Some(peer_scope),
                });
                continue;
            }
            let Some((length, source)) = self.services[service_index].try_recv_from(&mut buffer)? else {
                break;
            };
            datagrams.push(ReceivedDatagram {
                payload: buffer[..length].to_vec(),
                source,
                peer_scope: None,
            });
        }
        Ok(datagrams)
    }

    fn try_receive_datagrams_with_byte_budget(
        &self,
        service_index: usize,
        max_datagrams: usize,
        max_payload_bytes: usize,
    ) -> Result<Vec<ReceivedDatagram>, DataplaneError> {
        let mut datagrams = Vec::with_capacity(max_datagrams);
        let mut payload_bytes = 0_usize;
        let mut buffer = vec![0_u8; u16::MAX as usize];
        while datagrams.len() < max_datagrams {
            if let Some((peer_scope, length, source)) =
                self.services[service_index].try_recv_peer_scoped(&mut buffer)?
            {
                payload_bytes = payload_bytes.saturating_add(length);
                datagrams.push(ReceivedDatagram {
                    payload: buffer[..length].to_vec(),
                    source,
                    peer_scope: Some(peer_scope),
                });
                if payload_bytes >= max_payload_bytes {
                    break;
                }
                continue;
            }
            let Some((length, source)) = self.services[service_index].try_recv_from(&mut buffer)? else {
                break;
            };
            payload_bytes = payload_bytes.saturating_add(length);
            datagrams.push(ReceivedDatagram {
                payload: buffer[..length].to_vec(),
                source,
                peer_scope: None,
            });
            if payload_bytes >= max_payload_bytes {
                break;
            }
        }
        Ok(datagrams)
    }

    fn burst_aggregation_target(
        &self,
        service_id: ServiceId,
        datagrams: &[ReceivedDatagram],
        max_datagrams: usize,
    ) -> Option<usize> {
        if datagrams.len() < BURST_BATCH_MIN_DATAGRAMS || datagrams.len() >= max_datagrams {
            return None;
        }
        let first = datagrams.first()?;
        if first.peer_scope.is_some() {
            return None;
        }
        let fanout = self
            .service_schedulers
            .get(&service_id)
            .cloned()
            .unwrap_or_else(ServiceSchedulerConfig::normal)
            .effective_fanout(first.payload.len());
        if fanout != 1 {
            return None;
        }
        let target = self
            .paths
            .iter()
            .map(|path| max_batch_payloads_for_path(path, first.payload.len()))
            .max()
            .unwrap_or(1)
            .min(max_datagrams);
        if target > datagrams.len() {
            Some(target)
        } else {
            None
        }
    }

    fn forward_datagrams(
        &mut self,
        service_index: usize,
        service_name: &str,
        datagrams: Vec<ReceivedDatagram>,
    ) -> Result<Vec<ForwardOutcome>, DataplaneError> {
        let service_id = self.services[service_index].config().service_id();
        self.ensure_service_enabled(service_id)?;
        self.learn_local_sources(service_id, service_index, &datagrams);
        let planned = self.plan_datagrams(service_id, datagrams)?;
        let frames = self.coalesce_planned_frames(service_id, planned)?;
        if !self.path_transports.is_empty() {
            return self.transmit_planned_frames(service_name, frames);
        }
        let mut outcomes = Vec::new();
        let mut fragments = FragmentReassembly::default();

        for plan in frames {
            let encoded = plan.frame.encode()?;
            if encoded.len() > plan.path.mtu() {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id: plan.path.path_id(),
                    frame_len: encoded.len(),
                    mtu: plan.path.mtu(),
                });
            }

            let decoded = Frame::decode(&encoded)?;
            match decoded.kind {
                FrameKind::Data => {
                    let Some(payload) = fragments.push_or_payload(&decoded)? else {
                        continue;
                    };
                    let emitted = self.services[service_index].emit_to_target(&payload)?;
                    let source = plan.datagrams[0].source;
                    outcomes.push(ForwardOutcome {
                        service: service_name.to_owned(),
                        source,
                        target: self.services[service_index].config().target(),
                        payload_len: emitted,
                        sequence: plan.datagrams[0].sequence,
                        path_id: decoded.path_id,
                        frame_count: plan.fragment_count,
                        batch_count: 0,
                        fragment_count: plan.fragment_count.saturating_sub(1),
                    });
                    if let Some(outcome) = outcomes.last() {
                        self.metrics.record_forward(outcome);
                    }
                }
                FrameKind::Batch => {
                    let payloads = decoded.batch_payloads()?;
                    if payloads.len() != plan.datagrams.len() {
                        return Err(DataplaneError::BatchDatagramMismatch);
                    }

                    for (index, payload) in payloads.iter().enumerate() {
                        let emitted = self.services[service_index].emit_to_target(payload)?;
                        outcomes.push(ForwardOutcome {
                            service: service_name.to_owned(),
                            source: plan.datagrams[index].source,
                            target: self.services[service_index].config().target(),
                            payload_len: emitted,
                            sequence: plan.datagrams[index].sequence,
                            path_id: decoded.path_id,
                            frame_count: 1,
                            batch_count: payloads.len(),
                            fragment_count: 0,
                        });
                        if let Some(outcome) = outcomes.last() {
                            self.metrics.record_forward(outcome);
                        }
                    }
                }
                FrameKind::Control => return Err(DataplaneError::UnexpectedFrameKind),
            }
        }

        Ok(outcomes)
    }

    fn forward_datagrams_summary(
        &mut self,
        service_index: usize,
        service_name: &str,
        datagrams: Vec<ReceivedDatagram>,
    ) -> Result<PacketBatchSummary, DataplaneError> {
        if self.path_transports.is_empty() {
            let outcomes = self.forward_datagrams(service_index, service_name, datagrams)?;
            return Ok(PacketBatchSummary::from_forward_outcomes(&outcomes));
        }
        let service_id = self.services[service_index].config().service_id();
        self.ensure_service_enabled(service_id)?;
        self.learn_local_sources(service_id, service_index, &datagrams);
        let planned = self.plan_datagrams(service_id, datagrams)?;
        let frames = self.coalesce_planned_frames(service_id, planned)?;
        self.transmit_planned_frames_summary(service_name, frames)
    }

    fn learn_local_sources(&mut self, service_id: ServiceId, service_index: usize, datagrams: &[ReceivedDatagram]) {
        if self.services[service_index].config().return_mode() != ServiceReturnMode::LearnedSingleSource {
            return;
        }
        if let Some(last) = datagrams.last() {
            self.learned_sources.insert(service_id, last.source);
        }
    }

    fn transmit_planned_frames(
        &mut self,
        service_name: &str,
        frames: Vec<FramePlan>,
    ) -> Result<Vec<ForwardOutcome>, DataplaneError> {
        let mut outcomes = Vec::new();
        for plan in frames {
            let encoded = plan.frame.encode()?;
            if encoded.len() > plan.path.mtu() {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id: plan.path.path_id(),
                    frame_len: encoded.len(),
                    mtu: plan.path.mtu(),
                });
            }
            let expected_fanout = plan.datagrams.iter().any(|datagram| datagram.expected_fanout);
            let peer_scope = plan.datagrams.iter().find_map(|datagram| datagram.peer_scope);
            if self.try_send_path_frame(
                plan.path.path_id(),
                &plan.frame,
                peer_scope,
                encoded.len(),
                expected_fanout,
            )? {
                for datagram in &plan.datagrams {
                    let target = self
                        .send_target_for_path(plan.path.path_id(), datagram.peer_scope)
                        .or_else(|| plan.path.transport_remote())
                        .ok_or(DataplaneError::NoPathAvailable)?;
                    let outcome = ForwardOutcome {
                        service: service_name.to_owned(),
                        source: datagram.source,
                        target,
                        payload_len: datagram.payload_len,
                        sequence: datagram.sequence,
                        path_id: plan.path.path_id(),
                        frame_count: plan.fragment_count,
                        batch_count: if plan.datagrams.len() > 1 {
                            plan.datagrams.len()
                        } else {
                            0
                        },
                        fragment_count: plan.fragment_count.saturating_sub(1),
                    };
                    self.metrics.record_forward(&outcome);
                    self.metrics.record_transmit_timing_sample(
                        plan.path.path_id(),
                        datagram.sequence,
                        1,
                        datagram.peer_scope,
                    );
                    outcomes.push(outcome);
                }
            }
        }
        Ok(outcomes)
    }

    fn transmit_planned_frames_summary(
        &mut self,
        service_name: &str,
        frames: Vec<FramePlan>,
    ) -> Result<PacketBatchSummary, DataplaneError> {
        let mut summary = PacketBatchSummary::default();
        let mut groups: Vec<PreparedFrameGroup> = Vec::new();
        for plan in frames {
            let expected_fanout = plan.datagrams.iter().any(|datagram| datagram.expected_fanout);
            let peer_scope = plan.datagrams.iter().find_map(|datagram| datagram.peer_scope);
            let frame_len = self.transport_security.path_frame_len(&plan.frame);
            if frame_len > plan.path.mtu() {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id: plan.path.path_id(),
                    frame_len,
                    mtu: plan.path.mtu(),
                });
            }
            let session_key =
                peer_scope.or_else(|| self.transport_security.session_key_for_service(plan.frame.service_id));
            let path_id = plan.path.path_id();
            let remote = session_key
                .and_then(|key| self.learned_path_remotes.get(&(key, path_id)).copied())
                .or_else(|| plan.path.transport_remote())
                .ok_or(DataplaneError::NoPathAvailable)?;
            let item = PreparedFramePlan {
                frame: plan.frame,
                frame_len,
                expected_fanout,
                datagram_count: plan.datagrams.len(),
                payload_bytes: plan.datagrams.iter().map(|datagram| datagram.payload_len).sum(),
                first_sequence: plan.datagrams.first().map(|datagram| datagram.sequence),
                peer_scope,
            };
            if let Some(group) = groups.last_mut() {
                if group.path_id == path_id && group.remote == remote && group.session_key == session_key {
                    group.items.push(item);
                    continue;
                }
            }
            groups.push(PreparedFrameGroup {
                path_id,
                remote,
                service_id: item.frame.service_id,
                session_key,
                items: vec![item],
            });
        }

        let mut protected_groups = Vec::with_capacity(groups.len());
        for group in groups {
            let protection = self
                .transport_security
                .reserve_frame_protection_batch(group.service_id, group.session_key, group.items.len())
                .map_err(DataplaneError::Crypto)?;
            protected_groups.push(ProtectedFrameGroupInput { group, protection });
        }
        let protected = protect_prepared_frame_groups(protected_groups)?;

        for group in &protected {
            let packets = group
                .items
                .iter()
                .map(|plan| plan.packet.as_slice())
                .collect::<Vec<_>>();
            let sent = match self
                .path_transports
                .send_frames_to(group.path_id, &packets, group.remote)
            {
                Ok(sent) => sent,
                Err(UdpServiceError::SendFailed(_error)) => 0,
                Err(error) => return Err(DataplaneError::from(error)),
            };

            self.record_protected_group_send(service_name, group, sent, &mut summary);
        }
        Ok(summary)
    }

    fn record_protected_group_send(
        &mut self,
        service_name: &str,
        group: &ProtectedFrameGroup,
        sent: usize,
        summary: &mut PacketBatchSummary,
    ) {
        for frame in &group.items[..sent] {
            self.metrics.record_forward_batch(
                service_name,
                group.path_id,
                frame.datagram_count as u64,
                frame.payload_bytes as u64,
            );
            if let Some(first_sequence) = frame.first_sequence {
                self.metrics.record_transmit_timing_sample(
                    group.path_id,
                    first_sequence,
                    frame.datagram_count,
                    frame.peer_scope,
                );
            }
            summary.record_many(frame.datagram_count, frame.payload_bytes);
        }
        for frame in &group.items[sent..] {
            self.metrics
                .record_send_failed(group.path_id, frame.frame_len, frame.expected_fanout);
        }
    }

    fn try_send_path_frame(
        &mut self,
        path_id: u16,
        frame: &Frame,
        peer_scope: Option<u32>,
        frame_len: usize,
        expected_fanout: bool,
    ) -> Result<bool, DataplaneError> {
        let packet = self
            .transport_security
            .protect_frame_for_service_or_session(frame.service_id, peer_scope, frame)
            .map_err(DataplaneError::Crypto)?;
        let session_key = peer_scope.or_else(|| self.transport_security.session_key_for_service(frame.service_id));
        let learned_remote = session_key.and_then(|key| self.learned_path_remotes.get(&(key, path_id)).copied());
        let sent = if let Some(remote) = learned_remote {
            self.path_transports.send_frame_to(path_id, &packet, remote)
        } else {
            self.path_transports.send_frame(path_id, &packet)
        };
        match sent {
            Ok(_sent) => Ok(true),
            Err(UdpServiceError::SendFailed(_error)) => {
                self.metrics.record_send_failed(path_id, frame_len, expected_fanout);
                Ok(false)
            }
            Err(error) => Err(DataplaneError::from(error)),
        }
    }

    fn send_target_for_path(&self, path_id: u16, peer_scope: Option<u32>) -> Option<SocketAddr> {
        peer_scope.and_then(|scope| self.learned_path_remotes.get(&(scope, path_id)).copied())
    }

    fn emit_remote_payload(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload: &[u8],
        peer_scope: Option<u32>,
        observation: SequenceObservation,
    ) -> Result<RemoteDeliverOutcome, DataplaneError> {
        let service_index = self
            .services
            .iter()
            .position(|service| service.config().service_id() == service_id)
            .ok_or(DataplaneError::UnknownServiceId(service_id))?;
        self.ensure_service_enabled(service_id)?;
        let service_name = self.services[service_index].config().name().to_owned();
        let return_mode = self.services[service_index].config().return_mode();
        let target = match return_mode {
            ServiceReturnMode::Fixed | ServiceReturnMode::PeerScopedSource => {
                self.services[service_index].config().target()
            }
            ServiceReturnMode::LearnedSingleSource => self
                .learned_sources
                .get(&service_id)
                .copied()
                .unwrap_or_else(|| self.services[service_index].config().target()),
        };
        let emitted = match (return_mode, peer_scope) {
            (ServiceReturnMode::PeerScopedSource, Some(peer_scope)) => {
                self.services[service_index]
                    .emit_to_from_peer_source(peer_scope, payload, target)?
                    .0
            }
            _ => self.services[service_index].emit_to(payload, target)?,
        };
        self.metrics
            .record_receive_for_service(&service_name, service_id, path_id, sequence, emitted, observation);
        Ok(RemoteDeliverOutcome {
            service: service_name,
            target,
            payload_len: emitted,
            sequence,
            path_id,
        })
    }

    fn emit_remote_payload_summary(
        &mut self,
        service_id: ServiceId,
        path_id: u16,
        sequence: u64,
        payload: &[u8],
        peer_scope: Option<u32>,
        observation: SequenceObservation,
    ) -> Result<usize, DataplaneError> {
        let service_index = self
            .services
            .iter()
            .position(|service| service.config().service_id() == service_id)
            .ok_or(DataplaneError::UnknownServiceId(service_id))?;
        let service_name = self.services[service_index].config().name().to_owned();
        let return_mode = self.services[service_index].config().return_mode();
        let target = match return_mode {
            ServiceReturnMode::Fixed | ServiceReturnMode::PeerScopedSource => {
                self.services[service_index].config().target()
            }
            ServiceReturnMode::LearnedSingleSource => self
                .learned_sources
                .get(&service_id)
                .copied()
                .unwrap_or_else(|| self.services[service_index].config().target()),
        };
        let emitted = match (return_mode, peer_scope) {
            (ServiceReturnMode::PeerScopedSource, Some(peer_scope)) => {
                self.services[service_index]
                    .emit_to_from_peer_source(peer_scope, payload, target)?
                    .0
            }
            _ => self.services[service_index].emit_to(payload, target)?,
        };
        self.metrics
            .record_receive_for_service(&service_name, service_id, path_id, sequence, emitted, observation);
        Ok(emitted)
    }

    fn plan_datagrams(
        &mut self,
        service_id: ServiceId,
        datagrams: Vec<ReceivedDatagram>,
    ) -> Result<Vec<PlannedDatagram>, DataplaneError> {
        let mut planned = Vec::with_capacity(datagrams.len());
        let burst_batching = datagrams.len() >= BURST_BATCH_MIN_DATAGRAMS;
        let mut current_batch_path: Option<CorePathConfig> = None;
        let mut current_batch_len = 0_usize;
        let mut current_path_run_len = 0_usize;
        for datagram in datagrams {
            let sequence = self.next_sequence_for(datagram.peer_scope);
            let paths = if let Some(peer_scope) = datagram.peer_scope {
                current_batch_path = None;
                current_batch_len = 0;
                current_path_run_len = 0;
                self.select_peer_scoped_paths(service_id, peer_scope, datagram.payload.len())?
            } else if burst_batching {
                self.select_service_paths_for_burst_batch(
                    service_id,
                    datagram.payload.len(),
                    datagram.source,
                    &mut current_batch_path,
                    &mut current_batch_len,
                    &mut current_path_run_len,
                )?
            } else {
                self.select_service_paths(service_id, datagram.payload.len(), Some(datagram.source))?
            };
            let expected_fanout = paths.len() > 1;
            let fragment = if paths
                .first()
                .is_some_and(|path| datagram.payload.len() > path.max_data_payload())
            {
                let datagram_id = self.next_datagram_id;
                self.next_datagram_id = self.next_datagram_id.wrapping_add(1).max(1);
                Some(datagram_id)
            } else {
                None
            };
            if burst_batching && datagram.peer_scope.is_none() {
                if expected_fanout || fragment.is_some() {
                    current_batch_path = None;
                    current_batch_len = 0;
                    current_path_run_len = 0;
                } else if let Some(path) = paths.first() {
                    if current_batch_path
                        .as_ref()
                        .is_some_and(|current| current.path_id() == path.path_id())
                    {
                        current_path_run_len += 1;
                        current_batch_len = if can_extend_batch(path, current_batch_len, datagram.payload.len()) {
                            next_batch_len(current_batch_len, datagram.payload.len())
                        } else {
                            next_batch_len(0, datagram.payload.len())
                        };
                    } else {
                        current_batch_path = Some(path.clone());
                        current_path_run_len = 1;
                        current_batch_len = next_batch_len(0, datagram.payload.len());
                    };
                }
            }
            let path_count = paths.len();
            let mut payload = Some(datagram.payload);
            for (index, path) in paths.into_iter().enumerate() {
                let payload = if index + 1 == path_count {
                    payload
                        .take()
                        .expect("last planned path must take the original datagram payload")
                } else {
                    payload
                        .as_ref()
                        .expect("fanout planning must keep the original payload until the last path")
                        .clone()
                };
                planned.push(PlannedDatagram {
                    payload,
                    source: datagram.source,
                    peer_scope: datagram.peer_scope,
                    sequence,
                    service_id,
                    path,
                    fragment,
                    expected_fanout,
                });
            }
        }
        Ok(planned)
    }

    fn select_service_paths_for_burst_batch(
        &mut self,
        service_id: ServiceId,
        payload_len: usize,
        source: SocketAddr,
        current_batch_path: &mut Option<CorePathConfig>,
        current_batch_len: &mut usize,
        current_path_run_len: &mut usize,
    ) -> Result<Vec<CorePathConfig>, DataplaneError> {
        let scheduler = self
            .service_schedulers
            .get(&service_id)
            .cloned()
            .unwrap_or_else(ServiceSchedulerConfig::normal);
        let max_path_run = if scheduler.path_run_datagrams() == 0 {
            BURST_PATH_SEND_RUN_DATAGRAMS
        } else {
            scheduler.path_run_datagrams().max(BURST_BATCH_MIN_DATAGRAMS)
        };
        let allow_burst_reuse = !self.scheduler.requires_per_datagram_decisions();
        let allow_noncoalesced_burst_reuse = self.scheduler.allows_noncoalesced_burst_reuse();
        if let Some(path) = current_batch_path.as_ref() {
            if allow_burst_reuse
                && (can_extend_batch(path, *current_batch_len, payload_len)
                    || (allow_noncoalesced_burst_reuse
                        && can_reuse_burst_path_without_coalescing(
                            path,
                            payload_len,
                            *current_path_run_len,
                            max_path_run,
                        )))
            {
                return Ok(vec![path.clone()]);
            }
        }

        *current_batch_path = None;
        *current_batch_len = 0;
        *current_path_run_len = 0;
        self.select_service_paths(service_id, payload_len, Some(source))
    }

    fn next_sequence_for(&mut self, peer_scope: Option<u32>) -> u64 {
        let sequence = self.next_sequences.entry(peer_scope).or_insert(1);
        let current = *sequence;
        *sequence = current.wrapping_add(1).max(1);
        current
    }

    fn plan_injected_datagram(
        &mut self,
        service_id: ServiceId,
        payload: Vec<u8>,
    ) -> Result<Vec<PlannedDatagram>, DataplaneError> {
        let source = "127.0.0.1:0"
            .parse()
            .expect("injected service source placeholder must parse");
        self.plan_datagrams(
            service_id,
            vec![ReceivedDatagram {
                payload,
                source,
                peer_scope: None,
            }],
        )
    }

    fn select_path(&mut self, payload_len: usize) -> Result<&CorePathConfig, DataplaneError> {
        let path_index = self
            .scheduler
            .select_path_index(&self.paths, payload_len)
            .ok_or(DataplaneError::NoPathAvailable)?;
        Ok(&self.paths[path_index])
    }

    fn select_path_for_service(
        &mut self,
        service_id: ServiceId,
        payload_len: usize,
        scheduler: &ServiceSchedulerConfig,
    ) -> Result<CorePathConfig, DataplaneError> {
        match scheduler.path_policy() {
            ServicePathPolicy::Inherit if scheduler.allowed_path_ids().is_empty() => {
                Ok(self.select_path(payload_len)?.clone())
            }
            ServicePathPolicy::Inherit => self.select_single_best_service_path(payload_len, scheduler),
            ServicePathPolicy::SingleBestPath => self.select_single_best_service_path(payload_len, scheduler),
            ServicePathPolicy::WeightedRoundRobin => {
                self.select_weighted_service_path(service_id, payload_len, scheduler)
            }
        }
    }

    fn select_single_best_service_path(
        &self,
        payload_len: usize,
        scheduler: &ServiceSchedulerConfig,
    ) -> Result<CorePathConfig, DataplaneError> {
        self.paths
            .iter()
            .filter(|path| {
                scheduler.allows_path_id(path.path_id())
                    && path.accepts_whole_packet()
                    && payload_len <= path.max_data_payload()
            })
            .min_by_key(|path| {
                (
                    std::cmp::Reverse(path.primitives().tx_capacity_bps().unwrap_or(0)),
                    path.primitives().latency_us().unwrap_or(u32::MAX),
                    path.primitives().loss_ppm(),
                    path.path_id(),
                )
            })
            .or_else(|| {
                self.paths
                    .iter()
                    .filter(|path| scheduler.allows_path_id(path.path_id()) && path.accepts_fragmented_packet())
                    .min_by_key(|path| {
                        (
                            std::cmp::Reverse(path.primitives().tx_capacity_bps().unwrap_or(0)),
                            path.primitives().latency_us().unwrap_or(u32::MAX),
                            path.primitives().loss_ppm(),
                            path.path_id(),
                        )
                    })
            })
            .cloned()
            .ok_or(DataplaneError::NoPathAvailable)
    }

    fn select_weighted_service_path(
        &mut self,
        service_id: ServiceId,
        payload_len: usize,
        scheduler: &ServiceSchedulerConfig,
    ) -> Result<CorePathConfig, DataplaneError> {
        let scheduler = self.service_weighted_schedulers.entry(service_id).or_insert_with(|| {
            WeightedRoundRobin::compile_with_path_weights(
                &self.paths,
                scheduler.allowed_path_ids(),
                scheduler.path_weights(),
            )
        });
        let path_index = scheduler
            .select_path_index(&self.paths, payload_len)
            .ok_or(DataplaneError::NoPathAvailable)?;
        Ok(self.paths[path_index].clone())
    }

    fn select_weighted_service_path_where(
        &mut self,
        service_id: ServiceId,
        payload_len: usize,
        scheduler: &ServiceSchedulerConfig,
        allowed: impl Fn(&CorePathConfig) -> bool,
    ) -> Result<CorePathConfig, DataplaneError> {
        let scheduler = self.service_weighted_schedulers.entry(service_id).or_insert_with(|| {
            WeightedRoundRobin::compile_with_path_weights(
                &self.paths,
                scheduler.allowed_path_ids(),
                scheduler.path_weights(),
            )
        });
        let path_index = scheduler
            .select_path_index_where(&self.paths, payload_len, allowed)
            .ok_or(DataplaneError::NoPathAvailable)?;
        Ok(self.paths[path_index].clone())
    }

    fn select_service_paths(
        &mut self,
        service_id: ServiceId,
        payload_len: usize,
        source: Option<SocketAddr>,
    ) -> Result<Vec<CorePathConfig>, DataplaneError> {
        let scheduler = self
            .service_schedulers
            .get(&service_id)
            .cloned()
            .unwrap_or_else(ServiceSchedulerConfig::normal);
        let fanout = scheduler.effective_fanout(payload_len);
        if fanout == 1 {
            if let Some(source) = source {
                return Ok(vec![self.select_service_flowlet_path(
                    service_id,
                    source,
                    payload_len,
                    scheduler.clone(),
                )?]);
            }
            return Ok(vec![self.select_path_for_service(
                service_id,
                payload_len,
                &scheduler,
            )?]);
        }
        let mut path_indices = AllPathSelector::select_path_indices(&self.paths, payload_len)
            .into_iter()
            .filter(|index| scheduler.allows_path_id(self.paths[*index].path_id()))
            .collect::<Vec<_>>();
        if fanout > 0 {
            path_indices.truncate(usize::from(fanout));
        }
        if path_indices.is_empty() {
            return Err(DataplaneError::NoPathAvailable);
        }
        Ok(path_indices
            .into_iter()
            .map(|index| self.paths[index].clone())
            .collect())
    }

    fn select_service_flowlet_path(
        &mut self,
        service_id: ServiceId,
        source: SocketAddr,
        payload_len: usize,
        scheduler: ServiceSchedulerConfig,
    ) -> Result<CorePathConfig, DataplaneError> {
        let now = Instant::now();
        let key = ServiceFlowletKey { service_id, source };
        if scheduler.flowlet_idle_us() > 0 {
            if let Some(flowlet) = self.service_flowlets.get_mut(&key) {
                if now.duration_since(flowlet.last_used) < Duration::from_micros(scheduler.flowlet_idle_us()) {
                    let within_max_hold = scheduler.flowlet_max_hold_us() == 0
                        || now.duration_since(flowlet.started_at)
                            < Duration::from_micros(scheduler.flowlet_max_hold_us());
                    if within_max_hold {
                        if let Some(path) = self.paths.iter().find(|path| {
                            path.path_id() == flowlet.path_id
                                && path.accepts_whole_packet()
                                && payload_len <= path.max_data_payload()
                        }) {
                            flowlet.last_used = now;
                            return Ok(path.clone());
                        }
                    }
                }
            }
        }

        let path = self.select_path_for_service(service_id, payload_len, &scheduler)?;
        if scheduler.flowlet_idle_us() > 0 {
            self.service_flowlets.insert(
                key,
                ServiceFlowletState {
                    path_id: path.path_id(),
                    started_at: now,
                    last_used: now,
                },
            );
        }
        Ok(path)
    }

    fn select_peer_scoped_paths(
        &mut self,
        service_id: ServiceId,
        peer_scope: u32,
        payload_len: usize,
    ) -> Result<Vec<CorePathConfig>, DataplaneError> {
        let scheduler = self
            .service_schedulers
            .get(&service_id)
            .cloned()
            .unwrap_or_else(ServiceSchedulerConfig::normal);
        let peer_remote_path_ids = self
            .paths
            .iter()
            .filter(|path| self.learned_path_remotes.contains_key(&(peer_scope, path.path_id())))
            .map(CorePathConfig::path_id)
            .collect::<Vec<_>>();
        let path_has_peer_remote = |path: &CorePathConfig| peer_remote_path_ids.contains(&path.path_id());
        let eligible: Vec<CorePathConfig> = self
            .paths
            .iter()
            .filter(|path| path_has_peer_remote(path) && scheduler.allows_path_id(path.path_id()))
            .cloned()
            .collect();
        if eligible.is_empty() {
            return Err(DataplaneError::NoPathAvailable);
        }
        let selected = match scheduler.path_policy() {
            ServicePathPolicy::WeightedRoundRobin => {
                self.select_weighted_service_path_where(service_id, payload_len, &scheduler, path_has_peer_remote)?
            }
            ServicePathPolicy::SingleBestPath | ServicePathPolicy::Inherit => eligible
                .iter()
                .filter(|path| path.accepts_whole_packet() && payload_len <= path.max_data_payload())
                .min_by_key(|path| {
                    (
                        std::cmp::Reverse(path.primitives().tx_capacity_bps().unwrap_or(0)),
                        path.primitives().latency_us().unwrap_or(u32::MAX),
                        path.primitives().loss_ppm(),
                        path.path_id(),
                    )
                })
                .or_else(|| eligible.iter().find(|path| path.accepts_fragmented_packet()))
                .cloned()
                .ok_or(DataplaneError::NoPathAvailable)?,
        };
        Ok(vec![selected])
    }

    fn coalesce_planned_frames(
        &self,
        service_id: ServiceId,
        planned: Vec<PlannedDatagram>,
    ) -> Result<Vec<FramePlan>, DataplaneError> {
        let mut frames = Vec::new();
        let mut planned = planned.into_iter().peekable();
        while let Some(current) = planned.next() {
            if current.fragment.is_some() {
                frames.extend(fragment_datagram(&current)?);
                continue;
            }

            let path = current.path.clone();
            let path_id = current.path.path_id();
            let sequence = current.sequence;
            let mut batch_len = V1_HEADER_LEN + 2 + 2 + current.payload.len();
            let mut batch_datagrams = vec![current.to_meta()];
            let mut batch_payloads = vec![current.payload];
            while let Some(candidate) = planned.peek() {
                if candidate.fragment.is_some()
                    || candidate.path.path_id() != path_id
                    || candidate.peer_scope != current.peer_scope
                {
                    break;
                }
                let candidate_len = 2 + candidate.payload.len();
                if batch_len + candidate_len > path.mtu() {
                    break;
                }
                let candidate = planned.next().expect("peek confirmed a planned candidate is available");
                batch_len += candidate_len;
                batch_datagrams.push(candidate.to_meta());
                batch_payloads.push(candidate.payload);
            }

            let frame = if batch_payloads.len() > 1 {
                Frame::batch(service_id, path_id, sequence, &batch_payloads)?
            } else {
                Frame::data(
                    service_id,
                    path_id,
                    sequence,
                    batch_payloads
                        .pop()
                        .expect("single-frame coalescing must retain one payload"),
                )?
            };
            frames.push(FramePlan {
                frame,
                path,
                datagrams: batch_datagrams,
                fragment_count: 1,
            });
        }
        Ok(frames)
    }

    fn ensure_service_enabled(&self, service_id: ServiceId) -> Result<(), DataplaneError> {
        if let Some(reason) = self.disabled_services.get(&service_id) {
            return Err(DataplaneError::ServiceDisabled {
                service_id,
                reason: reason.clone(),
            });
        }
        Ok(())
    }
}

fn service_weighted_schedulers(
    services: &[crate::udp_service::UdpServiceConfig],
    paths: &[CorePathConfig],
) -> HashMap<ServiceId, WeightedRoundRobin> {
    services
        .iter()
        .filter(|service| service.scheduler().path_policy() == ServicePathPolicy::WeightedRoundRobin)
        .map(|service| {
            (
                service.service_id(),
                WeightedRoundRobin::compile_with_path_weights(
                    paths,
                    service.scheduler().allowed_path_ids(),
                    service.scheduler().path_weights(),
                ),
            )
        })
        .collect()
}

fn service_weighted_schedulers_from_primitives(
    service_schedulers: &HashMap<ServiceId, ServiceSchedulerConfig>,
    paths: &[CorePathConfig],
) -> HashMap<ServiceId, WeightedRoundRobin> {
    service_schedulers
        .iter()
        .filter(|(_service_id, scheduler)| scheduler.path_policy() == ServicePathPolicy::WeightedRoundRobin)
        .map(|(service_id, scheduler)| {
            (
                *service_id,
                WeightedRoundRobin::compile_with_path_weights(
                    paths,
                    scheduler.allowed_path_ids(),
                    scheduler.path_weights(),
                ),
            )
        })
        .collect()
}

fn next_batch_len(current_len: usize, payload_len: usize) -> usize {
    if current_len == 0 {
        V1_HEADER_LEN + 2 + 2 + payload_len
    } else {
        current_len + 2 + payload_len
    }
}

fn can_extend_batch(path: &CorePathConfig, current_len: usize, payload_len: usize) -> bool {
    path.accepts_whole_packet()
        && payload_len <= path.max_data_payload()
        && next_batch_len(current_len, payload_len) <= path.mtu()
}

fn can_reuse_burst_path_without_coalescing(
    path: &CorePathConfig,
    payload_len: usize,
    current_path_run_len: usize,
    max_path_run: usize,
) -> bool {
    path.accepts_whole_packet() && payload_len <= path.max_data_payload() && current_path_run_len < max_path_run
}

fn max_batch_payloads_for_path(path: &CorePathConfig, payload_len: usize) -> usize {
    let mut count = 0_usize;
    let mut batch_len = 0_usize;
    while can_extend_batch(path, batch_len, payload_len) {
        batch_len = next_batch_len(batch_len, payload_len);
        count += 1;
    }
    count.max(1)
}

#[derive(Debug, Clone)]
struct ReceivedDatagram {
    payload: Vec<u8>,
    source: SocketAddr,
    peer_scope: Option<u32>,
}

#[derive(Debug, Clone)]
pub(crate) struct PlannedDatagram {
    pub(crate) payload: Vec<u8>,
    pub(crate) source: SocketAddr,
    pub(crate) peer_scope: Option<u32>,
    pub(crate) sequence: u64,
    pub(crate) service_id: ServiceId,
    pub(crate) path: CorePathConfig,
    pub(crate) fragment: Option<u32>,
    pub(crate) expected_fanout: bool,
}

impl PlannedDatagram {
    pub(crate) fn to_meta(&self) -> PlannedDatagramMeta {
        PlannedDatagramMeta {
            source: self.source,
            peer_scope: self.peer_scope,
            sequence: self.sequence,
            expected_fanout: self.expected_fanout,
            payload_len: self.payload.len(),
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct PlannedDatagramMeta {
    pub(crate) source: SocketAddr,
    pub(crate) peer_scope: Option<u32>,
    pub(crate) sequence: u64,
    pub(crate) expected_fanout: bool,
    pub(crate) payload_len: usize,
}

#[derive(Debug)]
pub(crate) struct FramePlan {
    pub(crate) frame: Frame,
    pub(crate) path: CorePathConfig,
    pub(crate) datagrams: Vec<PlannedDatagramMeta>,
    pub(crate) fragment_count: usize,
}

#[derive(Debug)]
struct PreparedFramePlan {
    frame: Frame,
    frame_len: usize,
    expected_fanout: bool,
    datagram_count: usize,
    payload_bytes: usize,
    first_sequence: Option<u64>,
    peer_scope: Option<u32>,
}

#[derive(Debug)]
struct PreparedFrameGroup {
    path_id: u16,
    remote: SocketAddr,
    service_id: ServiceId,
    session_key: Option<u32>,
    items: Vec<PreparedFramePlan>,
}

struct ProtectedFrameGroupInput {
    group: PreparedFrameGroup,
    protection: ReservedFrameProtection,
}

#[derive(Debug)]
struct ProtectedFrameGroup {
    path_id: u16,
    remote: SocketAddr,
    items: Vec<PreparedFrameSend>,
}

#[derive(Debug)]
struct PreparedFrameSend {
    packet: Vec<u8>,
    frame_len: usize,
    expected_fanout: bool,
    datagram_count: usize,
    payload_bytes: usize,
    first_sequence: Option<u64>,
    peer_scope: Option<u32>,
}

fn protect_prepared_frame_groups(
    groups: Vec<ProtectedFrameGroupInput>,
) -> Result<Vec<ProtectedFrameGroup>, DataplaneError> {
    groups
        .into_iter()
        .map(protect_prepared_frame_group)
        .collect::<Result<Vec<_>, _>>()
}

fn protect_prepared_frame_group(input: ProtectedFrameGroupInput) -> Result<ProtectedFrameGroup, DataplaneError> {
    let items = protect_prepared_frame_items(input.group.items, input.protection)?;
    Ok(ProtectedFrameGroup {
        path_id: input.group.path_id,
        remote: input.group.remote,
        items,
    })
}

fn protect_prepared_frame_items(
    items: Vec<PreparedFramePlan>,
    protection: ReservedFrameProtection,
) -> Result<Vec<PreparedFrameSend>, DataplaneError> {
    if items.len() < PARALLEL_PROTECT_MIN_FRAMES {
        return protect_prepared_frame_item_chunk(items.into_iter().enumerate().collect(), protection);
    }

    let available_workers = std::thread::available_parallelism()
        .map(usize::from)
        .unwrap_or(1)
        .max(1);
    let worker_count = available_workers
        .min(items.len().div_ceil(PARALLEL_PROTECT_MIN_FRAMES))
        .max(1);
    if worker_count == 1 {
        return protect_prepared_frame_item_chunk(items.into_iter().enumerate().collect(), protection);
    }

    let chunk_size = items.len().div_ceil(worker_count);
    let mut chunks = Vec::with_capacity(worker_count);
    let mut current = Vec::with_capacity(chunk_size);
    for (index, item) in items.into_iter().enumerate() {
        current.push((index, item));
        if current.len() >= chunk_size {
            chunks.push(current);
            current = Vec::with_capacity(chunk_size);
        }
    }
    if !current.is_empty() {
        chunks.push(current);
    }

    std::thread::scope(|scope| {
        let handles = chunks
            .into_iter()
            .map(|chunk| {
                let protection = protection.clone();
                scope.spawn(move || protect_prepared_frame_item_chunk(chunk, protection))
            })
            .collect::<Vec<_>>();
        let mut protected = Vec::new();
        for handle in handles {
            let mut chunk = handle
                .join()
                .unwrap_or_else(|_panic| Err(DataplaneError::Crypto(CryptoError::InvalidInput)))?;
            protected.append(&mut chunk);
        }
        Ok(protected)
    })
}

fn protect_prepared_frame_item_chunk(
    items: Vec<(usize, PreparedFramePlan)>,
    protection: ReservedFrameProtection,
) -> Result<Vec<PreparedFrameSend>, DataplaneError> {
    let mut protected = Vec::with_capacity(items.len());
    for (index, item) in items {
        let packet = protection
            .protect_frame(index, &item.frame)
            .map_err(DataplaneError::Crypto)?;
        protected.push(PreparedFrameSend {
            packet,
            frame_len: item.frame_len,
            expected_fanout: item.expected_fanout,
            datagram_count: item.datagram_count,
            payload_bytes: item.payload_bytes,
            first_sequence: item.first_sequence,
            peer_scope: item.peer_scope,
        });
    }
    Ok(protected)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct PendingRemoteEmitKey {
    service_index: usize,
    service_id: ServiceId,
    path_id: u16,
    peer_scope: Option<u32>,
    target: SocketAddr,
}

#[derive(Debug)]
struct PendingRemoteEmit {
    payload: Vec<u8>,
    sequence: u64,
    observation: SequenceObservation,
}

#[derive(Debug, Default)]
struct PendingRemoteEmitBatch {
    key: Option<PendingRemoteEmitKey>,
    items: Vec<PendingRemoteEmit>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct RemoteReorderKey {
    service_id: ServiceId,
    peer_scope: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct ServiceFlowletKey {
    service_id: ServiceId,
    source: SocketAddr,
}

#[derive(Debug, Clone, Copy)]
struct ServiceFlowletState {
    path_id: u16,
    started_at: Instant,
    last_used: Instant,
}

#[derive(Debug)]
struct RemoteReorderEntry {
    path_id: u16,
    payload: Vec<u8>,
    observation: SequenceObservation,
    received_at: Instant,
}

#[derive(Debug)]
struct ReadyRemoteReorderEntry {
    sequence: u64,
    path_id: u16,
    payload: Vec<u8>,
    observation: SequenceObservation,
}

#[derive(Debug, Default)]
struct RemoteReorderBuffer {
    next_sequence: u64,
    entries: BTreeMap<u64, RemoteReorderEntry>,
}

impl RemoteReorderBuffer {
    fn new(next_sequence: u64) -> Self {
        Self {
            next_sequence,
            entries: BTreeMap::new(),
        }
    }
}

fn duration_us(duration: Duration) -> u64 {
    duration.as_micros().min(u128::from(u64::MAX)) as u64
}

/// Summary of one config reapply operation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ReapplyOutcome {
    pub unchanged: usize,
    pub updated: usize,
    pub rebound: usize,
    pub added: usize,
    pub removed: usize,
}

/// Observable result for one forwarded datagram.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForwardOutcome {
    pub service: String,
    pub source: SocketAddr,
    pub target: SocketAddr,
    pub payload_len: usize,
    pub sequence: u64,
    pub path_id: u16,
    pub frame_count: usize,
    pub batch_count: usize,
    pub fragment_count: usize,
}

/// Observable result for one remote frame delivered to a local UDP target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteDeliverOutcome {
    pub service: String,
    pub target: SocketAddr,
    pub payload_len: usize,
    pub sequence: u64,
    pub path_id: u16,
}

/// Aggregate packet counters for production runner hot-path drains.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PacketBatchSummary {
    pub packets: usize,
    pub bytes: usize,
}

impl PacketBatchSummary {
    fn record(&mut self, payload_len: usize) {
        self.packets += 1;
        self.bytes += payload_len;
    }

    fn record_many(&mut self, packets: usize, bytes: usize) {
        self.packets += packets;
        self.bytes += bytes;
    }

    fn from_forward_outcomes(outcomes: &[ForwardOutcome]) -> Self {
        let mut summary = Self::default();
        for outcome in outcomes {
            summary.record(outcome.payload_len);
        }
        summary
    }
}

/// Reserved Gatherlink service payload that Python must decode or reject.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReservedServiceEvent {
    pub service_id: ServiceId,
    pub path_id: u16,
    pub sequence: u64,
    pub payload: Vec<u8>,
    pub frame_bytes: usize,
    pub peer_scope: Option<u32>,
}

/// Planned duplicate control frame for one path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlTransmitPlan {
    pub path_id: u16,
    pub sequence: u64,
    pub frame_len: usize,
    pub payload_len: usize,
}

/// Dataplane execution errors.
#[derive(Debug)]
pub enum DataplaneError {
    UdpService(UdpServiceError),
    Protocol(ProtocolError),
    Crypto(CryptoError),
    UnknownService(String),
    UnknownServiceId(ServiceId),
    ServiceDisabled { service_id: ServiceId, reason: String },
    NoDatagramForwarded,
    NoPathAvailable,
    UnexpectedFrameKind,
    BatchDatagramMismatch,
    InvalidFragmentPlan,
    TooManyFragments(usize),
    FrameExceedsPathMtu { path_id: u16, frame_len: usize, mtu: usize },
}

impl fmt::Display for DataplaneError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UdpService(error) => write!(formatter, "{error}"),
            Self::Protocol(error) => write!(formatter, "{error}"),
            Self::Crypto(error) => write!(formatter, "{error}"),
            Self::UnknownService(name) => write!(formatter, "unknown UDP service: {name}"),
            Self::UnknownServiceId(service_id) => write!(formatter, "unknown UDP service id: {service_id}"),
            Self::ServiceDisabled { service_id, reason } => {
                write!(formatter, "service id {service_id} is disabled: {reason}")
            }
            Self::NoDatagramForwarded => write!(formatter, "no datagram was forwarded"),
            Self::NoPathAvailable => write!(formatter, "no path is available for packet framing"),
            Self::UnexpectedFrameKind => write!(formatter, "decoded frame was not a data frame"),
            Self::BatchDatagramMismatch => write!(formatter, "batch payload count did not match planned datagrams"),
            Self::InvalidFragmentPlan => write!(formatter, "internal fragment plan is invalid"),
            Self::TooManyFragments(count) => write!(formatter, "datagram requires too many fragments: {count}"),
            Self::FrameExceedsPathMtu {
                path_id,
                frame_len,
                mtu,
            } => write!(
                formatter,
                "encoded frame for path {path_id} is {frame_len} bytes, exceeding path MTU {mtu}",
            ),
        }
    }
}

impl std::error::Error for DataplaneError {}

impl From<UdpServiceError> for DataplaneError {
    fn from(error: UdpServiceError) -> Self {
        Self::UdpService(error)
    }
}

impl From<ProtocolError> for DataplaneError {
    fn from(error: ProtocolError) -> Self {
        Self::Protocol(error)
    }
}
