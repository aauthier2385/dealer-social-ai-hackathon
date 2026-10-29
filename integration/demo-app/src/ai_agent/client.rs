//! Server-side client for the Dealer Social AI service.
//!
//! Configured by two environment variables. When either is missing, every AI feature stays
//! hidden and the rest of the app works exactly as before:
//!   DEALER_SOCIAL_AI_URL    e.g. https://dealer-social-ai.onrender.com (http only for 127.0.0.1)
//!   DEALER_SOCIAL_AI_TOKEN  the AI service's SERVICE_TOKEN (at least 32 characters)

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::time::Duration;
use uuid::Uuid;

#[derive(Clone)]
pub struct Config {
    base: String,
    token: String,
}

/// Why an AI call failed, in words that are safe to show staff.
#[derive(Debug)]
pub struct AiError {
    pub status: u16,
    pub message: String,
}

impl AiError {
    fn new(status: u16, message: impl Into<String>) -> Self {
        AiError {
            status,
            message: message.into(),
        }
    }
}

pub fn config() -> Option<Config> {
    let base = std::env::var("DEALER_SOCIAL_AI_URL").ok()?;
    let token = std::env::var("DEALER_SOCIAL_AI_TOKEN").ok()?;
    let base = base.trim().trim_end_matches('/').to_string();
    let token = token.trim().to_string();
    let local = base.starts_with("http://127.0.0.1:") || base.starts_with("http://localhost:");
    if !(base.starts_with("https://") || local) || token.len() < 32 || token.contains(char::is_whitespace) {
        return None;
    }
    Some(Config { base, token })
}

pub fn enabled() -> bool {
    config().is_some()
}

/// The AI service rate-limits per user. It gets an opaque reference, never the user's id.
fn user_ref(user: Uuid) -> String {
    let secret = std::env::var("APP_SECRET").unwrap_or_default();
    let digest = Sha256::digest(format!("dsai:{secret}:{user}").as_bytes());
    digest.iter().take(12).map(|b| format!("{b:02x}")).collect()
}

fn http() -> &'static reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none()) // never forward the token elsewhere
            .connect_timeout(Duration::from_secs(10))
            .build()
            .expect("reqwest client")
    })
}

const MAX_REPLY: usize = 1024 * 1024;
const MAX_PHOTO_REPLY: usize = 8 * 1024 * 1024; // the blurred photo comes back inline

async fn send(
    path: &str,
    user: Uuid,
    content_type: &str,
    body: Vec<u8>,
    timeout: Duration,
) -> Result<Value, AiError> {
    send_limited(path, user, content_type, body, timeout, MAX_REPLY).await
}

async fn send_limited(
    path: &str,
    user: Uuid,
    content_type: &str,
    body: Vec<u8>,
    timeout: Duration,
    max_reply: usize,
) -> Result<Value, AiError> {
    let cfg = config().ok_or_else(|| AiError::new(503, "The AI assistant is not set up on this site."))?;
    let resp = http()
        .post(format!("{}{}", cfg.base, path))
        .bearer_auth(&cfg.token)
        .header("content-type", content_type)
        .header("x-user-ref", user_ref(user))
        .timeout(timeout)
        .body(body)
        .send()
        .await
        .map_err(|e| {
            eprintln!("[ai] {} unreachable: timeout={}", path.split('?').next().unwrap_or(""), e.is_timeout());
            AiError::new(503, "The AI assistant is not responding. Try again in a minute.")
        })?;
    let status = resp.status().as_u16();
    let bytes = resp
        .bytes()
        .await
        .map_err(|_| AiError::new(502, "The AI assistant sent an incomplete reply."))?;
    if bytes.len() > max_reply {
        return Err(AiError::new(502, "The AI assistant sent a reply that was too large."));
    }
    let json: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    if (200..300).contains(&status) {
        return Ok(json);
    }
    // Only the event, status and path are logged: never customer content.
    eprintln!("[ai] {} status={status}", path.split('?').next().unwrap_or(""));
    let message = match status {
        401 => "The AI assistant rejected this site's token. Check DEALER_SOCIAL_AI_TOKEN.".to_string(),
        _ => json
            .get("message")
            .and_then(Value::as_str)
            .map(|m| m.chars().take(200).collect())
            .unwrap_or_else(|| "The AI assistant could not finish that request.".to_string()),
    };
    Err(AiError::new(status, message))
}

pub async fn voice_note(user: Uuid, wav: Vec<u8>) -> Result<Value, AiError> {
    send("/v1/memory/voice-note", user, "audio/wav", wav, Duration::from_secs(150)).await
}

/// Feature 5: blur bystanders' faces and license plates in a store photo (SAM 3.1).
/// `query` carries the staff member's taps (keep/blur points) and the blur-every-face switch.
pub async fn photo_blur(user: Uuid, photo: Vec<u8>, content_type: &str, query: &str) -> Result<Value, AiError> {
    let path = if query.is_empty() {
        "/v1/photo/privacy-blur".to_string()
    } else {
        format!("/v1/photo/privacy-blur?{query}")
    };
    send_limited(&path, user, content_type, photo, Duration::from_secs(120), MAX_PHOTO_REPLY).await
}

pub async fn post_json(path: &str, user: Uuid, body: &Value) -> Result<Value, AiError> {
    let bytes = serde_json::to_vec(body).map_err(|_| AiError::new(400, "Could not prepare the request."))?;
    send(path, user, "application/json", bytes, Duration::from_secs(90)).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_ref_is_opaque_and_stable() {
        let id = Uuid::new_v4();
        let a = user_ref(id);
        assert_eq!(a, user_ref(id));
        assert_eq!(a.len(), 24);
        assert!(!a.contains(&id.to_string()[..8]));
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
