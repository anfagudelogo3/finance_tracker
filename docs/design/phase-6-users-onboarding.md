# Phase 6 — Users agent + onboarding: scope and decisions

Status: **scoped, in progress** (`feature/phase-6-users-onboarding`). This document records every
decision taken while scoping Phase 6 and why. The roadmap ([agent-roadmap.md](../agent-roadmap.md)
§5 and §7) summarizes it and links here.

## 1. What the users agent is for

The users agent handles everything about **the user and their settings**: onboarding, business
details, main currency, categories, and help ("¿qué puedes hacer?"). It does **not** touch
transactions.

| Owner | Responsibility |
|---|---|
| Users agent (`agents/users.py`) | Profile details, currency, categories, help |
| Onboarding (`agents/onboarding.py`) | First-time setup flow, restart |
| Expense / income agents | Creating **and later correcting** their own records |
| Reporting agent | Read-only |

**Corrections stay with the expense and income agents** (decided). "No, eran 15000" or "borra el
último gasto" are about one transaction, and those agents already own the fields, category list,
validation and confirmation format. The router already sends them by domain. Corrections remain a
later phase (roadmap Phase 8), not part of Phase 6.

## 2. Onboarding

### 2.1 When it runs

- On a user's **very first message, before anything else**, whatever that message says. It is
  **blocking**: nothing is recorded until onboarding is finished or skipped.
  *Why:* the user may change the default categories during onboarding. Recording the first
  expense before that could classify it with a category the user is about to remove.
- **The first message is held, not lost.** When onboarding ends, the bot offers it back:
  *"Antes de empezar me escribiste 'almuerzo 20000'. ¿Lo registro?"*. If accepted, the record keeps
  the **original message date**. Only the first message is held. Messages sent during onboarding
  aren't held, and the held message survives an onboarding restart.
- **No existing users to migrate:** the database is rebuilt from scratch after Phase 6, so every
  user goes through onboarding.

### 2.2 Flow

1. **Welcome message** (approved draft, §2.6), sent as its own message.
2. **Personal or business**, as numbered options with a short description of each.
   If business: name, industry, what it sells, its main costs.
3. **Main currency.**
4. **Expense categories:** the defaults are listed and described; the user keeps them or
   customizes them.
5. **Income categories:** the same.

Each question is a separate message. WhatsApp splits messages over ~1,600 characters.

**Language: Spanish only in this version.** There's no language question and no language
setting. An English option was discussed and dropped. It would need every `MSG_*` string,
onboarding text and agent prompt in both languages, English category names, and English keyword
fast paths.

### 2.3 Answers: closed first, open only when needed

- Answers can be **text or voice notes**. Voice notes are transcribed with Whisper and then take the
  same path as text.
- Closed questions use **numbered options**. Code matches them without an LLM call: the digit
  ("1"), the number spoken ("uno", "la uno") or the option name ("personal", "negocio").
- Only open answers go to the LLM: business details, a currency that doesn't match a known code,
  and category customization ("quita ropa, agrega mascotas"). Example: the income-categories
  question first asks "¿usas las de siempre?". "Sí" / "1" needs no LLM call. Anything else is
  interpreted by the LLM.
- Cost: a handful of Haiku calls per onboarding, once per user.

### 2.4 Skipping, off-topic messages, abandonment, restart

| Situation | Behavior |
|---|---|
| `omitir` | Skips **the current question only**. Nothing is stored for it, so the default applies: personal, COP, default categories. Skipping the profile question also skips the business details. |
| `omitir todo` | Skips every remaining question with its default. |
| Off-topic or finance message mid-onboarding | The reminder *"Para empezar necesito terminar tu configuración (o escribe omitir)."* followed by the current question again. No LLM call for this; nothing is recorded. |
| Abandoned (no answer for `ONBOARDING_ABANDON_HOURS` = 24 h) | Onboarding starts **over from the beginning**; the held message is kept. |
| `reiniciar configuración` (must be the whole message, accents optional) | Reruns onboarding **without the profile questions** (type and business details): the profile is fixed from the first onboarding; business details are edited through the users agent. Saves a **complete new snapshot** in `user_prefs` (`source="onboarding_restart"`), rather than merging into the last one, so restarts can be counted. |

Single changes after onboarding don't need a restart: "agrega la categoría mascotas" goes through
the router to the users agent and changes only that.

### 2.5 Default categories

| Profile | Expense | Income |
|---|---|---|
| Personal | comida, transporte, mercado, salud, entretenimiento, hogar, educacion, ropa, servicios, otro | salario, freelance, negocio, inversion, reembolso, regalo, otro |
| Business | insumos, nómina, arriendo, servicios públicos, impuestos, marketing, transporte, mantenimiento, otro | ventas, servicios, otro |

