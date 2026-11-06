//! Additive, versioned NDJSON control protocol. Legacy text remains accepted.
use serde::Deserialize;
use serde_json::{Value, json};

/// Maximum wire frame size, including the terminating newline.
pub const MAX_FRAME: usize = 64 * 1024;
pub const MAX_RESPONSE: usize = 1024 * 1024;
pub const MAX_SUBSCRIBERS: usize = 8;
pub const CAPABILITIES: &[&str] = &["settings", "subscribe", "sequential"];

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub v: u64,
    pub id: Option<String>,
    pub op: String,
    pub if_revision: Option<String>,
    pub settings: Option<Value>,
    pub folders: Option<Vec<String>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub interval_seconds: u64,
    pub selection: String,
    pub folders: Vec<String>,
}

pub fn error(code: &str, message: impl ToString) -> Value {
    json!({"ok":false,"code":code,"error":message.to_string()})
}

pub fn parse(frame: &[u8]) -> Result<Request, Value> {
    if frame.len() >= MAX_FRAME {
        return Err(error("frame_too_large", "Request exceeds 64 KiB"));
    }
    let request: Request =
        serde_json::from_slice(frame).map_err(|e| error("invalid_request", e))?;
    if request.v != 1 {
        return Err(error(
            "unsupported_version",
            "Only protocol v:1 is supported",
        ));
    }
    Ok(request)
}

pub fn metadata(mut value: Value, id: Option<&str>) -> Value {
    value["v"] = json!(1);
    if let Some(id) = id {
        value["id"] = json!(id);
    }
    value["protocol_version"] = json!(1);
    value["daemon_version"] = json!(env!("CARGO_PKG_VERSION"));
    value["capabilities"] = json!(CAPABILITIES);
    if value["ok"] == false && value.get("code").is_none() {
        value["code"] = json!("command_failed");
    }
    value
}

// Quickshell decodes socket chunks independently. ASCII JSON keeps Unicode
// codepoints intact even when a socket read splits a multibyte UTF-8 sequence.
// JSON.parse reconstructs the original strings, including surrogate pairs.
fn ascii_json(value: &Value) -> Vec<u8> {
    use std::fmt::Write;
    let text = serde_json::to_string(value).expect("JSON values serialize");
    if text.is_ascii() {
        return text.into_bytes();
    }
    let mut wire = String::with_capacity(text.len());
    for character in text.chars() {
        if character.is_ascii() {
            wire.push(character);
        } else {
            for unit in character.encode_utf16(&mut [0; 2]) {
                write!(wire, "\\u{unit:04x}").expect("String writes cannot fail");
            }
        }
    }
    wire.into_bytes()
}

pub fn encode(value: &Value) -> Vec<u8> {
    let mut bytes = ascii_json(value);
    if bytes.len() + 1 > MAX_RESPONSE {
        let mut failure = error("response_too_large", "Response exceeds 1 MiB");
        if let Some(id) = value.get("id") {
            failure["id"] = id.clone();
        }
        bytes = ascii_json(&metadata(failure, None));
    }
    bytes.push(b'\n');
    bytes
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_version_envelope_and_frame_limit() {
        assert!(parse(br#"{"v":1,"id":"a","op":"hello"}"#).is_ok());
        assert!(parse(br#"{"v":2,"op":"hello"}"#).is_err());
        assert!(parse(br#"{"v":1,"id":42,"op":"hello"}"#).is_err());
        assert!(parse(&vec![b' '; MAX_FRAME]).is_err());
        let mut frame = br#"{"v":1,"op":"hello"}"#.to_vec();
        frame.resize(MAX_FRAME - 1, b' ');
        assert!(parse(&frame).is_ok());
        frame.push(b' ');
        assert_eq!(parse(&frame).unwrap_err()["code"], "frame_too_large");
    }
    #[test]
    fn wire_is_ascii_and_round_trips_unicode_at_every_chunk_boundary() {
        let value = json!({"path":"/pictures/雪 café 😀/100% #.png", "id":"é😀"});
        let bytes = encode(&value);
        assert!(bytes.is_ascii());
        for split in 0..bytes.len() {
            let a = std::str::from_utf8(&bytes[..split]).unwrap();
            let b = std::str::from_utf8(&bytes[split..]).unwrap();
            let restored: Value = serde_json::from_str(&format!("{a}{b}")).unwrap();
            assert_eq!(restored, value);
        }
    }
    #[test]
    fn bounded_response_preserves_correlation() {
        let bytes = encode(&json!({"id":"a","data":"x".repeat(MAX_RESPONSE)}));
        assert!(bytes.len() <= MAX_RESPONSE);
        let value: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["id"], "a");
        assert_eq!(value["code"], "response_too_large");
    }
}
