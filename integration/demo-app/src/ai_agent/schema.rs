//! Three new tables. Each statement is safe to run on every start.

use sqlx::PgPool;

const STATEMENTS: &[&str] = &[
    // Who sold whom: the customer's salesperson at this store, and the basics of the sale.
    "CREATE TABLE IF NOT EXISTS customer_relationships(
        id UUID PRIMARY KEY,
        dealership_id UUID NOT NULL REFERENCES dealerships(id) ON DELETE CASCADE,
        customer_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        salesperson_id UUID REFERENCES users(id) ON DELETE SET NULL,
        vehicle TEXT NOT NULL DEFAULT '',
        delivery_date DATE,
        birthday TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT now(),
        updated_at TIMESTAMP NOT NULL DEFAULT now(),
        UNIQUE(dealership_id, customer_id))",
    "CREATE INDEX IF NOT EXISTS idx_rel_salesperson ON customer_relationships(dealership_id, salesperson_id)",
    // Verified relationship facts. Staff only: customers never see these.
    "CREATE TABLE IF NOT EXISTS customer_facts(
        id UUID PRIMARY KEY,
        relationship_id UUID NOT NULL REFERENCES customer_relationships(id) ON DELETE CASCADE,
        about TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL,
        fact TEXT NOT NULL,
        timing TEXT NOT NULL DEFAULT '',
        certainty TEXT NOT NULL DEFAULT 'stated',
        quote TEXT NOT NULL DEFAULT '',
        heard_as TEXT NOT NULL DEFAULT '',
        created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMP NOT NULL DEFAULT now(),
        UNIQUE(relationship_id, fact))",
    // The follow-up to-do list. Drafts are approved and sent by a person, never automatically.
    "CREATE TABLE IF NOT EXISTS follow_ups(
        id UUID PRIMARY KEY,
        dealership_id UUID NOT NULL REFERENCES dealerships(id) ON DELETE CASCADE,
        customer_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        salesperson_id UUID REFERENCES users(id) ON DELETE SET NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        due_date DATE NOT NULL,
        channel TEXT NOT NULL,
        draft_text TEXT NOT NULL DEFAULT '',
        call_notes JSONB NOT NULL DEFAULT '[]',
        card_text TEXT NOT NULL DEFAULT '',
        uses_facts JSONB NOT NULL DEFAULT '[]',
        brief JSONB,
        source TEXT NOT NULL DEFAULT 'plan',
        status TEXT NOT NULL DEFAULT 'pending',
        snoozed_until DATE,
        done_note TEXT NOT NULL DEFAULT '',
        completed_at TIMESTAMP,
        completed_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMP NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS idx_followups_due ON follow_ups(dealership_id, status, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_followups_owner ON follow_ups(salesperson_id, status, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_followups_customer ON follow_ups(customer_id, status, due_date)",
];

pub async fn migrate(p: &PgPool) {
    for sql in STATEMENTS {
        if let Err(e) = sqlx::query(sql).execute(p).await {
            panic!("AI agent migration failed: {e}\n{sql}");
        }
    }
}
