//! The Follow-ups tab (the daily to-do list), the bar in each customer's chat, the salesperson
//! handoff and the manager's at-risk brief.
//!
//! The rule: this tab is the to-do list, the chat is where messages are sent. "Use draft" opens the
//! customer's conversation with the draft already in the message box; a person edits and sends it.

use actix_web::{web, HttpRequest, HttpResponse};
use chrono::{Duration, NaiveDate};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::PgPool;
use uuid::Uuid;

use super::client::{self, AiError};
use super::voice::person_name;
use crate::routes::ui::{current_user, esc, go, html, shell, CurrentUser};

pub fn routes(c: &mut web::ServiceConfig) {
    c.route("/follow-ups", web::get().to(page))
        .route("/follow-ups/{id}/done", web::post().to(done))
        .route("/follow-ups/{id}/snooze", web::post().to(snooze))
        .route("/follow-ups/{id}/skip", web::post().to(skip))
        .route("/follow-ups/handoff", web::post().to(handoff))
        .route("/follow-ups/at-risk", web::post().to(at_risk));
}

// ------------------------------------------------------------------------------------------
// Shared helpers
// ------------------------------------------------------------------------------------------

/// A pending follow-up counts as due from its due date, or from the end of a snooze.
const DUE_SQL: &str = "COALESCE(GREATEST(f.due_date, f.snoozed_until), f.due_date)";

/// Overdue + due today, for the nav badge. Salespeople count their own; admins the whole store.
pub async fn due_count(p: &PgPool, me: &CurrentUser) -> i64 {
    if !me.is_staff() || !client::enabled() {
        return 0;
    }
    let today = crate::timezone::today(&me.timezone);
    sqlx::query_as::<_, (i64,)>(&format!(
        "SELECT COUNT(*) FROM follow_ups f WHERE f.dealership_id=$1 AND f.status='pending' \
         AND {DUE_SQL}<=$2 AND ($3 OR f.salesperson_id=$4)"
    ))
    .bind(me.dealership_id)
    .bind(today)
    .bind(me.is_admin())
    .bind(me.id)
    .fetch_one(p)
    .await
    .map(|r| r.0)
    .unwrap_or(0)
}

/// Facts for one customer at this store, numbered F1, F2... for the AI service.
async fn load_facts(p: &PgPool, store: Uuid, customer: Uuid, limit: i64) -> Vec<Value> {
    let rows: Vec<(String, String, String, String, String)> = sqlx::query_as(
        "SELECT f.about,f.category,f.fact,f.timing,f.certainty FROM customer_facts f \
         JOIN customer_relationships r ON r.id=f.relationship_id \
         WHERE r.dealership_id=$1 AND r.customer_id=$2 ORDER BY f.created_at, f.fact LIMIT $3",
    )
    .bind(store)
    .bind(customer)
    .bind(limit)
    .fetch_all(p)
    .await
    .unwrap_or_default();
    rows.into_iter()
        .enumerate()
        .map(|(n, (about, category, fact, timing, certainty))| {
            json!({"id": format!("F{}", n + 1), "about": about, "category": category,
                   "fact": fact, "timing": timing, "certainty": certainty})
        })
        .collect()
}

/// The salesperson's own recent texts to customers: the AI copies their voice from these.
/// The AI service swaps customer names out and ignores anything sensitive before using them.
async fn voice_samples(p: &PgPool, store: Uuid, salesperson: Uuid) -> Vec<String> {
    sqlx::query_as::<_, (String,)>(
        "SELECT m.body FROM messages m JOIN users u ON u.id=m.recipient_id \
         WHERE m.sender_id=$1 AND m.dealership_id=$2 AND u.role='customer' \
         AND char_length(m.body) BETWEEN 20 AND 320 ORDER BY m.created_at DESC LIMIT 5",
    )
    .bind(salesperson)
    .bind(store)
    .fetch_all(p)
    .await
    .unwrap_or_default()
    .into_iter()
    .map(|r| r.0)
    .collect()
}

fn strings(v: &Value, limit: usize) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().or_else(|| x.get("fact").and_then(Value::as_str)))
                .take(limit)
                .map(|s| s.chars().take(300).collect())
                .collect()
        })
        .unwrap_or_default()
}

fn text(v: &Value, key: &str, limit: usize) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(limit)
        .collect()
}

