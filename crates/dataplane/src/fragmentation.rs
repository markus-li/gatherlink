//! Fragmentation and reassembly helpers.
//!
//! This module is execution plumbing. Policy about when paths should be used is
//! compiled by Python before Rust receives runtime state.

use std::collections::HashMap;

use gatherlink_protocol::frame::{FragmentInfo, Frame};

use crate::engine::{DataplaneError, FramePlan, PlannedDatagram};

/// Build data-frame fragments for one planned datagram.
pub(crate) fn fragment_datagram(datagram: &PlannedDatagram) -> Result<Vec<FramePlan>, DataplaneError> {
    let max_fragment_payload = datagram.path.max_fragment_payload();
    if max_fragment_payload == 0 {
        return Err(DataplaneError::NoPathAvailable);
    }

    let fragment_count = datagram.payload.len().div_ceil(max_fragment_payload);
    if fragment_count > u16::MAX as usize {
        return Err(DataplaneError::TooManyFragments(fragment_count));
    }

    let datagram_id = datagram.fragment.ok_or(DataplaneError::InvalidFragmentPlan)?;
    let mut frames = Vec::with_capacity(fragment_count);
    for (index, chunk) in datagram.payload.chunks(max_fragment_payload).enumerate() {
        let fragment = FragmentInfo::new(datagram_id, index as u16, fragment_count as u16, datagram.payload.len())?;
        let frame = Frame::fragment(
            datagram.service_id,
            datagram.path.path_id(),
            datagram.sequence,
            fragment,
            chunk.to_vec(),
        )?;
        frames.push(FramePlan {
            frame,
            path: datagram.path.clone(),
            datagrams: vec![datagram.to_meta()],
            fragment_count,
        });
    }
    Ok(frames)
}

/// Small in-memory reassembly buffer for decoded fragment frames.
#[derive(Debug, Default)]
pub(crate) struct FragmentReassembly {
    datagrams: HashMap<u32, FragmentBuffer>,
}

impl FragmentReassembly {
    /// Return a complete payload immediately for unfragmented frames, or once all fragments arrive.
    pub(crate) fn push_or_payload(&mut self, frame: &Frame) -> Result<Option<Vec<u8>>, DataplaneError> {
        let Some(fragment) = frame.fragment_info()? else {
            return Ok(Some(frame.payload.clone()));
        };

        let buffer = self
            .datagrams
            .entry(fragment.datagram_id)
            .or_insert_with(|| FragmentBuffer::new(fragment));
        let result = buffer.push(fragment, frame.payload.clone())?;
        if result.is_some() {
            self.datagrams.remove(&fragment.datagram_id);
        }
        Ok(result)
    }
}

#[derive(Debug)]
struct FragmentBuffer {
    original_len: usize,
    chunks: Vec<Option<Vec<u8>>>,
    received: usize,
}

impl FragmentBuffer {
    fn new(fragment: FragmentInfo) -> Self {
        Self {
            original_len: usize::from(fragment.original_len),
            chunks: vec![None; usize::from(fragment.fragment_count)],
            received: 0,
        }
    }

    fn push(&mut self, fragment: FragmentInfo, payload: Vec<u8>) -> Result<Option<Vec<u8>>, DataplaneError> {
        let index = usize::from(fragment.fragment_index);
        if usize::from(fragment.original_len) != self.original_len || index >= self.chunks.len() {
            return Err(DataplaneError::InvalidFragmentPlan);
        }

        if self.chunks[index].is_none() {
            self.received += 1;
        }
        self.chunks[index] = Some(payload);

        if self.received != self.chunks.len() {
            return Ok(None);
        }

        let mut output = Vec::with_capacity(self.original_len);
        for chunk in &self.chunks {
            let chunk = chunk.as_ref().ok_or(DataplaneError::InvalidFragmentPlan)?;
            output.extend_from_slice(chunk);
        }
        if output.len() != self.original_len {
            return Err(DataplaneError::InvalidFragmentPlan);
        }
        Ok(Some(output))
    }
}
