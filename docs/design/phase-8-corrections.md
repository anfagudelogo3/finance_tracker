# Phase 8 — Conversational corrections: scope and decisions

Status: **scoped, not started**; every scoping question is decided (§2, §4). This document records the decisions taken while scoping
Phase 8 and the design that follows from them. The roadmap ([agent-roadmap.md](../agent-roadmap.md)
§7) summarizes it and links here.

## 1. Goal

Today a wrong amount, category or date stays wrong forever: the bot can only create records.
Phase 8 lets the user **edit, delete and undo** expenses and incomes from the chat, and
extends reports to **incomes and net cash flow**.

Ownership doesn't change (decided in Phase 6): corrections belong to the **expense and income
agents**, each for its own records. The users agent never touches transactions; the
reporting agent stays read-only.

## 2. Decisions

### 2.1 How a record is referenced — all three ways

| Reference | Example | How it's resolved |
|---|---|---|
| **Swipe-reply** | "eran 15000" replying to "💸 Gasto registrado: COP 25.000 en comida…" | Exact: the reply context (Phase 7a) already carries the record ids |
| **"The last one"** | "borra el último gasto", "el último ingreso era de 2 millones" | The user's most recent live record of that kind |
| **Description** | "el Uber de ayer", "el almuerzo del lunes" | A search over the user's recent records (amount, category, merchant, description, date) |

If a reference matches **more than one** record (one message created two expenses, or two
Ubers yesterday), the agent **asks which one**. Ask, don't assume (roadmap §4.5).

### 2.2 What can change

**Every field of the record**: amount, currency, category, date, description, payment method
and merchant (the last two for expenses).

**`created_at` never changes.** It's the moment the record was processed, kept for auditing.
Every edit gets its own timestamp in the edit log (§3.1).

**Kind changes** ("eso era un ingreso, no un gasto"): both agents can do their half. The
expense agent soft-deletes the expense and the income agent creates the income (or the
reverse). The two events are linked in the log so the move can be traced and undone as one.

### 2.3 Confirmation

| Action | Confirmation | Reply |
|---|---|---|
| Edit of an **exact** reference (swipe-reply) | **No**: applied directly | Shows what changed: "✏️ Gasto actualizado: COP 25.000 → COP 15.000 (comida, 2026-10-20)" |
| Edit of a **non-exact** reference ("el último", "el Uber de ayer") | **Yes, the target first**: "¿Es este? 💸 COP 14.000 en transporte (2026-10-21) → lo cambio a COP 12.000" | Same as above, after "sí" |
| Kind change ("eso era un ingreso") | **No** when the record is exact; target confirmed first otherwise | Shows both halves: removed as expense, recorded as income |
| Delete | **Yes**: "¿Borro el gasto COP 25.000 en comida del 2026-10-20?" | "🗑️ Gasto borrado: …" |
| Undo | **Always clarifies first**, naming exactly what it would undo: "¿Deshago la creación del gasto COP 20.000 en comida de hoy?" | What was undone |
| Bulk delete ("borra todos mis gastos") | **Declined**: the router refuses it | — |

**Rule: ask until sure.** Whenever the target isn't certain — a non-exact reference, an undo,
or more than one candidate — the agent states the exact record (or action) it found and
executes only after the user confirms it. There's no fixed "how far back" limit: the agent
proposes, the user confirms. Only a swipe-reply is exact enough to edit without asking.

Confirmations reuse the Phase 6c mechanism: the pending action is stored in `user_prefs`
(`pending_action`) with its question and `asked_at`, and `handler.py` settles it on the very
next message before routing: "sí" applies it with no LLM call; anything else cancels it;
older than `PENDING_ACTION_MINUTES` doesn't count.

### 2.4 Deletes are soft

`deleted_at` (already on `expenses` and `incomes`) is set; rows are never removed. Every read
already filters `deleted_at IS NULL` (reports, Excel, category counts, reply context), and
Phase 8 keeps that rule for every new query.

### 2.5 History: an append-only edit log

Every create, edit, delete, undo and kind change is one row in a new **`record_events`**
table, never updated or deleted. It holds the record (kind + id), the action, the **values
before and after**, the source, the inbound message that caused it, and the timestamp. It's
the audit trail, and it's what makes undo possible: undo applies an event's "before" values.

### 2.6 Reports: expenses, incomes, then net

Reports get three parts, per currency: an **expenses** section (as today, by category), an
**incomes** section (by category), and the **net** (incomes − expenses) at the end. Excel
gets incomes too (a second sheet or a type column, decided during implementation).

## 3. Design

### 3.1 Data

