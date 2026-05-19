//! Protocol version constants.
//!
//! Version fields are part of authenticated protocol context later. Public UDP
//! listeners must still silently drop unauthenticated/invalid packets.

/// Initial protocol version for core userland UDP frames.
pub const PROTOCOL_VERSION: u8 = 1;
