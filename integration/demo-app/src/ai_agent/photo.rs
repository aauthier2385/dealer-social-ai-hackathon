//! Privacy blur before a store photo is posted to the News Feed (staff only).
//!
//! The composer sends the chosen photo here; this app forwards it to the AI service, where SAM 3.1
//! finds every face and license plate. Background people and plates come back blurred and all
//! metadata (including GPS location) is removed. Staff can tap a face to keep or blur it.

use actix_web::{web, HttpRequest, HttpResponse};

use super::client;
use super::voice::json_error;
use crate::routes::ui::current_user;

const MAX_PHOTO: usize = 12 * 1024 * 1024;
const PHOTO_TYPES: &[&str] = &["image/jpeg", "image/png", "image/webp"];

pub fn routes(c: &mut web::ServiceConfig) {
    c.service(
        web::resource("/ai/photo-blur")
            .app_data(web::PayloadConfig::new(MAX_PHOTO))
            .route(web::post().to(blur)),
    );
}

/// Only the taps and the switch are passed on, and only in the shape the AI service expects.
pub fn clean_query(raw: &str) -> String {
    let mut out = Vec::new();
    for pair in raw.split('&') {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let value = value.replace("%2C", ",").replace("%2c", ",").replace("%3B", ";").replace("%3b", ";");
        let ok = !value.is_empty()
            && value.len() <= 400
            && value.chars().all(|c| c.is_ascii_digit() || matches!(c, ',' | ';' | '.'));
        match key {
            "keep" | "blur" if ok => out.push(format!("{key}={value}")),
            "blur_all_faces" if value == "1" => out.push("blur_all_faces=1".into()),
            _ => {}
        }
    }
    out.join("&")
}

async fn blur(r: HttpRequest, p: web::Data<sqlx::PgPool>, body: web::Bytes) -> HttpResponse {
    let Some(me) = current_user(&r, p.get_ref()).await else {
        return json_error(401, "Please sign in again.");
    };
    if !me.is_staff() {
        return json_error(403, "Only staff can use the privacy blur.");
    }
    if !client::enabled() {
        return json_error(503, "The AI assistant is not set up on this site.");
    }
    let content_type = r
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if !PHOTO_TYPES.contains(&content_type.as_str()) {
        return json_error(415, "Send a JPEG, PNG or WebP photo.");
    }
    if body.is_empty() {
        return json_error(400, "Choose a photo first.");
    }
    match client::photo_blur(me.id, body.to_vec(), &content_type, &clean_query(r.query_string())).await {
        Ok(v) => HttpResponse::Ok().insert_header(("Cache-Control", "no-store")).json(v),
        Err(e) => json_error(if e.status == 429 { 429 } else if e.status >= 500 || e.status == 401 { 503 } else { 400 }, &e.message),
    }
}

#[cfg(test)]
mod tests {
    use super::clean_query;

    #[test]
    fn only_taps_and_the_switch_pass() {
        assert_eq!(clean_query("keep=10,20;30,40&blur=5,5&blur_all_faces=1"), "keep=10,20;30,40&blur=5,5&blur_all_faces=1");
        assert_eq!(clean_query("keep=10%2C20%3B30%2C40"), "keep=10,20;30,40");
        assert_eq!(clean_query("keep=<script>&evil=1&blur_all_faces=yes&blur="), "");
    }
}