```sql
CREATE TABLE IF NOT EXISTS record_events (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    record_kind   VARCHAR(10) NOT NULL,      -- 'expense' | 'income'
    record_id     INTEGER NOT NULL,
    action        VARCHAR(20) NOT NULL,      -- create | update | delete | undo | move
    before        JSONB,                     -- field values before (NULL for create)
    after         JSONB,                     -- field values after (NULL for delete)
    message_id    INTEGER REFERENCES messages(id) ON DELETE SET NULL,  -- what caused it
    related_event INTEGER REFERENCES record_events(id),  -- the other half of a move; the event an undo reverses
    source        VARCHAR(20) NOT NULL,      -- expense_agent | income_agent | undo
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

- Creating a record also writes a `create` event, so "deshaz eso" right after a new
  record works the same way as after an edit.
- **Bot messages → the records they changed.** Phase 7a links a bot confirmation to the
  records its inbound message *created*. A correction confirmation creates nothing; it
  changes an existing record. So `record_events.message_id` plus the bot turn's `message_id`
  make the link: a swipe-reply to "✏️ Gasto actualizado…" resolves to the record that
  message's events touched. `reply_context` gets that second lookup.
- The database is rebuilt (test data only), so there are no migrations.

### 3.2 Agents become multi-step, without slowing down plain records

The expense and income agents move from one forced tool call to `run_tool_loop`, with tools:

| Tool | Does |
|---|---|
| `record_expenses` / `record_incomes` | Create (today's tool) |
| `find_records` | Search the user's live records of this kind (last N, by text, amount, date range) |
| `update_record` | Change fields of one record by id; logs `update` |
| `delete_record` | Stores a pending delete and returns the confirmation question (code-written) |
| `undo` | Finds the user's latest event(s) of this kind and stores a pending undo with the clarification question |

**Latency guard:** a loop normally costs a second Claude call after the tool runs. For the
common case, a new record with nothing else, the loop ends **in code** right after
`record_*` succeeds ("terminal tool"), so plain expenses stay one call, as today. Only
corrections pay for the extra turn.

`user_id` is injected by the executor and record ids are always checked against it, so a
record id from the model can never reach another user's data.

### 3.3 Routing

- The router keeps delegating by kind: "no, eran 15000" replying to an expense goes to
  `delegate_to_expenses`; "eso era un ingreso" goes to **both** (expenses to remove, incomes
  to create).
- The `<replied_to>` block already carries record ids; the router passes them in the task
  ("corrige el gasto #12: el monto es 15000").
- The keyword fast path can keep sending short expense messages to the expense agent: the
  agent now handles corrections too.
- `adv-delete-all` stays a decline. The skipped `fu-correccion` and `reply-correction`
  routing cases get enabled.

### 3.4 Evals

A new **`corrections.json`** dataset, run against the agents with the database stubbed (like
`users_agent.json`): message (+ reply context or conversation) → expected action (`update`,
`delete`, `undo`, `move`, `ask`), target record id and final values, and whether a
confirmation was asked. Cases cover the three reference styles, ambiguity (two matches →
ask), every editable field, kind changes, deletes, undo, and bulk delete. Reports get unit
tests for the expenses / incomes / net layout.

## 4. Decided (were open questions)

1. **How far back "the last one" and undo reach:** no fixed limit — **always clarify and
   execute only when sure** (§2.3). The agent names the exact record or action and waits for
   "sí"; a swipe-reply is the only reference exact enough to edit without asking.
2. **An undo can't be undone.** Undo writes an `undo` event; "deshaz eso" never targets an
   `undo` event (it clarifies which earlier action is meant, if any).
3. **A kind change needs no confirmation** when the record is exact: nothing is lost, and the
   reply shows both halves. With a non-exact reference the target is confirmed first, like
   any edit.
4. **Description search window: the last 60 days.** Older records need a date in the message
   ("el Uber del 3 de agosto").
5. **Excel:** a second sheet "Ingresos" and a summary sheet with totals and the net, per
   currency.

## 5. Delivery

| PR | Content |
|---|---|
| **8a — Edit log + reports** | `record_events` (create events from day one), reports and Excel with incomes and net. No new chat capability yet. |
| **8b — Edit and delete** | Agents on `run_tool_loop` with the terminal-tool guard; `find_records`, `update_record`, `delete_record` (with confirmation); reply-context link for correction confirmations; `corrections.json`. |
| **8c — Undo and kind changes** | `undo` with clarification; `move` between expense and income; routing cases enabled. |

Each PR: tests and the matching evals green, docs updated, and the live routing eval rerun
whenever a prompt changes.