The business lists are **fixed defaults**. Suggesting categories from the business's "main costs"
answer was discussed and left for later: it would cost an extra LLM call.

### 2.6 Welcome message (approved)

> 👋 ¡Hola! Soy tu asistente financiero personal, aquí mismo en WhatsApp.
>
> Olvídate de hojas de cálculo y apps complicadas: solo escríbeme o mándame una nota de voz como le
> hablarías a un amigo —*"almuerzo 25 mil"*, *"me pagaron la quincena"*— y yo me encargo de
> organizarlo todo. 💸
>
> Con mi ayuda vas a:
> ✅ Saber exactamente en qué se va tu dinero
> ✅ Registrar gastos e ingresos en segundos, por texto, voz o foto del recibo
> ✅ Pedir resúmenes cuando quieras: *"¿cuánto gasté este mes?"*
> ✅ Descargar tus movimientos en Excel
>
> Tomar el control de tus finanzas nunca fue tan fácil. Antes de empezar, te haré unas preguntas
> rápidas para adaptarme a ti (menos de 2 minutos). 🚀

### 2.7 As built (6b)

- `agents/onboarding.py::handle(user_id, prefs, text, message_type, message_id, now)` returns an
  `OnboardingOutcome(replies, prefs, handled, replay)`. `handler.py` calls it after
  transcribing voice notes and before any routing. `handled=False` means "route normally".
- **State:** `user_prefs.onboarding` = `{status, step, restart, draft, held, started_at,
  updated_at}`. Every answered step appends a `user_prefs` row (`source="onboarding"`); answers
  collect in `draft` and only become `profile` / `default_currency` / categories at the end.
  The final row is a **complete snapshot** (`append_user_prefs(..., replace=True)`).
- **What's held:** the first message only if it could be a record: it contains a digit (an
  amount), or it's a photo. A greeting isn't held. A held photo can't be replayed (its bytes
  aren't kept), so the user is asked to send it again.
- **Replay:** after "sí", `handler._route` processes the held text with its original
  `message_id` and Bogota timestamp; the expense and income agents now take the record date
  from `request.now` (Bogota) rather than "today", so the record keeps the original date.
- **Categories:** applied by `database.set_user_categories` in one transaction: renames keep
  the row, missing names are added or reactivated, others are deactivated; each change is a
  `category_events` row. New users are still seeded with the personal defaults at creation,
  so a business onboarding shows up in `category_events` as seed adds followed by deactivations.
- **Main currency** is passed to the expense and income agents (`AgentRequest.default_currency`)
  as the currency to assume when a message doesn't name one.
- **Closed matching:** digits, spoken numbers ("uno", "la dos"), option names, yes/no words,
  currency names (pesos, dólares, euros…), all accent- and punctuation-insensitive.
- **Claude calls** (`CLAUDE_EXTRACTION_MODEL`, forced tool): business details, an unknown
  currency, category customization. Each returns `is_answer: false` for unrelated text, which
  produces the reminder.
- **Timezone:** every timestamp is `America/Bogota` (`config.TIMEZONE`), including the
  database session (`get_connection` sets `timezone`), so SQL defaults agree with the code.

## 3. Users agent capabilities

| Area | Actions | Confirmation |
|---|---|---|
| Profile type | **Not changeable.** Fixed at the first onboarding; requests get a polite decline. | — |
| Business details | Edit name, industry, what it sells, main costs | No |
| Main currency | Change it. Affects only future records (each record stores its own currency). | **Yes** |
| Categories | Add | No |
| | Rename. Past records follow, because they reference the category by id. | **Yes** if it has records |
| | Rename onto an existing name = **merge**. Both categories' records end up under one. | **Yes** |
| | Deactivate ("borrar"). Never deleted. | **Yes** if it has records |
| | Reactivate: adding a name that exists but is deactivated brings it back with its history. | No |
| Help | "¿Qué puedes hacer?", how-to questions, list my categories / settings | — |
| Out of scope (polite decline) | Delete account or data, budgets, language change | — |

### 3.1 Destructive changes apply to the future; history stays

| Operation | Past records | Future |
|---|---|---|
| Rename | Show the new name. It's the same category with a better label, so reports don't split it. | New name |
| Merge | Moved into the target category (confirmed) | Target only |
| Deactivate | Unchanged. Reports keep showing the old category for those periods. | The agents can't pick it |
| Currency change | Unchanged | New default when a message doesn't name one |

### 3.2 Confirmation

The agent asks first, e.g. *"La categoría 'ropa' tiene 12 gastos. ¿La desactivo?"*. A pending
confirmation is valid **only for the very next message**: anything other than a "sí" cancels it,
so a stale "sí" can't confirm something the user has forgotten.

### 3.3 As built (6c)