/// Build (or rebuild) the customer's follow-up plan with the AI service and store it.
/// Pending plan items are replaced; anything already done, sent or skipped is kept.
pub async fn build_plan(p: &PgPool, me: &CurrentUser, customer: Uuid) -> Result<usize, AiError> {
    let rel: Option<(Option<Uuid>, String, Option<NaiveDate>, Option<String>)> = sqlx::query_as(
        "SELECT salesperson_id,vehicle,delivery_date,birthday FROM customer_relationships \
         WHERE dealership_id=$1 AND customer_id=$2",
    )
    .bind(me.dealership_id)
    .bind(customer)
    .fetch_optional(p)
    .await
    .ok()
    .flatten();
    let Some((owner, vehicle, delivery, birthday)) = rel else {
        return Err(AiError { status: 404, message: "No relationship record for this customer.".into() });
    };
    let owner = owner.unwrap_or(me.id);
    let today = crate::timezone::today(&me.timezone);
    let body = json!({
        "customer_name": person_name(p, customer, me.dealership_id).await,
        "vehicle": vehicle,
        "salesperson_name": person_name(p, owner, me.dealership_id).await,
        "store_name": me.dealership_name,
        "delivery_date": delivery.unwrap_or(today).to_string(),
        "birthday": birthday.unwrap_or_default(),
        "voice_samples": voice_samples(p, me.dealership_id, owner).await,
        "facts": load_facts(p, me.dealership_id, customer, 60).await,
    });
    let plan = client::post_json("/v1/plan/touchpoints", me.id, &body).await?;
    let touchpoints = plan.get("touchpoints").and_then(Value::as_array).cloned().unwrap_or_default();

    let mut tx = p.begin().await.map_err(|_| AiError { status: 500, message: "Database busy.".into() })?;
    let _ = sqlx::query(
        "DELETE FROM follow_ups WHERE dealership_id=$1 AND customer_id=$2 AND status='pending' AND source='plan'",
    )
    .bind(me.dealership_id)
    .bind(customer)
    .execute(&mut *tx)
    .await;
    let mut created = 0;
    for tp in &touchpoints {
        let Some(due) = tp.get("date").and_then(Value::as_str).and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok()) else {
            continue;
        };
        let res = sqlx::query(
            "INSERT INTO follow_ups(id,dealership_id,customer_id,salesperson_id,kind,title,due_date,channel,draft_text,call_notes,card_text,uses_facts,source) \
             VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'plan')",
        )
        .bind(Uuid::new_v4())
        .bind(me.dealership_id)
        .bind(customer)
        .bind(owner)
        .bind(text(tp, "kind", 30))
        .bind(text(tp, "title", 80))
        .bind(due)
        .bind(text(tp, "channel", 20))
        .bind(text(tp, "text", 600))
        .bind(json!(strings(tp.get("call_notes").unwrap_or(&Value::Null), 8)))
        .bind(text(tp, "card", 800))
        .bind(json!(strings(tp.get("uses_facts").unwrap_or(&Value::Null), 12)))
        .execute(&mut *tx)
        .await;
        if res.is_ok() {
            created += 1;
        }
    }
    tx.commit().await.map_err(|_| AiError { status: 500, message: "Could not save the plan.".into() })?;
    Ok(created)
}

type Row = (
    Uuid,          // id
    Uuid,          // customer
    String,        // customer name
    Option<Uuid>,  // salesperson
    String,        // salesperson name
    String,        // kind
    String,        // title
    NaiveDate,     // effective due date
    String,        // channel
    String,        // draft text
    Value,         // call notes
    String,        // card
    Value,         // uses facts
    Option<Value>, // handoff brief
);

const ROW_SQL: &str = "SELECT f.id,f.customer_id,\
 COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'')||' '||COALESCE(c.last_name,'')),''),c.username),\
 f.salesperson_id,\
 COALESCE((SELECT sp.display_name FROM staff_profiles sp WHERE sp.user_id=f.salesperson_id AND sp.dealership_id=f.dealership_id LIMIT 1),\
   (SELECT NULLIF(TRIM(COALESCE(s.first_name,'')||' '||COALESCE(s.last_name,'')),'') FROM users s WHERE s.id=f.salesperson_id),'Unassigned'),\
 f.kind,f.title,COALESCE(GREATEST(f.due_date, f.snoozed_until), f.due_date),f.channel,f.draft_text,f.call_notes,f.card_text,f.uses_facts,f.brief \
 FROM follow_ups f JOIN users c ON c.id=f.customer_id";

async fn owned(p: &PgPool, me: &CurrentUser, id: Uuid) -> bool {
    sqlx::query_as::<_, (bool,)>(
        "SELECT true FROM follow_ups WHERE id=$1 AND dealership_id=$2 AND status='pending' AND ($3 OR salesperson_id=$4)",
    )
    .bind(id)
    .bind(me.dealership_id)
    .bind(me.is_admin())
    .bind(me.id)
    .fetch_optional(p)
    .await
    .ok()
    .flatten()
    .is_some()
}

/// Called after a staff member sends a message that started from a follow-up draft.
pub async fn mark_sent(p: &PgPool, me: &CurrentUser, id: Uuid, customer: Uuid) {
    let _ = sqlx::query(
        "UPDATE follow_ups SET status='sent',completed_at=now(),completed_by=$3 \
         WHERE id=$1 AND customer_id=$2 AND dealership_id=$4 AND status='pending' AND ($5 OR salesperson_id=$3)",
    )
    .bind(id)
    .bind(customer)
    .bind(me.id)
    .bind(me.dealership_id)
    .bind(me.is_admin())
    .execute(p)
    .await;
}

/// The draft to put in the message box when a conversation is opened from "Use draft".
pub async fn draft_for(p: &PgPool, me: &CurrentUser, id: Uuid, customer: Uuid) -> Option<String> {
    if !me.is_staff() || !owned(p, me, id).await {
        return None;
    }
    sqlx::query_as::<_, (String,)>(
        "SELECT draft_text FROM follow_ups WHERE id=$1 AND customer_id=$2 AND draft_text<>''",
    )
    .bind(id)
    .bind(customer)
    .fetch_optional(p)
    .await
    .ok()
    .flatten()
    .map(|r| r.0)
}

