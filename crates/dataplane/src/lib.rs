//! gatherlink-dataplane
//!
//! High-speed Rust packet engine. No business policy belongs here.

pub mod engine;
pub mod runtime_config;
pub mod udp_service;
pub mod sockets;
pub mod receive;
pub mod transmit;
pub mod dedupe;
pub mod reorder;
pub mod mtu;
pub mod queues;
pub mod metrics;
pub mod errors;
