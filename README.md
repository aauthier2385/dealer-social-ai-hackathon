# Dealer Social AI

**A relationship-memory agent for car dealerships, built on Meta's models.**

Built for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), Startup track, theme *Build for Your World*.

> **Everything in this repository was built during the Hackathon.** It is a new, standalone
> service. The existing product it plugs into, [The Dealer Social](https://www.thedealersocial.com),
> is not part of this repository. The few connecting lines added to that app during the
> Hackathon will be copied into [`integration/`](integration/) so every piece of new work is
> visible here.

## The problem

Dealerships forget people. A salesperson knows that a customer's daughter starts driving in the
spring and that his dog rides in the truck bed, but none of that gets written down. When the
salesperson leaves (and turnover in car sales is high), the relationship leaves with them.

The Dealer Social is a live app that keeps dealership staff and customers connected between
visits. This service gives it a memory.

## What it will do

| | Feature | Meta models | Status |
|---|---|---|---|
| 0 | Service foundation: auth, credit protection, safe Meta client, test page | Muse Spark | ✅ Built |
| 1 | **Relationship memory**: voice note → relationship facts, each linked to the exact sentence it came from | Voice Transcribe, Muse Spark | ✅ Built |
| 2 | **Follow-up planner**: the dealership's own cadence (day-after check-in, thank-you card, 30-day call, first service, birthday, anniversary) plus life-event check-ins, drafted in the salesperson's voice; a human approves every message | Muse Spark | ✅ Built |
| 3 | **Salesperson handoff**: a brief and a warm introduction when a customer's salesperson leaves | Muse Spark | Planned |
| 4 | **Relationships at risk**: a manager brief of customers who need attention | Muse Spark | Planned |
| 5 | **Privacy blur**: bystanders' faces and license plates blurred before store photos are posted | SAM 3.1 | Planned |
| 6 | **Truckiversary**: delivery-day photos turned into one-year anniversary posts | Muse Image | Planned |

## How relationship memory stays honest

Every step can only remove facts, never add them:

1. **Muse Voice Transcribe** turns the voice note into speaker-labelled text (diarization).
2. The transcript is split into numbered sentences `[S1]`, `[S2]`, ...
3. **Muse Spark** extracts relationship facts with **structured output** (a strict JSON schema).
   Each fact must cite one sentence and copy an exact quote from it.
4. **Code verifies every quote** really appears in the transcript. Anything that does not is
   dropped, and a wrong sentence number is corrected to the real source.
5. A **privacy screen** withholds sensitive details (health, money and deal numbers, religion,
   politics, legal trouble, immigration, relationship trouble, bereavement). It runs in code as well as in the
   model, so one model mistake cannot leak a detail.
6. A second **Muse Spark pass acts as the judge**: it re-checks each fact against its sentence (read
   with the lines just before it, so a short answer is judged with its question) and drops anything
   not clearly stated. It also writes the summary, from verified facts only. A missing verdict counts as a rejection (fail closed). The
   judge can lower certainty ("not sure") but never raise it.
7. Sentences behind withheld details are **redacted** from the transcript that is returned.

Phone recordings (.m4a, .mp3, .webm) are converted to 16 kHz mono WAV **in the browser**, so the
server needs no audio software and never receives anything but the format Meta expects.

## How the follow-up planner stays on-brand and safe

- **Dates are calculated in code** from the dealership's cadence, never by the model: the day after
  the sale (call or text, from the customer's stated preference), a thank-you card within 48 hours,
  a 30-day call, the first service visit at 90 days, the birthday (only if known) and the purchase
  anniversary.
- **Muse Spark writes the content** with structured output: texts in the salesperson's own style
  (learned from three real example texts), call notes for calls, and a handwritten thank-you card.
  It may suggest up to three extra check-ins for life events, such as a child turning 16.
- **Each salesperson keeps their own voice**: send up to five texts they have really sent and the
  drafts copy their greeting, punctuation, slang and emoji use. Customer names in those samples are
  swapped for `{first_name}` before anything reaches the model, samples about money or health are
  ignored, and a draft that mentions a customer from the samples is replaced. With no samples, the
  store's default voice is used.
- **Code checks every draft**: it may cite only facts it was given; nothing sensitive; texts fit in
  a message; birthday texts must be pure celebration (no service, deals or offers); only the
  service text may contain the booking link, and it always does. Life-event suggestions must come
  from a stated fact with a timing and fall within two years. A draft that fails is replaced by a
  safe draft in the same voice, and the plan says so.

## Architecture

```
Dealership staff / customers
          │
          ▼
The Dealer Social (existing product, private)      ── demo instance uses fictional data only
          │  HTTPS + shared service token
          ▼
Dealer Social AI (this repository, public)         ── stateless: stores no customer data
          │  Meta Model API key (server-side only)
          ▼
Meta Model API  (Muse Spark · Voice Transcribe · SAM 3.1 · Muse Image)
```

## Safety and privacy by design

- **Secrets come only from the environment** (`.env` locally, Render settings in production). The
  service refuses to start with a missing or weak token, or with Meta enabled and no key.
- **Every AI route requires the service token**, checked in constant time.
- **Credit protection**: per-user, per-minute and per-day limits, plus a cap on simultaneous Meta
  calls. Extra calls are turned away instead of queued.
- **Safe Meta client**: 30-second time limit, response size cap, redirects never followed (the
  key cannot be forwarded), standard-library HTTPS with no third-party HTTP client.
- **Nothing sensitive is logged**: only event names, status codes and timings. A test enforces
  this.
- **Reasoning effort is set per task** (`minimal` for the connection test, `low` for extraction
  and judging) so thinking tokens do not waste credits.
- **Contributor model variants are refused** at startup, so customer content is never offered for
  model training.
- **Strict browser security headers**, including a Content-Security-Policy with no inline scripts.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness check for Render |
| POST | `/v1/ping-model` | Bearer token | Sends a short prompt to Muse Spark and returns the reply |
| POST | `/v1/plan/touchpoints` | Bearer token | JSON: customer, vehicle, salesperson, store, delivery date, optional birthday, optional voice samples, verified facts. Returns a dated plan of drafts and call notes |
| POST | `/v1/memory/voice-note` | Bearer token | Body: a mono 16-bit WAV (16/24 kHz, up to 5 min). Returns people, verified facts with sources, withheld details and a redacted transcript |
| GET | `/playground` | none (page) | Browser test page; the token is typed in and kept in page memory only |

```bash
curl -X POST http://127.0.0.1:8090/v1/ping-model \
  -H "Authorization: Bearer $SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello to the sales team"}'
```

## Run it locally (Windows)

Requires Python 3.11+.

```powershell
Copy-Item .env.example .env      # then set SERVICE_TOKEN and MODEL_API_KEY in .env
py -c "import secrets; print(secrets.token_urlsafe(32))"   # makes a SERVICE_TOKEN
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_local.ps1
```

Open <http://127.0.0.1:8090/playground>.

macOS / Linux:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8090 --env-file .env
```

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && ruff format --check .
python -m unittest discover -s tests -t . -v
```

The tests use a fake Meta API, so they need no key and use no credits. CI runs them on every push,
along with a startup check and a [gitleaks](https://github.com/gitleaks/gitleaks) secret scan.

## Hackathon build log

| Date | Built |
|---|---|
| Oct 27 | Service foundation: config, token auth, rate and concurrency limits, safe Meta client, `/health`, `/v1/ping-model`, `/playground`, 28 tests, CI |
| Oct 27 | Feature 1, relationship memory: Voice Transcribe (diarization), structured extraction, quote verification in code, privacy screen and redaction, judge pass, browser audio conversion and recording, playground results view, 49 tests |
| Oct 27 | First real run fixes: summary now written by the judge from verified facts only (a co-signing detail had leaked through the summary), co-signing and credit applications added to the privacy screen, one withheld entry per sentence, most serious category wins, 55 tests |
| Oct 27 | Real-run tuning from three debriefs: question-and-answer facts (the self-check reads each answer with the lines before it), bereavement added to the privacy screen, self-check switched to minimal reasoning to cut cost, safe facts kept from sentences that also hold a sensitive detail, 61 tests |
| Oct 27 | Second real run: transcription artifacts stripped, "he passed" added to bereavement, the withheld notice counts every redacted line, short answers show the question they answered, the summary hedges uncertain facts, 66 tests |
| Oct 27 | Feature 2, follow-up planner: cadence scheduling in code, drafts in the salesperson's voice, draft checks with safe replacements, life-event check-ins, playground timeline with copy buttons, favicon, 84 tests |
| Oct 27 | First real planner run: a named pet and its breed stay one animal (memory and planner), a short answer no longer shows a withheld question as its question, 87 tests |
| Oct 27 | Per-salesperson texting voice: optional voice samples with customer names swapped out, a check that drafts never borrow a sample customer, a texting-style box in the playground; extraction no longer turns a pronoun into a fact, 95 tests |
| Oct 27 | Voice variety: copy the salesperson's style without repeating whole sample sentences, signature phrases in at most two messages, thank-you cards written as cards, 96 tests |
| Oct 27 | Fact tags checked in code: a touchpoint only lists the facts its draft really mentions, 99 tests |
