//! Core dataplane engine.
//!
//! The engine owns userland UDP service sockets and applies the Gatherlink
//! frame boundary before emitting virtual UDP payloads. Business policy stays in
//! Python; this module executes already-compiled runtime state.

use std::fmt;
use std::net::SocketAddr;

use gatherlink_protocol::control::ControlPayload;
use gatherlink_protocol::errors::ProtocolError;
use gatherlink_protocol::frame::{Frame, FrameKind, V1_HEADER_LEN};

use crate::fragmentation::{fragment_datagram, FragmentReassembly};
use crate::metrics::{DataplaneMetrics, MetricsSnapshot};
use crate::runtime_config::{CorePathConfig, CoreRuntimeConfig, SchedulerMode};
use crate::scheduler::all_paths::AllPathSelector;
use crate::scheduler::weighted_rr::WeightedRoundRobin;
use crate::udp_service::{UdpServiceError, UserlandUdpService};

/// Core userland UDP dataplane.
#[derive(Debug)]
pub struct CoreDataplane {
    services: Vec<UserlandUdpService>,
    paths: Vec<CorePathConfig>,
    scheduler: WeightedRoundRobin,
    next_sequence: u64,
    next_datagram_id: u32,
    metrics: DataplaneMetrics,
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

        Ok(Self {
            services,
            paths: config.paths().to_vec(),
            scheduler: compile_scheduler(config.scheduler().mode(), config.paths()),
            next_sequence: 1,
            next_datagram_id: 1,
            metrics: DataplaneMetrics::new(config.paths()),
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
        self.paths = config.paths().to_vec();
        self.scheduler = compile_scheduler(config.scheduler().mode(), config.paths());
        self.metrics.reconcile_paths(config.paths());
        Ok(outcome)
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

    /// Record telemetry for a received remote data frame.
    ///
    /// Python still owns what to do with this fact. Rust only updates the same counters the production receive loop
    /// will eventually maintain directly.
    pub fn observe_received_data_frame(&mut self, service_id: u64, path_id: u16, sequence: u64, payload_len: usize) {
        self.metrics.record_receive(service_id, path_id, sequence, payload_len);
    }

    /// Record a received control metaband payload parsed by the protocol crate.
    pub fn observe_received_control_payload(&mut self, payload: &ControlPayload, frame_bytes: usize) {
        self.metrics.record_control_received(payload, frame_bytes);
    }

    /// Record a received control metaband payload on a specific path.
    pub fn observe_received_control_payload_on_path(
        &mut self,
        path_id: u16,
        payload: &ControlPayload,
        frame_bytes: usize,
    ) {
        self.metrics
            .record_control_received_on_path(path_id, payload, frame_bytes);
    }

    /// Record a sent control metaband payload parsed by the protocol crate.
    pub fn observe_sent_control_payload(&mut self, payload: &ControlPayload, frame_bytes: usize) {
        self.metrics.record_control_sent(payload, frame_bytes);
    }

    /// Record a sent control metaband payload on a specific path.
    pub fn observe_sent_control_payload_on_path(&mut self, path_id: u16, payload: &ControlPayload, frame_bytes: usize) {
        self.metrics.record_control_sent_on_path(path_id, payload, frame_bytes);
    }

    /// Build duplicate control frames for every eligible path.
    ///
    /// Control duplication is deliberately separate from ordinary data scheduling:
    /// every returned frame carries the same sequence and payload bytes, while
    /// the frame path id identifies the transport path that carried the copy.
    pub fn duplicate_control_payload_for_all_paths(
        &mut self,
        service_id: u64,
        payload: Vec<u8>,
    ) -> Result<Vec<ControlTransmitPlan>, DataplaneError> {
        let path_indices = AllPathSelector::select_path_indices(&self.paths, payload.len());
        if path_indices.is_empty() {
            return Err(DataplaneError::NoPathAvailable);
        }

        let sequence = self.next_sequence;
        self.next_sequence += 1;
        let mut plans = Vec::with_capacity(path_indices.len());
        for path_index in path_indices {
            let path = &self.paths[path_index];
            let frame = Frame::control(
                0,
                service_id,
                path.path_id(),
                path.route_id(),
                sequence,
                payload.clone(),
            )?;
            let encoded = frame.encode()?;
            if encoded.len() > path.mtu() {
                return Err(DataplaneError::FrameExceedsPathMtu {
                    path_id: path.path_id(),
                    frame_len: encoded.len(),
                    mtu: path.mtu(),
                });
            }
            plans.push(ControlTransmitPlan {
                path_id: path.path_id(),
                route_id: path.route_id(),
                sequence,
                frame_len: encoded.len(),
                payload_len: payload.len(),
            });
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
        });

        for _ in 1..max_datagrams {
            let mut buffer = vec![0_u8; u16::MAX as usize];
            let Some((length, source)) = self.services[service_index].try_recv_from(&mut buffer)? else {
                break;
            };
            datagrams.push(ReceivedDatagram {
                payload: buffer[..length].to_vec(),
                source,
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
        let service_id = service_index as u64 + 1;
        let planned = self.plan_datagrams(service_id, datagrams)?;
        let frames = self.coalesce_planned_frames(service_id, planned)?;
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
            match decoded.header.kind {
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
                        path_id: decoded.header.path_id,
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
                            path_id: decoded.header.path_id,
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

    fn plan_datagrams(
        &mut self,
        service_id: u64,
        datagrams: Vec<ReceivedDatagram>,
    ) -> Result<Vec<PlannedDatagram>, DataplaneError> {
        let mut planned = Vec::with_capacity(datagrams.len());
        for datagram in datagrams {
            let sequence = self.next_sequence;
            self.next_sequence += 1;
            let path = self.select_path(datagram.payload.len())?.clone();
            let fragment = if datagram.payload.len() > path.max_data_payload() {
                let datagram_id = self.next_datagram_id;
                self.next_datagram_id = self.next_datagram_id.wrapping_add(1).max(1);
                Some(datagram_id)
            } else {
                None
            };
            planned.push(PlannedDatagram {
                payload: datagram.payload,
                source: datagram.source,
                sequence,
                service_id,
                path,
                fragment,
            });
        }
        Ok(planned)
    }

    fn select_path(&mut self, payload_len: usize) -> Result<&CorePathConfig, DataplaneError> {
        let path_index = self
            .scheduler
            .select_path_index(&self.paths, payload_len)
            .ok_or(DataplaneError::NoPathAvailable)?;
        Ok(&self.paths[path_index])
    }

    fn coalesce_planned_frames(
        &self,
        service_id: u64,
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
                if candidate.fragment.is_some() || candidate.path.path_id() != current.path.path_id() {
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
                Frame::batch(
                    0,
                    service_id,
                    current.path.path_id(),
                    current.path.route_id(),
                    current.sequence,
                    &batch_payloads,
                )?
            } else {
                Frame::data(
                    0,
                    service_id,
                    current.path.path_id(),
                    current.path.route_id(),
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
}

fn compile_scheduler(mode: SchedulerMode, paths: &[CorePathConfig]) -> WeightedRoundRobin {
    match mode {
        SchedulerMode::RoundRobin => WeightedRoundRobin::compile(paths),
    }
}

#[derive(Debug, Clone)]
struct ReceivedDatagram {
    payload: Vec<u8>,
    source: SocketAddr,
}

#[derive(Debug, Clone)]
pub(crate) struct PlannedDatagram {
    pub(crate) payload: Vec<u8>,
    pub(crate) source: SocketAddr,
    pub(crate) sequence: u64,
    pub(crate) service_id: u64,
    pub(crate) path: CorePathConfig,
    pub(crate) fragment: Option<u32>,
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

/// Planned duplicate control frame for one path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlTransmitPlan {
    pub path_id: u16,
    pub route_id: u16,
    pub sequence: u64,
    pub frame_len: usize,
    pub payload_len: usize,
}

/// Dataplane execution errors.
#[derive(Debug)]
pub enum DataplaneError {
    UdpService(UdpServiceError),
    Protocol(ProtocolError),
    UnknownService(String),
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
            Self::UnknownService(name) => write!(formatter, "unknown UDP service: {name}"),
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