fn due_label(due: NaiveDate, today: NaiveDate) -> (String, &'static str) {
    let days = (due - today).num_days();
    match days {
        d if d < -1 => (format!("{} days overdue", -d), "late"),
        -1 => ("1 day overdue".into(), "late"),
        0 => ("Due today".into(), "today"),
        1 => ("Tomorrow".into(), ""),
        _ => (due.format("%a %b %-d").to_string(), ""),
    }
}

fn channel_label(c: &str) -> &'static str {
    match c {
        "text" => "Text",
        "call" => "Call",
        "call_or_text" => "Call or text",
        "mail" => "Mail",
        _ => "Follow-up",
    }
}

fn chips(v: &Value, class: &str) -> String {
    strings(v, 12)
        .iter()
        .map(|s| format!(r#"<span class="fu-chip {class}">{}</span>"#, esc(s)))
        .collect()
}

fn list(v: &Value) -> String {
    let items: String = strings(v, 8).iter().map(|s| format!("<li>{}</li>", esc(s))).collect();
    if items.is_empty() {
        String::new()
    } else {
        format!(r#"<ul class="fu-notes">{items}</ul>"#)
    }
}

fn brief_html(b: &Option<Value>) -> String {
    let Some(b) = b else { return String::new() };
    let points = |key: &str| -> String {
        b.get(key)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.get("point").and_then(Value::as_str))
                    .map(|s| format!("<li>{}</li>", esc(s)))
                    .collect()
            })
            .unwrap_or_default()
    };
    format!(
        r#"<details class="fu-brief"><summary>Handoff brief</summary><p><b>{}</b></p><p class="fu-sub">Know before you call</p><ul class="fu-notes">{}</ul><p class="fu-sub">Talk about</p><ul class="fu-notes">{}</ul></details>"#,
        esc(b.get("headline").and_then(Value::as_str).unwrap_or("")),
        points("know_first"),
        points("talk_about")
    )
}

