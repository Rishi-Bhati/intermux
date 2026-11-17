//! Protocol v1: newline-delimited JSON; transport code enforces the line limit.
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const VERSION: u32 = 1;
pub const DAEMON_VERSION: &str = "3.0.0-beta.1";
pub const MAX_LINE: usize = 64 * 1024;
pub const TOPICS: [&str; 4] = ["interfaces", "bindings", "traffic", "gui"];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Request {
    pub v: u32,
    pub id: Value,
    pub method: String,
    pub params: Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    BadRequest,
    UnknownMethod,
    UnsupportedVersion,
    Internal,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProtocolError {
    pub code: ErrorCode,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Response {
    Success {
        v: u32,
        id: Value,
        result: Value,
    },
    Failure {
        v: u32,
        id: Value,
        error: ProtocolError,
    },
}

impl Response {
    pub fn success(id: Value, result: Value) -> Self {
        Self::Success {
            v: VERSION,
            id,
            result,
        }
    }
    pub fn error(id: Value, code: ErrorCode, message: impl Into<String>) -> Self {
        Self::Failure {
            v: VERSION,
            id,
            error: ProtocolError {
                code,
                message: message.into(),
            },
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub v: u32,
    pub event: String,
    pub data: Value,
}

impl Event {
    pub fn new(event: &str, data: Value) -> Self {
        Self {
            v: VERSION,
            event: event.into(),
            data,
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct HelloParams {
    pub client: String,
    pub protocols: Vec<u32>,
}

#[derive(Debug, Deserialize)]
pub struct SubscribeParams {
    pub topics: Vec<String>,
}
