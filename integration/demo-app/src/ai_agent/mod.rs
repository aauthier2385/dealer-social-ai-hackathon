//! Dealer Social AI (built during the Meta Global AI Developer Hackathon, Oct 2026).
//!
//! Connects the app to the separate Dealer Social AI service, which runs Meta's models
//! (Muse Voice Transcribe and Muse Spark). Everything in this folder is new hackathon work:
//!
//! * `client`    - the server-side HTTPS client for the AI service (the token never reaches a browser)
//! * `schema`    - three new tables: customer relationships, customer facts, follow-ups
//! * `voice`     - voice note in a customer's chat -> review -> saved facts + follow-up plan
//! * `followups` - the Follow-ups tab, the chat bar, handoff and the at-risk brief
//!
//! The AI service stores nothing. Customer data lives only in this app's database, and staff
//! approve every message before it is sent.

pub mod client;
pub mod followups;
pub mod schema;
pub mod voice;

use actix_web::web;

pub fn routes(c: &mut web::ServiceConfig) {
    voice::routes(c);
    followups::routes(c);
}
