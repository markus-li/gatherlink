//! Request/response parsing for the privileged time helper.
//!
//! The helper intentionally uses a tiny line-oriented local protocol instead of
//! a broad RPC framework. Python owns policy and sends already-approved,
//! bounded correction requests; this crate only validates the narrow execution
//! primitive before touching system time.

use std::fmt;

pub const DEFAULT_MAX_STEP_US: i64 = 500_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TimeCorrectionRequest {
    pub target_unix_us: i64,
    pub source: String,
    pub quality: String,
    pub max_step_us: i64,
    pub apply: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TimeCorrectionResponse {
    pub status: TimeCorrectionStatus,
    pub applied: bool,
    pub offset_us: i64,
    pub target_unix_us: i64,
    pub system_unix_us: i64,
    pub warning: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeCorrectionStatus {
    Preview,
    Applied,
    Refused,
    Error,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestError {
    message: String,
}

impl RequestError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for RequestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for RequestError {}

impl TimeCorrectionRequest {
    pub fn parse(input: &str) -> Result<Self, RequestError> {
        let mut target_unix_us = None;
        let mut source = None;
        let mut quality = None;
        let mut max_step_us = DEFAULT_MAX_STEP_US;
        let mut apply = false;

        for raw_line in input.lines() {
            let line = raw_line.trim();
            if line.is_empty() {
                continue;
            }
            let (key, value) = line
                .split_once('=')
                .ok_or_else(|| RequestError::new(format!("invalid request line: {line}")))?;
            match key {
                "target_unix_us" => {
                    target_unix_us = Some(parse_i64(key, value)?);
                }
                "source" => {
                    source = Some(value.to_owned());
                }
                "quality" => {
                    quality = Some(value.to_owned());
                }
                "max_step_us" => {
                    max_step_us = parse_i64(key, value)?;
                }
                "apply" => {
                    apply = parse_bool(key, value)?;
                }
                other => {
                    return Err(RequestError::new(format!("unknown request field: {other}")));
                }
            }
        }

        if max_step_us <= 0 {
            return Err(RequestError::new("max_step_us must be positive"));
        }

        Ok(Self {
            target_unix_us: target_unix_us.ok_or_else(|| RequestError::new("target_unix_us is required"))?,
            source: source.unwrap_or_else(|| "unknown".to_owned()),
            quality: quality.unwrap_or_else(|| "unknown".to_owned()),
            max_step_us,
            apply,
        })
    }

    pub fn render(&self) -> String {
        format!(
            "target_unix_us={}\nsource={}\nquality={}\nmax_step_us={}\napply={}\n",
            self.target_unix_us, self.source, self.quality, self.max_step_us, self.apply
        )
    }
}

impl TimeCorrectionResponse {
    pub fn render(&self) -> String {
        let status = match self.status {
            TimeCorrectionStatus::Preview => "preview",
            TimeCorrectionStatus::Applied => "applied",
            TimeCorrectionStatus::Refused => "refused",
            TimeCorrectionStatus::Error => "error",
        };
        let mut rendered = format!(
            "status={status}\napplied={}\noffset_us={}\ntarget_unix_us={}\nsystem_unix_us={}\n",
            self.applied, self.offset_us, self.target_unix_us, self.system_unix_us
        );
        if let Some(warning) = &self.warning {
            rendered.push_str("warning=");
            rendered.push_str(warning);
            rendered.push('\n');
        }
        rendered
    }
}

fn parse_i64(key: &str, value: &str) -> Result<i64, RequestError> {
    value
        .parse::<i64>()
        .map_err(|_| RequestError::new(format!("{key} must be an integer")))
}

fn parse_bool(key: &str, value: &str) -> Result<bool, RequestError> {
    match value {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(RequestError::new(format!("{key} must be true or false"))),
    }
}
