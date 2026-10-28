# Integration with The Dealer Social

The Dealer Social is the existing product (private repository). Every line added to it during the
Hackathon is copied here so judges can see all new work. The demo instance uses fictional people only.

## What was added to the app

| File | What it does |
|---|---|
| [`demo-app/src/ai_agent/client.rs`](demo-app/src/ai_agent/client.rs) | Server-side HTTPS client for this AI service. The service token never reaches a browser; users are sent as an opaque hashed reference. AI features stay hidden unless `DEALER_SOCIAL_AI_URL` and `DEALER_SOCIAL_AI_TOKEN` are set |
| [`demo-app/src/ai_agent/schema.rs`](demo-app/src/ai_agent/schema.rs) | Three new tables: `customer_relationships` (who sold whom), `customer_facts` (verified facts, staff only) and `follow_ups` (the to-do list) |
| [`demo-app/src/ai_agent/voice.rs`](demo-app/src/ai_agent/voice.rs) | Voice note in a customer's chat: forwards the WAV to `/v1/memory/voice-note`, then saves the facts staff kept and builds the plan |
| [`demo-app/src/ai_agent/followups.rs`](demo-app/src/ai_agent/followups.rs) | The **Follow-ups** tab (Overdue, Today, Coming up; Use draft, Mark done, Snooze, Skip), the next-follow-up bar in each chat, the manager's at-risk brief (`/v1/risk/brief`) and reassigning a departed salesperson's customers with warm introductions (`/v1/handoff/brief`) |
| [`demo-app/static/branding/ai-voice-note.js`](demo-app/static/branding/ai-voice-note.js) | Browser side of the voice note: record or choose a file, convert to 16 kHz mono WAV on the device, review the facts, fix misheard names, save |
| [`demo-app/demo_seed/ai_followups_demo.sql`](demo-app/demo_seed/ai_followups_demo.sql) | A fictional store (manager, three salespeople, eight customers) with facts, follow-ups and message history dated from today, for the demo |
| [`demo-app/changes-to-existing-files.diff`](demo-app/changes-to-existing-files.diff) | The few marked lines changed in existing files: register the module and tables, add the nav tab, add the chat bar and "Use draft" to the conversation page, count due follow-ups in the nav badge, and allow the microphone for this site only |

## How the app uses the AI service

```
Customer chat (staff) ── voice note ──► /v1/memory/voice-note ──► review, fix names ──► save facts
                                                                                     └► /v1/plan/touchpoints ──► follow_ups
Follow-ups tab ── Use draft ──► chat with the draft in the message box ──► person sends ──► marked sent
Admin: reassign ──► /v1/handoff/brief per customer ──► intro follow-up + brief for the new salesperson
Admin: at-risk brief ──► /v1/risk/brief (code ranks the whole store; AI explains the top few)
```

Each salesperson's texting voice comes from the last few texts they actually sent customers; the
AI service swaps customer names out of those samples and ignores anything sensitive.
