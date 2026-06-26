# Open Challenges

Capabilities that are part of the long-term vision but are **deliberately deferred** —
they have real prerequisites (infrastructure, third-party approval, OAuth) that put them
beyond the current phased plan ([agentic-architecture.md](agentic-architecture.md)).
Documented here so they are not lost.

---

## Challenge 1 — Proactive scheduled budget alerts

**The goal:** the app messages you *unprompted* — e.g. a weekly batch that reads the
`expenses` / `incomes` / `budgets` tables and sends each user a summary and any
over-budget warnings.

**Why it's deferred — two hard prerequisites:**

1. **A separate execution path.** This does not run from the webhook. It needs a
   scheduler (AWS EventBridge cron) triggering a dedicated batch Lambda that iterates
   users, computes spend-vs-budget per currency (reusing `budgets.py`), and sends
   messages.

2. **The WhatsApp 24-hour window.** You can only send free-form WhatsApp messages within
   24 hours of the user's last message. A weekly batch is almost always *outside* that
   window, so it **must use pre-approved WhatsApp template messages**. The current Twilio
   **sandbox cannot do this at all** — it requires migrating to a registered WhatsApp
   Business sender (Twilio or Meta Cloud API) with approved templates.

**Interim approach (already in the plan):** reactive warnings — when an incoming message
reveals a budget was crossed, the warning is appended to the reply. This needs none of
the above and ships in Phase 3.

**What a future implementation looks like:**
```
EventBridge cron (weekly) → Batch Lambda
   → for each user: spend vs budget per currency (budgets.py)
   → build summary + alerts
   → send via approved WhatsApp template
```

**Prerequisites checklist:**
- [ ] Migrate off Twilio sandbox to a registered WhatsApp Business sender
- [ ] Design + get approval for summary/alert message templates
- [ ] EventBridge schedule + batch Lambda entrypoint (`batch_handler.py`)
- [ ] Reuse `budgets.py` so reactive and proactive share the same math

---

## Challenge 2 — Gmail / bank income extraction

**The goal:** the Income agent automatically pulls income (and possibly bank balances)
by reading bank notification emails in the user's Gmail, instead of relying only on the
user manually sending messages.

**Why it's deferred — this is the heaviest piece:**

- **OAuth & secure token storage.** Requires a Google OAuth consent flow per user and
  secure, refreshable token storage (AWS Secrets Manager / SSM, not the database in
  plaintext).
- **A polling/ingestion path.** Like the proactive alerts, this is scheduled work, not
  request-response — another EventBridge + Lambda track that polls Gmail.
- **Brittle parsing.** Bank email formats vary by bank and change over time; extraction
  needs its own eval dataset and ongoing maintenance.
- **Privacy surface.** Reading a user's email is a significant trust/permission
  escalation that deserves explicit, careful consent UX.

**Recommended sequencing:** only after Phases 0–3 are stable and the Income agent's
manual path is solid. Treat as its own multi-step project with a dedicated design doc.

**Prerequisites checklist:**
- [ ] Google Cloud project + OAuth credentials + verified consent screen
- [ ] Per-user token storage in Secrets Manager / SSM
- [ ] Scheduled Gmail polling Lambda
- [ ] Per-bank email parsers + eval dataset
- [ ] Explicit user consent + revocation flow
