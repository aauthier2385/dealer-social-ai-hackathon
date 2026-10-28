//! Voice note in a customer's chat -> reviewed facts -> follow-up plan.
//!
//! 1. Staff record or upload a voice note in the customer's conversation. The browser converts it
//!    to 16 kHz mono WAV and sends it here; this app forwards it to the AI service, which returns
//!    verified facts (each tied to the words it came from) with sensitive details withheld.
//! 2. Staff review it: fix a misheard name, untick anything they do not want kept.
//! 3. Save stores the facts on the customer's record (staff only) and, if asked, builds the
//!    follow-up plan in the salesperson's own texting voice.

use actix_web::{web, HttpRequest, HttpResponse};
use chrono::NaiveDate;
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::PgPool;
use uuid::Uuid;

use super::client::{self, AiError};
use crate::routes::ui::{current_user, CurrentUser};

const MAX_WAV: usize = 12 * 1024 * 1024; // 5 minutes of 16 kHz mono is about 9.6 MB
const MAX_FACTS: usize = 60;
pub const CATEGORIES: &[&str] = &[
    "family",
    "pets",
    "hobbies",
    "work",
    "vehicle",
    "life_event",
    "contact_preference",
    "important_date",
    "personal",
];

pub fn routes(c: &mut web::ServiceConfig) {
    c.service(
        web::resource("/ai/voice-note/{customer}")
            .app_data(web::PayloadConfig::new(MAX_WAV))
            .route(web::post().to(transcribe)),
    )
    .service(
        web::resource("/ai/voice-note/{customer}/save")
            .app_data(web::JsonConfig::default().limit(256 * 1024))
            .route(web::post().to(save)),
    );
}

pub fn json_error(status: u16, message: &str) -> HttpResponse {
    HttpResponse::build(
        actix_web::http::StatusCode::from_u16(status)
            .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY),
    )
    .insert_header(("Cache-Control", "no-store"))
    .json(json!({ "message": message }))
}

fn ai_error(e: AiError) -> HttpResponse {
    let status = if e.status == 429 { 429 } else if e.status >= 500 || e.status == 401 { 503 } else { 400 };
    json_error(status, &e.message)
}

/// The display name the app already uses for a person: staff roster name first, then their own.
pub async fn person_name(p: &PgPool, user: Uuid, store: Uuid) -> String {
    sqlx::query_as::<_, (String,)>(
        "SELECT COALESCE((SELECT sp.display_name FROM staff_profiles sp WHERE sp.user_id=u.id AND sp.dealership_id=$2 LIMIT 1),\
         NULLIF(TRIM(COALESCE(u.first_name,'')||' '||COALESCE(u.last_name,'')),''),u.username) FROM users u WHERE u.id=$1",
    )
    .bind(user)
    .bind(store)
    .fetch_one(p)
    .await
    .map(|r| r.0)
    .unwrap_or_else(|_| "Customer".into())
}

/// A customer of the signed-in staff member's current store, or None.
pub async fn store_customer(p: &PgPool, me: &CurrentUser, customer: Uuid) -> Option<String> {
    let ok: Option<(Uuid,)> = sqlx::query_as(
        "SELECT u.id FROM users u WHERE u.id=$1 AND u.role='customer' AND u.status='active' \
         AND EXISTS(SELECT 1 FROM dealership_memberships dm WHERE dm.user_id=u.id AND dm.dealership_id=$2)",
    )
    .bind(customer)
    .bind(me.dealership_id)
    .fetch_optional(p)
    .await
    .ok()
    .flatten();
    ok?;
    Some(person_name(p, customer, me.dealership_id).await)
}

async fn staff_and_customer(
    r: &HttpRequest,
    p: &PgPool,
    customer: Uuid,
) -> Result<(CurrentUser, String), HttpResponse> {
    let me = current_user(r, p)
        .await
        .ok_or_else(|| json_error(401, "Please sign in again."))?;
    if !me.is_staff() {
        return Err(json_error(403, "Only staff can add relationship notes."));
    }
    if !client::enabled() {
        return Err(json_error(503, "The AI assistant is not set up on this site."));
    }
    let name = store_customer(p, &me, customer)
        .await
        .ok_or_else(|| json_error(404, "That customer is not part of this store."))?;
    Ok((me, name))
}

async fn transcribe(
    r: HttpRequest,
    p: web::Data<PgPool>,
    path: web::Path<Uuid>,
    body: web::Bytes,
) -> HttpResponse {
    let customer = path.into_inner();
    let me_id = match staff_and_customer(&r, p.get_ref(), customer).await {
        Ok((me, _)) => me.id,
        Err(resp) => return resp,
    };
    if body.len() < 44 || &body[0..4] != b"RIFF" || &body[8..12] != b"WAVE" {
        return json_error(415, "Send the recording as WAV audio.");
    }
    match client::voice_note(me_id, body.to_vec()).await {
        Ok(v) => HttpResponse::Ok().insert_header(("Cache-Control", "no-store")).json(v),
        Err(e) => ai_error(e),
    }
}

#[derive(Deserialize)]
struct SaveFact {
    #[serde(default)]
    about: String,
    category: String,
    fact: String,
    #[serde(default)]
    timing: String,
    #[serde(default)]
    certainty: String,
    #[serde(default)]
    quote: String,
    #[serde(default)]
    heard_as: String,
}

