//! gatherlink-dataplane
//!
//! High-speed Rust packet engine. No business policy belongs here.

pub mod dedupe;
pub mod engine;
pub mod errors;
mod fragmentation;
pub mod metrics;
pub mod mtu;
pub mod queues;
pub mod receive;
pub mod reorder;
pub mod runtime_config;
mod scheduler;
pub mod sockets;
pub mod transmit;
pub mod udp_service;
