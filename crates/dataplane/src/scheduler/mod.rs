//! Compiled scheduler execution.
//!
//! Python owns scheduler policy and scoring. This module only executes the
//! compiled scheduler mode and path state handed to Rust.

pub mod all_paths;
pub mod weighted_rr;