#[derive(Deserialize)]
struct SaveForm {
    #[serde(default)]
    vehicle: String,
    #[serde(default)]
    delivery_date: String,
    #[serde(default)]
    birthday: String,
    #[serde(default)]
    make_plan: bool,
    facts: Vec<SaveFact>,
}

fn clip(s: &str, n: usize) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(n).collect()
}

pub fn valid_birthday(s: &str) -> Option<String> {
    let s = s.trim();
    let (m, d) = s.split_once('-')?;
    if m.len() != 2 || d.len() != 2 {
        return None;
    }
    let (m, d): (u32, u32) = (m.parse().ok()?, d.parse().ok()?);
    NaiveDate::from_ymd_opt(2024, m, d).map(|_| s.to_string())
}

async fn save(
    r: HttpRequest,
    p: web::Data<PgPool>,
    path: web::Path<Uuid>,
    form: web::Json<SaveForm>,
) -> HttpResponse {
    let customer = path.into_inner();
    let pool = p.get_ref();
    let (me, _name) = match staff_and_customer(&r, pool, customer).await {
        Ok(x) => x,
        Err(resp) => return resp,
    };
    let form = form.into_inner();
    if form.facts.len() > MAX_FACTS {
        return json_error(400, "Too many facts in one note.");
    }
    let delivery = if form.delivery_date.trim().is_empty() {
        None
    } else {
        match NaiveDate::parse_from_str(form.delivery_date.trim(), "%Y-%m-%d") {
            Ok(d) => Some(d),
            Err(_) => return json_error(400, "Delivery date must look like 2026-10-27."),
        }
    };
    let birthday = if form.birthday.trim().is_empty() {
        None
    } else {
        match valid_birthday(&form.birthday) {
            Some(b) => Some(b),
            None => return json_error(400, "Birthday must look like 06-14 (month-day)."),
        }
    };
    let vehicle = clip(&form.vehicle, 80);

    // The salesperson who saves the first note owns the relationship; later notes keep the owner.
    let rel: Result<(Uuid,), _> = sqlx::query_as(
        "INSERT INTO customer_relationships(id,dealership_id,customer_id,salesperson_id,vehicle,delivery_date,birthday) \
         VALUES($1,$2,$3,$4,$5,$6,$7) \
         ON CONFLICT(dealership_id,customer_id) DO UPDATE SET \
           salesperson_id=COALESCE(customer_relationships.salesperson_id,EXCLUDED.salesperson_id), \
           vehicle=CASE WHEN EXCLUDED.vehicle<>'' THEN EXCLUDED.vehicle ELSE customer_relationships.vehicle END, \
           delivery_date=COALESCE(EXCLUDED.delivery_date,customer_relationships.delivery_date), \
           birthday=COALESCE(EXCLUDED.birthday,customer_relationships.birthday), updated_at=now() \
         RETURNING id",
    )
    .bind(Uuid::new_v4())
    .bind(me.dealership_id)
    .bind(customer)
    .bind(me.id)
    .bind(&vehicle)
    .bind(delivery)
    .bind(&birthday)
    .fetch_one(pool)
    .await;
    let rel_id = match rel {
        Ok(r) => r.0,
        Err(e) => {
            eprintln!("[ai] relationship save failed: {e}");
            return json_error(500, "Could not save. Nothing was changed.");
        }
    };

    let mut saved = 0;
    for f in &form.facts {
        let category = f.category.trim();
        let fact = clip(&f.fact, 300);
        if fact.is_empty() || !CATEGORIES.contains(&category) {
            continue; // "sensitive" and unknown categories are never stored
        }
        let certainty = if f.certainty == "uncertain" { "uncertain" } else { "stated" };
        let done = sqlx::query(
            "INSERT INTO customer_facts(id,relationship_id,about,category,fact,timing,certainty,quote,heard_as,created_by) \
             VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(relationship_id,fact) DO NOTHING",
        )
        .bind(Uuid::new_v4())
        .bind(rel_id)
        .bind(clip(&f.about, 80))
        .bind(category)
        .bind(&fact)
        .bind(clip(&f.timing, 60))
        .bind(certainty)
        .bind(clip(&f.quote, 300))
        .bind(clip(&f.heard_as, 80))
        .bind(me.id)
        .execute(pool)
        .await;
        if matches!(done, Ok(ref d) if d.rows_affected() == 1) {
            saved += 1;
        }
    }

    let mut out = json!({ "facts_saved": saved, "follow_ups": 0 });
    if form.make_plan {
        match super::followups::build_plan(pool, &me, customer).await {
            Ok(n) => out["follow_ups"] = json!(n),
            Err(e) => {
                out["plan_error"] = Value::String(format!("Facts saved, but the plan was not built: {}", e.message))
            }
        }
    }
    HttpResponse::Ok().insert_header(("Cache-Control", "no-store")).json(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn birthdays() {
        assert_eq!(valid_birthday("06-14").as_deref(), Some("06-14"));
        assert_eq!(valid_birthday("02-29").as_deref(), Some("02-29"));
        assert!(valid_birthday("02-30").is_none());
        assert!(valid_birthday("6-14").is_none());
        assert!(valid_birthday("June 14").is_none());
    }

    #[test]
    fn clip_collapses_space_and_limits_length() {
        assert_eq!(clip("  a \n b  ", 10), "a b");
        assert_eq!(clip("abcdef", 3), "abc");
    }
}
