//! Core dataplane engine.
//!
//! The engine owns userland UDP service sockets and applies the Gatherlink
//! frame boundary before emitting virtual UDP payloads. Business policy stays in
//! Python; this module executes already-compiled runtime state.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::net::SocketAddr;

use gatherlink_crypto::errors::CryptoError;
use gatherlink_protocol::control::SequenceObservation;
use gatherlink_protocol::errors::ProtocolError;
use gatherlink_protocol::frame::{Frame, FrameKind, V1_HEADER_LEN};
use gatherlink_protocol::ids::{is_reserved_service_id, ServiceId};

use crate::dedupe::{DedupeObservation, DedupeWindow};
use crate::fragmentation::{fragment_datagram, FragmentReassembly};
use crate::metrics::{DataplaneMetrics, MetricsSnapshot};
use crate::runtime_config::{CorePathConfig, CoreRuntimeConfig, SchedulerConfig};
use crate::scheduler::all_paths::AllPathSelector;
use crate::scheduler::compiled::CompiledScheduler;
use crate::security::TransportSecurity;
use crate::sockets::PathTransportSet;
use crate::udp_service::{ServiceReturnMode, ServiceSchedulerConfig, UdpServiceError, UserlandUdpService};

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
    reserved_events: Vec<ReservedServiceEvent>,
    learned_sources: HashMap<ServiceId, SocketAddr>,
    learned_path_remotes: HashMap<(u32, u16), SocketAddr>,
    disabled_services: HashMap<ServiceId, String>,
    service_schedulers: HashMap<ServiceId, ServiceSchedulerConfig>,
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
            reserved_events: Vec::new(),
            learned_sources: HashMap::new(),
            learned_path_remotes: HashMap::new(),
            disabled_services: HashMap::new(),
            service_schedulers,
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
        self.metrics.snapshot()
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
    }

    /// Return actual local path transport socket addresses.
    pub fn path_transport_local_addrs(&self) -> Result<HashMap<u16, SocketAddr>, DataplaneError> {
        self.path_transports.local_addrs().map_err(DataplaneError::from)
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
            .copied()
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
                    payload_len: frame_plan.datagrams.iter().map(|datagram| datagram.payload.len()).sum(),
                });
            }
        }
        Ok(plans)
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
                    payload_len: frame_plan.datagrams.iter().map(|datagram| datagram.payload.len()).sum(),
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
            .copied()
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
            let mut buffer = vec![0_u8; u16::MAX as usize];
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
        for _ in 0..max_datagrams {
            let mut buffer = vec![0_u8; u16::MAX as usize];
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
                        payload_len: datagram.payload.len(),
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
                    outcomes.push(outcome);
                }
            }
        }
        Ok(outcomes)
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

    fn plan_datagrams(
        &mut self,
        service_id: ServiceId,
        datagrams: Vec<ReceivedDatagram>,
    ) -> Result<Vec<PlannedDatagram>, DataplaneError> {
        let mut planned = Vec::with_capacity(datagrams.len());
        for datagram in datagrams {
            let sequence = self.next_sequence_for(datagram.peer_scope);
            let paths = if let Some(peer_scope) = datagram.peer_scope {
                self.select_peer_scoped_paths(peer_scope, datagram.payload.len())?
            } else {
                self.select_service_paths(service_id, datagram.payload.len())?
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
            for path in paths {
                planned.push(PlannedDatagram {
                    payload: datagram.payload.clone(),
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

    fn select_service_paths(
        &mut self,
        service_id: ServiceId,
        payload_len: usize,
    ) -> Result<Vec<CorePathConfig>, DataplaneError> {
        let fanout = self
            .service_schedulers
            .get(&service_id)
            .copied()
            .unwrap_or_else(ServiceSchedulerConfig::normal)
            .effective_fanout(payload_len);
        if fanout == 1 {
            return Ok(vec![self.select_path(payload_len)?.clone()]);
        }
        let mut path_indices = AllPathSelector::select_path_indices(&self.paths, payload_len);
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

    fn select_peer_scoped_paths(
        &mut self,
        peer_scope: u32,
        payload_len: usize,
    ) -> Result<Vec<CorePathConfig>, DataplaneError> {
        let eligible: Vec<CorePathConfig> = self
            .paths
            .iter()
            .filter(|path| self.learned_path_remotes.contains_key(&(peer_scope, path.path_id())))
            .cloned()
            .collect();
        if eligible.is_empty() {
            return Err(DataplaneError::NoPathAvailable);
        }
        let selected = eligible
            .into_iter()
            .find(|path| path.accepts_whole_packet() && payload_len <= path.max_data_payload())
            .or_else(|| {
                self.paths
                    .iter()
                    .find(|path| self.learned_path_remotes.contains_key(&(peer_scope, path.path_id())))
                    .cloned()
            })
            .ok_or(DataplaneError::NoPathAvailable)?;
        Ok(vec![selected])
    }

    fn coalesce_planned_frames(
        &self,
        service_id: ServiceId,
        planned: Vec<PlannedDatagram>,
    ) -> Result<Vec<FramePlan>, DataplaneError> {
        let mut frames = Vec::new();
        let mut cursor = 0_usize;
        while cursor < planned.len() {
            let current = &planned[cursor];
            if current.fragment.is_some() {
                frames.extend(fragment_datagram(current)?);
                cursor += 1;
                continue;
            }

            let mut batch_payloads = vec![current.payload.clone()];
            let mut batch_datagrams = vec![current.clone()];
            let mut batch_len = V1_HEADER_LEN + 2 + 2 + current.payload.len();
            let mut next = cursor + 1;
            while next < planned.len() {
                let candidate = &planned[next];
                if candidate.fragment.is_some()
                    || candidate.path.path_id() != current.path.path_id()
                    || candidate.peer_scope != current.peer_scope
                {
                    break;
                }
                let candidate_len = 2 + candidate.payload.len();
                if batch_len + candidate_len > current.path.mtu() {
                    break;
                }
                batch_len += candidate_len;
                batch_payloads.push(candidate.payload.clone());
                batch_datagrams.push(candidate.clone());
                next += 1;
            }

            let frame = if batch_payloads.len() > 1 {
                Frame::batch(service_id, current.path.path_id(), current.sequence, &batch_payloads)?
            } else {
                Frame::data(
                    service_id,
                    current.path.path_id(),
                    current.sequence,
                    current.payload.clone(),
                )?
            };
            frames.push(FramePlan {
                frame,
                path: current.path.clone(),
                datagrams: batch_datagrams,
                fragment_count: 1,
            });
            cursor = next;
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

#[derive(Debug)]
pub(crate) struct FramePlan {
    pub(crate) frame: Frame,
    pub(crate) path: CorePathConfig,
    pub(crate) datagrams: Vec<PlannedDatagram>,
    pub(crate) fragment_count: usize,
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