fn item_html(r: &Row, today: NaiveDate, show_owner: bool) -> String {
    let (id, customer, name, _sp, sp_name, _kind, title, due, channel, draft, notes, card, uses, brief) = r;
    let (due_text, due_cls) = due_label(*due, today);
    let mut body = String::new();
    if !card.is_empty() {
        body.push_str(&format!(r#"<div class="fu-draft card-msg">{}</div>"#, esc(card)));
    }
    if !draft.is_empty() {
        body.push_str(&format!(r#"<div class="fu-draft">{}</div>"#, esc(&draft.replace("{booking_link}", "[booking link]"))));
    }
    body.push_str(&list(notes));
    let uses = chips(uses, "");
    if !uses.is_empty() {
        body.push_str(&format!(r#"<div class="fu-chips">{uses}</div>"#));
    }
    body.push_str(&brief_html(brief));
    let use_draft = if !draft.is_empty() {
        format!(r#"<a class="btn sm" href="/messages/{customer}?followup={id}">Use draft</a>"#)
    } else {
        format!(r#"<a class="btn sm ghost" href="/messages/{customer}">Open chat</a>"#)
    };
    let owner = if show_owner {
        format!(r#"<span class="fu-chip owner">{}</span>"#, esc(sp_name))
    } else {
        String::new()
    };
    format!(
        r#"<li class="fu-item {due_cls}"><div class="fu-head"><a class="fu-name" href="/messages/{customer}">{name}</a><span class="fu-title">{title}</span><span class="fu-chip channel">{channel}</span>{owner}<span class="fu-due {due_cls}">{due_text}</span></div>{body}
<div class="fu-actions">{use_draft}
<form method="post" action="/follow-ups/{id}/done" class="fu-done"><input name="note" maxlength="120" placeholder="Note (optional), e.g. left voicemail" aria-label="Note"><button class="btn sm green">Mark done</button></form>
<form method="post" action="/follow-ups/{id}/snooze"><input type="hidden" name="days" value="3"><button class="sm ghost btn">Snooze 3 days</button></form>
<form method="post" action="/follow-ups/{id}/snooze"><input type="hidden" name="days" value="7"><button class="sm ghost btn">1 week</button></form>
<form method="post" action="/follow-ups/{id}/skip"><button class="sm danger">Skip</button></form>
</div></li>"#,
        name = esc(name),
        title = esc(title),
        channel = channel_label(channel),
    )
}

// ------------------------------------------------------------------------------------------
// The Follow-ups tab
// ------------------------------------------------------------------------------------------

#[derive(Deserialize)]
struct PageQuery {
    /// "all" (admin default), "mine", or a salesperson's user id.
    who: Option<String>,
    done: Option<String>,
    error: Option<String>,
}

async fn staff_options(p: &PgPool, store: Uuid) -> Vec<(Uuid, String, bool)> {
    sqlx::query_as(
        "SELECT sp.user_id, sp.display_name, sp.employment_inactive FROM staff_profiles sp \
         WHERE sp.dealership_id=$1 AND sp.user_id IS NOT NULL ORDER BY sp.employment_inactive, sp.display_name",
    )
    .bind(store)
    .fetch_all(p)
    .await
    .unwrap_or_default()
}

async fn page(r: HttpRequest, p: web::Data<PgPool>, q: web::Query<PageQuery>) -> HttpResponse {
    render(&r, p.get_ref(), &q, None).await
}

async fn render(r: &HttpRequest, p: &PgPool, q: &PageQuery, risk: Option<String>) -> HttpResponse {
    let me = match current_user(r, p).await {
        Some(u) => u,
        None => return go("/login"),
    };
    if !me.is_staff() {
        return go("/feed");
    }
    if !client::enabled() {
        let body = r#"<div class="page-head"><div><h1>Follow-ups</h1><p>The AI follow-up assistant is not set up on this site yet.</p></div></div>"#;
        return html(shell("Follow-ups", "followups", &me, body));
    }
    let today = crate::timezone::today(&me.timezone);
    let who = q.who.as_deref().unwrap_or(if me.is_admin() { "all" } else { "mine" });
    let filter: Option<Uuid> = if !me.is_admin() || who == "mine" {
        Some(me.id)
    } else {
        Uuid::parse_str(who).ok()
    };

    let rows: Vec<Row> = sqlx::query_as(&format!(
        "{ROW_SQL} WHERE f.dealership_id=$1 AND f.status='pending' AND ($2::uuid IS NULL OR f.salesperson_id=$2) \
         AND {DUE_SQL}<=$3 ORDER BY {DUE_SQL}, c.last_name, f.created_at LIMIT 300"
    ))
    .bind(me.dealership_id)
    .bind(filter)
    .bind(today + Duration::days(14))
    .fetch_all(p)
    .await
    .unwrap_or_default();
    let later: i64 = sqlx::query_as::<_, (i64,)>(&format!(
        "SELECT COUNT(*) FROM follow_ups f WHERE f.dealership_id=$1 AND f.status='pending' \
         AND ($2::uuid IS NULL OR f.salesperson_id=$2) AND {DUE_SQL}>$3"
    ))
    .bind(me.dealership_id)
    .bind(filter)
    .bind(today + Duration::days(14))
    .fetch_one(p)
    .await
    .map(|r| r.0)
    .unwrap_or(0);

    let show_owner = filter.is_none() || filter != Some(me.id);
    let section = |title: &str, cls: &str, items: Vec<&Row>, empty: &str| -> String {
        let inner: String = items.iter().map(|r| item_html(r, today, show_owner)).collect();
        let inner = if inner.is_empty() {
            format!(r#"<li class="fu-empty">{empty}</li>"#)
        } else {
            inner
        };
        format!(
            r#"<section class="fu-section {cls}"><h2>{title} <span class="fu-count">{}</span></h2><ol class="fu-list">{inner}</ol></section>"#,
            items.len()
        )
    };
    let overdue: Vec<&Row> = rows.iter().filter(|r| r.7 < today).collect();
    let due_today: Vec<&Row> = rows.iter().filter(|r| r.7 == today).collect();
    let coming: Vec<&Row> = rows.iter().filter(|r| r.7 > today).collect();

    let mut admin_tools = String::new();
    if me.is_admin() {
        let staff = staff_options(p, me.dealership_id).await;
        let mut options = String::new();
        for (id, name, inactive) in &staff {
            let sel = if Some(*id) == filter && who != "mine" { " selected" } else { "" };
            options.push_str(&format!(
                r#"<option value="{id}"{sel}>{}{}</option>"#,
                esc(name),
                if *inactive { " (left)" } else { "" }
            ));
        }
        let all_sel = if who == "all" { " selected" } else { "" };
        let mine_sel = if who == "mine" { " selected" } else { "" };
        let from: String = staff
            .iter()
            .map(|(id, n, inactive)| format!(r#"<option value="{id}">{}{}</option>"#, esc(n), if *inactive { " (left)" } else { "" }))
            .collect();
        let to: String = staff
            .iter()
            .filter(|s| !s.2)
            .map(|(id, n, _)| format!(r#"<option value="{id}">{}</option>"#, esc(n)))
            .collect();
        admin_tools = format!(
            r#"<div class="card fu-tools">
<form method="get" action="/follow-ups" class="fu-filter"><label for="who">Showing</label><select id="who" name="who" onchange="this.form.submit()"><option value="all"{all_sel}>Whole store</option><option value="mine"{mine_sel}>Just mine</option>{options}</select><noscript><button class="sm">Show</button></noscript></form>
<form method="post" action="/follow-ups/at-risk"><button class="sm" data-busy="Building the brief…">This week's at-risk brief</button></form>
<details class="fu-handoff"><summary>Reassign a salesperson's customers</summary>
<form method="post" action="/follow-ups/handoff" class="fu-handoff-form">
<label>From <select name="from" required>{from}</select></label>
<label>To <select name="to" required>{to}</select></label>
<button class="sm" data-busy="Writing introductions…">Reassign &amp; write introductions</button>
<p class="muted small">Pending follow-ups move to the new salesperson, and each customer gets a warm introduction in their voice, with a brief. Up to 25 at a time.</p>
</form></details></div>"#
        );
    }

    let notice = match (q.done.as_deref(), q.error.as_deref()) {
        (Some(msg), _) => format!(r#"<div class="fu-notice ok" role="status">{}</div>"#, esc(msg)),
        (_, Some(msg)) => format!(r#"<div class="fu-notice bad" role="alert">{}</div>"#, esc(msg)),
        _ => String::new(),
    };
    let later_line = if later > 0 {
        format!(r#"<p class="muted small fu-later">{later} more scheduled after the next two weeks (birthdays, anniversaries and first service visits).</p>"#)
    } else {
        String::new()
    };

    let body = format!(
        r#"<style>{FU_CSS}</style>
<div class="page-head"><div><h1>Follow-ups</h1><p>Your follow-up schedule, drafted by AI in your voice. You approve every message.</p></div></div>
{notice}{admin_tools}{risk}
{overdue}{today_s}{coming}{later_line}
<script>document.querySelectorAll('button[data-busy]').forEach(function(b){{b.form.addEventListener('submit',function(){{b.textContent=b.getAttribute('data-busy');b.disabled=true;}});}});</script>"#,
        risk = risk.unwrap_or_default(),
        overdue = section("Overdue", "late", overdue, "Nothing overdue. Nice work."),
        today_s = section("Today", "today", due_today, "Nothing due today."),
        coming = section("Coming up", "", coming, "Nothing in the next two weeks."),
    );
    html(shell("Follow-ups", "followups", &me, &body))
}

// ------------------------------------------------------------------------------------------
// Actions
// ------------------------------------------------------------------------------------------

#[derive(Deserialize)]
struct DoneForm {
    #[serde(default)]
    note: String,
}

#[derive(Deserialize)]
struct SnoozeForm {
    days: i64,
}

fn back(msg: &str) -> HttpResponse {
    go(&format!("/follow-ups?done={}", urlencode(msg)))
}

fn back_error(msg: &str) -> HttpResponse {
    go(&format!("/follow-ups?error={}", urlencode(msg)))
}

fn urlencode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' => (b as char).to_string(),
            b' ' => "+".into(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

async fn staff(r: &HttpRequest, p: &PgPool) -> Option<CurrentUser> {
    current_user(r, p).await.filter(|u| u.is_staff())
}

async fn done(r: HttpRequest, p: web::Data<PgPool>, id: web::Path<Uuid>, f: web::Form<DoneForm>) -> HttpResponse {
    let Some(me) = staff(&r, p.get_ref()).await else { return go("/login") };
    let id = id.into_inner();
    if !owned(p.get_ref(), &me, id).await {
        return back_error("That follow-up is no longer open.");
    }
    let note: String = f.note.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(120).collect();
    let _ = sqlx::query("UPDATE follow_ups SET status='done',done_note=$2,completed_at=now(),completed_by=$3 WHERE id=$1")
        .bind(id)
        .bind(note)
        .bind(me.id)
        .execute(p.get_ref())
        .await;
    back("Marked done.")
}

async fn snooze(r: HttpRequest, p: web::Data<PgPool>, id: web::Path<Uuid>, f: web::Form<SnoozeForm>) -> HttpResponse {
    let Some(me) = staff(&r, p.get_ref()).await else { return go("/login") };
    let id = id.into_inner();
    if !owned(p.get_ref(), &me, id).await || !(1..=30).contains(&f.days) {
        return back_error("That follow-up is no longer open.");
    }
    let until = crate::timezone::today(&me.timezone) + Duration::days(f.days);
    let _ = sqlx::query("UPDATE follow_ups SET snoozed_until=$2 WHERE id=$1")
        .bind(id)
        .bind(until)
        .execute(p.get_ref())
        .await;
    back(&format!("Snoozed until {}.", until.format("%a %b %-d")))
}

async fn skip(r: HttpRequest, p: web::Data<PgPool>, id: web::Path<Uuid>) -> HttpResponse {
    let Some(me) = staff(&r, p.get_ref()).await else { return go("/login") };
    let id = id.into_inner();
    if !owned(p.get_ref(), &me, id).await {
        return back_error("That follow-up is no longer open.");
    }
    let _ = sqlx::query("UPDATE follow_ups SET status='skipped',completed_at=now(),completed_by=$2 WHERE id=$1")
        .bind(id)
        .bind(me.id)
        .execute(p.get_ref())
        .await;
    back("Skipped.")
}

// ------------------------------------------------------------------------------------------
// Handoff: reassign a salesperson's customers, with a warm introduction for each
// ------------------------------------------------------------------------------------------

#[derive(Deserialize)]
struct HandoffForm {
    from: Uuid,
    to: Uuid,
}

const HANDOFF_BATCH: i64 = 25;

async fn handoff(r: HttpRequest, p: web::Data<PgPool>, f: web::Form<HandoffForm>) -> HttpResponse {
    let pool = p.get_ref();
    let Some(me) = current_user(&r, pool).await.filter(|u| u.is_admin()) else { return go("/follow-ups") };
    if f.from == f.to {
        return back_error("Choose two different salespeople.");
    }
    // The new salesperson must be active staff at this store.
    let to_ok: Option<(bool,)> = sqlx::query_as(
        "SELECT true FROM staff_profiles WHERE user_id=$1 AND dealership_id=$2 AND NOT employment_inactive",
    )
    .bind(f.to)
    .bind(me.dealership_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();
    if to_ok.is_none() {
        return back_error("The new salesperson must be active at this store.");
    }
    let customers: Vec<(Uuid, String, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT customer_id, vehicle, delivery_date FROM customer_relationships \
         WHERE dealership_id=$1 AND salesperson_id=$2 ORDER BY updated_at DESC LIMIT $3",
    )
    .bind(me.dealership_id)
    .bind(f.from)
    .bind(HANDOFF_BATCH)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    if customers.is_empty() {
        return back_error("That salesperson has no customers to reassign.");
    }
    let from_name = person_name(pool, f.from, me.dealership_id).await;
    let to_name = person_name(pool, f.to, me.dealership_id).await;
    let samples = voice_samples(pool, me.dealership_id, f.to).await;
    let today = crate::timezone::today(&me.timezone);

    let (mut moved, mut briefs) = (0, 0);
    for (customer, vehicle, since) in customers {
        let name = person_name(pool, customer, me.dealership_id).await;
        let body = json!({
            "customer_name": name, "vehicle": vehicle, "store_name": me.dealership_name,
            "from_salesperson": from_name, "to_salesperson": to_name,
            "customer_since": since.map(|d| d.to_string()).unwrap_or_default(),
            "voice_samples": samples, "facts": load_facts(pool, me.dealership_id, customer, 60).await,
        });
        let first = name.split_whitespace().next().unwrap_or("there").to_string();
        let to_first = to_name.split_whitespace().next().unwrap_or("").to_string();
        let (intro, notes, brief) = match client::post_json("/v1/handoff/brief", me.id, &body).await {
            Ok(b) => {
                briefs += 1;
                let intro = b.get("intro").cloned().unwrap_or(Value::Null);
                (
                    text(&intro, "text", 600),
                    json!(strings(intro.get("call_notes").unwrap_or(&Value::Null), 6)),
                    Some(json!({"headline": b.get("headline"), "know_first": b.get("know_first"), "talk_about": b.get("talk_about")})),
                )
            }
            // No AI reply: still move the customer, with a plain introduction to edit.
            Err(_) => (
                format!("Hey {first}, it's {to_first} at {}. I'll be taking care of you from here on out. Let me know if you ever need anything!", me.dealership_name),
                json!([]),
                None,
            ),
        };
        let mut tx = match pool.begin().await {
            Ok(t) => t,
            Err(_) => break,
        };
        let _ = sqlx::query("UPDATE customer_relationships SET salesperson_id=$3, updated_at=now() WHERE dealership_id=$1 AND customer_id=$2")
            .bind(me.dealership_id).bind(customer).bind(f.to).execute(&mut *tx).await;
        let _ = sqlx::query("UPDATE follow_ups SET salesperson_id=$3 WHERE dealership_id=$1 AND customer_id=$2 AND status='pending'")
            .bind(me.dealership_id).bind(customer).bind(f.to).execute(&mut *tx).await;
        let _ = sqlx::query(
            "INSERT INTO follow_ups(id,dealership_id,customer_id,salesperson_id,kind,title,due_date,channel,draft_text,call_notes,brief,source) \
             VALUES($1,$2,$3,$4,'handoff_intro','Introduce yourself',$5,'text',$6,$7,$8,'handoff')",
        )
        .bind(Uuid::new_v4()).bind(me.dealership_id).bind(customer).bind(f.to).bind(today)
        .bind(intro).bind(notes).bind(brief)
        .execute(&mut *tx)
        .await;
        if tx.commit().await.is_ok() {
            moved += 1;
        }
    }
    back(&format!(
        "Moved {moved} customer{} from {from_name} to {to_name}. {briefs} introduction{} written by AI and waiting in {to_name}'s follow-ups.",
        if moved == 1 { "" } else { "s" },
        if briefs == 1 { "" } else { "s" }
    ))
}

// ------------------------------------------------------------------------------------------
// At-risk brief (manager)
// ------------------------------------------------------------------------------------------

async fn at_risk(r: HttpRequest, p: web::Data<PgPool>) -> HttpResponse {
    let pool = p.get_ref();
    let Some(me) = current_user(&r, pool).await.filter(|u| u.is_admin()) else { return go("/follow-ups") };
    let today = crate::timezone::today(&me.timezone);
    let rels: Vec<(Uuid, Uuid, Option<Uuid>, String, Option<NaiveDate>, bool, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT r.id, r.customer_id, r.salesperson_id, r.vehicle, r.delivery_date, \
           (r.salesperson_id IS NOT NULL AND EXISTS(SELECT 1 FROM users s WHERE s.id=r.salesperson_id AND s.status='active') \
            AND NOT EXISTS(SELECT 1 FROM staff_profiles sp WHERE sp.user_id=r.salesperson_id AND sp.dealership_id=r.dealership_id AND sp.employment_inactive)), \
           (SELECT MAX(m.created_at)::date FROM messages m JOIN users o ON o.id=CASE WHEN m.sender_id=r.customer_id THEN m.recipient_id ELSE m.sender_id END \
             WHERE m.dealership_id=r.dealership_id AND (m.sender_id=r.customer_id OR m.recipient_id=r.customer_id) AND o.role<>'customer') \
         FROM customer_relationships r WHERE r.dealership_id=$1 ORDER BY r.updated_at DESC LIMIT 300",
    )
    .bind(me.dealership_id)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    if rels.is_empty() {
        return render(&r, pool, &PageQuery { who: None, done: None, error: Some("No customer relationships yet. Save a voice note in a customer's chat first.".into()) }, None).await;
    }

    let mut customers = Vec::new();
    for (rel, customer, sp, vehicle, delivered, active, last) in &rels {
        let overdue: Vec<(String, NaiveDate)> = sqlx::query_as(&format!(
            "SELECT f.kind, f.due_date FROM follow_ups f WHERE f.customer_id=$1 AND f.dealership_id=$2 AND f.status='pending' AND {DUE_SQL}<$3 ORDER BY f.due_date LIMIT 10"
        ))
        .bind(customer)
        .bind(me.dealership_id)
        .bind(today)
        .fetch_all(pool)
        .await
        .unwrap_or_default();
        let known = ["day1", "card", "day30", "day90", "birthday", "anniversary", "life_event", "handoff_intro"];
        customers.push(json!({
            "ref": rel.to_string(),
            "customer_name": person_name(pool, *customer, me.dealership_id).await,
            "salesperson": match sp { Some(s) => person_name(pool, *s, me.dealership_id).await, None => String::new() },
            "salesperson_active": active,
            "vehicle": vehicle,
            "delivery_date": delivered.map(|d| d.to_string()).unwrap_or_default(),
            "last_contact_date": last.map(|d| d.to_string()).unwrap_or_default(),
            "overdue": overdue.iter().filter(|o| known.contains(&o.0.as_str())).map(|(k, d)| json!({"kind": k, "due_date": d.to_string()})).collect::<Vec<_>>(),
            "facts": load_facts(pool, me.dealership_id, *customer, 12).await,
        }));
    }
    let body = json!({"store_name": me.dealership_name, "as_of": today.to_string(), "top": 8, "ai": true, "customers": customers});
    let section = match client::post_json("/v1/risk/brief", me.id, &body).await {
        Ok(v) => risk_html(&v, &rels),
        Err(e) => format!(r#"<div class="fu-notice bad" role="alert">{}</div>"#, esc(&e.message)),
    };
    render(&r, pool, &PageQuery { who: None, done: None, error: None }, Some(section)).await
}

fn risk_html(v: &Value, rels: &[(Uuid, Uuid, Option<Uuid>, String, Option<NaiveDate>, bool, Option<NaiveDate>)]) -> String {
    let t = v.get("totals").cloned().unwrap_or(Value::Null);
    let n = |k: &str| t.get(k).and_then(Value::as_i64).unwrap_or(0);
    let mut items = String::new();
    for c in v.get("customers").and_then(Value::as_array).cloned().unwrap_or_default() {
        let customer = c
            .get("ref")
            .and_then(Value::as_str)
            .and_then(|r| Uuid::parse_str(r).ok())
            .and_then(|r| rels.iter().find(|x| x.0 == r))
            .map(|x| x.1);
        let link = customer.map(|id| format!(r#"<a class="btn sm ghost" href="/messages/{id}">Open chat</a>"#)).unwrap_or_default();
        let signals: String = c
            .get("signals")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|s| s.get("text").and_then(Value::as_str)).map(|s| format!("<li>{}</li>", esc(s))).collect())
            .unwrap_or_default();
        let level = text(&c, "level", 10);
        let sp = text(&c, "salesperson", 60);
        let active = c.get("salesperson_active").and_then(Value::as_bool).unwrap_or(true);
        let sp_label = if sp.is_empty() { "No salesperson".to_string() } else if active { sp } else { format!("{sp} (left)") };
        let opener = text(&c, "opener", 240);
        let opener = if opener.is_empty() { String::new() } else { format!(r#"<div class="fu-draft">{}</div>"#, esc(&opener)) };
        items.push_str(&format!(
            r#"<li class="fu-item risk-{level}"><div class="fu-head"><span class="fu-rank">#{rank}</span><span class="fu-name">{name}</span><span class="fu-chip level-{level}">{level_label}</span><span class="fu-chip owner">{sp}</span></div>
<ul class="fu-notes muted">{signals}</ul><p>{why}</p><p><b>Next:</b> {step} <span class="fu-chip channel">{owner}</span></p>{opener}<div class="fu-actions">{link}</div></li>"#,
            rank = c.get("rank").and_then(Value::as_i64).unwrap_or(0),
            name = esc(&text(&c, "customer", 80)),
            level_label = if level == "high" { "High" } else { "Medium" },
            sp = esc(&sp_label),
            why = esc(&text(&c, "why", 300)),
            step = esc(&text(&c, "next_step", 200)),
            owner = esc(&text(&c, "owner", 60)),
        ));
    }
    if items.is_empty() {
        items = r#"<li class="fu-empty">Every relationship is on track this week.</li>"#.into();
    }
    format!(
        r#"<section class="card fu-risk"><h2>Relationships at risk this week</h2><p><b>{headline}</b></p>
<p class="muted small">{checked} customers checked by code · {attention} need attention · {nosp} with no salesperson · {od} overdue follow-ups. Explanations by Muse Spark, each tied to the signals it cites.</p>
<ol class="fu-list">{items}</ol></section>"#,
        headline = esc(&text(v, "headline", 240)),
        checked = n("customers_checked"),
        attention = n("needs_attention"),
        nosp = n("no_salesperson"),
        od = n("overdue_follow_ups"),
    )
}

// ------------------------------------------------------------------------------------------
// The bar at the top of a customer's chat (staff only)
// ------------------------------------------------------------------------------------------

pub async fn chat_bar(p: &PgPool, me: &CurrentUser, customer: Uuid) -> String {
    if !me.is_staff() || !client::enabled() {
        return String::new();
    }
    let next: Option<Row> = sqlx::query_as(&format!(
        "{ROW_SQL} WHERE f.dealership_id=$1 AND f.customer_id=$2 AND f.status='pending' ORDER BY {DUE_SQL} LIMIT 1"
    ))
    .bind(me.dealership_id)
    .bind(customer)
    .fetch_optional(p)
    .await
    .ok()
    .flatten();
    let facts: i64 = sqlx::query_as::<_, (i64,)>(
        "SELECT COUNT(*) FROM customer_facts f JOIN customer_relationships r ON r.id=f.relationship_id WHERE r.dealership_id=$1 AND r.customer_id=$2",
    )
    .bind(me.dealership_id)
    .bind(customer)
    .fetch_one(p)
    .await
    .map(|r| r.0)
    .unwrap_or(0);
    let today = crate::timezone::today(&me.timezone);
    let has_plan = if next.is_some() { "1" } else { "0" };
    let next_html = match next {
        Some(r) => {
            let (due, cls) = due_label(r.7, today);
            let action = if r.9.is_empty() {
                r#"<a class="btn sm ghost" href="/follow-ups">Open follow-ups</a>"#.to_string()
            } else {
                format!(r#"<a class="btn sm" href="/messages/{}?followup={}">Use draft</a>"#, r.1, r.0)
            };
            format!(r#"<span class="ai-next"><b>{}</b> · <span class="fu-due {cls}">{due}</span></span>{action}"#, esc(&r.6))
        }
        None => r#"<span class="ai-next muted">No follow-ups scheduled yet.</span>"#.into(),
    };
    let remembered = if facts > 0 {
        format!(r#"<span class="fu-chip">{facts} things remembered</span>"#)
    } else {
        String::new()
    };
    format!(
        r#"<div class="ai-bar" data-customer="{customer}" data-has-plan="{has_plan}">{next_html}{remembered}<button type="button" class="btn sm ghost" id="aiVoiceOpen">🎙 Voice note</button></div>
<div class="ai-panel" id="aiPanel" hidden></div>"#
    )
}

pub const FU_CSS: &str = r#"
.fu-section{margin-top:18px}.fu-section h2{display:flex;align-items:center;gap:8px}
.fu-section.late h2{color:var(--red)}
.fu-count{font-size:13px;font-weight:700;background:#eef2f7;color:var(--muted);border-radius:999px;padding:1px 9px}
.fu-list{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.fu-item{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--ford-2);border-radius:12px;padding:12px 14px;display:grid;gap:8px;box-shadow:var(--shadow)}
.fu-item.late,.fu-item.risk-high{border-left-color:var(--red)}.fu-item.today,.fu-item.risk-medium{border-left-color:var(--amber)}
.fu-head{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.fu-name{font-weight:750;font-size:16px;text-decoration:none;color:var(--text)}
.fu-title{font-weight:600}.fu-rank{font-weight:800;color:var(--muted)}
.fu-due{margin-left:auto;font-size:13px;font-weight:700;color:var(--muted)}.fu-due.late{color:var(--red)}.fu-due.today{color:#9a6700}
.fu-chip{font-size:11px;letter-spacing:.03em;text-transform:uppercase;border:1px solid var(--line);border-radius:999px;padding:2px 8px;color:var(--muted);background:#fff}
.fu-chip.channel{border-color:var(--ford-2);color:var(--ford-2)}.fu-chip.owner{border-color:#c9d4e3}
.fu-chip.level-high{border-color:var(--red);color:var(--red)}.fu-chip.level-medium{border-color:var(--amber);color:#9a6700}
.fu-chips{display:flex;flex-wrap:wrap;gap:6px}
.fu-draft{background:#f3f7fc;border-radius:10px;padding:10px 12px;white-space:pre-wrap;overflow-wrap:anywhere}
.fu-draft.card-msg{font-family:"Segoe Print","Bradley Hand",cursive}
.fu-notes{margin:0;padding-left:20px}.fu-sub{margin:8px 0 2px;font-weight:700;font-size:13px}
.fu-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.fu-actions form{margin:0;display:flex;gap:6px}
.fu-done input{width:230px;max-width:100%;padding:6px 10px;border:1px solid var(--line);border-radius:9px;font:inherit;font-size:13px}
.fu-empty{color:var(--muted);padding:10px 2px}
.fu-notice{border-radius:12px;padding:10px 14px;margin-bottom:12px;font-weight:600}.fu-notice.ok{background:#e7f6e9;color:#1d5c27}.fu-notice.bad{background:#fdecec;color:#8c1d20}
.fu-tools{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:6px}.fu-tools form{margin:0}
.fu-filter{display:flex;gap:8px;align-items:center}.fu-filter select,.fu-handoff select{padding:6px 10px;border:1px solid var(--line);border-radius:9px;font:inherit}
.fu-handoff{flex-basis:100%}.fu-handoff summary{cursor:pointer;font-weight:650;color:var(--ford-2)}
.fu-handoff-form{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:10px}.fu-handoff-form p{flex-basis:100%;margin:0}
.fu-brief summary{cursor:pointer;font-weight:650;color:var(--ford-2)}
.fu-risk{margin-top:14px}.fu-later{margin-top:14px}
@media(max-width:640px){.fu-due{margin-left:0}.fu-done input{width:100%}}
"#;