- `agents/users.py::handle` runs `core/agent_loop.run_tool_loop` on `claude-haiku-5-5`
  (effort low) with six tools: `get_settings`, `add_category`, `rename_category`,
  `deactivate_category`, `set_default_currency`, `update_business_details`. `user_id` is
  injected by the executor; no tool schema has one. The agent writes its own reply (it never
  handles amounts); confirmation questions are written in code.
- **Confirmation is deterministic.** A change that needs it doesn't run: the tool stores
  `pending_action` (with the question and `asked_at`) in `user_prefs`. On the next message
  `handler.py` calls `users.resolve_pending` before routing: a "sí" (or "1", "dale"…) applies
  it with no LLM call; "no" replies "Listo, no hice ningún cambio."; anything else cancels it
  silently and the message is routed normally. A question older than
  `PENDING_ACTION_MINUTES` (30) no longer counts. One confirmation per message.
- Needs confirmation: currency change (always), merge (always), rename or deactivate of a
  category with records. Without records they apply at once. `otro` is protected.
- Category changes go through `database.change_category` (one transaction + a
  `category_events` row); record counts come from `database.get_category_overview`.
- **Router:** new `delegate_to_users` tool. Help questions ("qué puedes hacer") now go to
  the users agent instead of the router's own small-talk reply; greetings and thanks stay with
  the router. "Create a category and record in it" delegates to users first, then expenses.
  Deleting records or the account is still declined by the router.
- **Latency:** the users agent has its own 8 s budget (`USERS_TIMEOUT_SECONDS`) because it
  runs inside the router's 12 s loop. Measured alone: p50 3.5 s, max 5.7 s. A settings message
  is therefore the slowest path (router + users agent + router's closing call); watch it in
  Phoenix.
- **Evals:** `evals/datasets/users_agent.json` (18 cases, 100%) and the routing dataset's
  users cases enabled (66 scored, 100% router and hybrid).

## 4. Data model

### 4.1 Category ids instead of text (6a)

`expenses` and `incomes` reference `categories(id)` through a new `category_id` column. Reports and
Excel read the name through that id, so a rename is a single-row change on `categories`.

- **No migration:** the production database is rebuilt from scratch with `scripts/setup_db.sql`
  once Phase 6 is in place (decided), so the schema change goes straight into the `CREATE TABLE`
  statements.
- The agents still pick a category **by name** (the model sees names). `database.save_expense` /
  `save_income` resolve the name to the user's id, falling back to `otro`.
- The old `category` text column is removed.

### 4.2 Category history: `category_events` (append-only)

`categories` stays the source of truth the agents read. Every change also inserts one row in
`category_events`: who, which category, `action` (`add`, `rename`, `merge`, `deactivate`,
`reactivate`), old and new name, records affected, `source` (`seed`, `onboarding`,
`users_agent`), and timestamp. Never updated.

*Why not snapshot categories inside `user_prefs`:* that copy could drift from the real table. A
dedicated log directly answers "how many times did this user edit categories?" and "what was
this category called in March?".

### 4.3 Preferences

`user_prefs` stays append-only (latest row wins). Keys used in Phase 6:

```jsonc
{
  "profile": {"type": "business", "business": {"name": "...", "industry": "...", "sells": "...", "main_costs": "..."}},
  "default_currency": "COP",
  "onboarding": {"status": "in_progress" | "done" | "skipped", "step": "currency", "completed_at": "...", "pending_message_id": 123}
}
```

`source` distinguishes `onboarding`, `onboarding_restart` and `users_agent`.

## 5. Delivery: three PRs

| PR | Content | Behavior change |
|---|---|---|
| **6a — Category ids** | `category_id` columns (no migration: DB rebuilt), `category_events`, save/read by id, business default categories in `config.py` | None (refactor; current tests and evals must stay green) |
| **6b — Onboarding** | Onboarding flow, held first message, `omitir` / `omitir todo` / `reiniciar configuración`, onboarding state in `user_prefs`, profile-based category seeding | New users only |
| **6c — Users agent** | `agents/users.py`, `delegate_to_users`, confirmation flow, help, enable the `skip:"phase-6"` routing cases, users-agent eval dataset | New capabilities |

**Tests:** onboarding is a deterministic state machine with unit tests and no LLM. The users agent
gets mocked tool-loop tests and an eval dataset mapping a message to the expected action and
arguments, e.g. "renombra comida a alimentación" → `rename_category`.

## 6. Deferred (explicitly out of Phase 6)

- Expense/income corrections, edits and deletes (Phase 8, in their own agents)
- English or other languages
- Changing the profile type; personal and business books in one account
- Business category suggestions from the onboarding answers
- Budgets; account/data deletion
- Phoenix trace review (deferred until the full feature set exists)
