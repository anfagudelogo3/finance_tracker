# Evaluation Framework

This document covers everything about evaluating the LLM calls in Finance Tracker:
what is evaluated, how the scoring works, how to run evals, and how to use Phoenix
to inspect traces. Read this top to bottom the first time; use the headings to jump
back to specific sections later.

---

## The four LLM calls and their eval strategy

Finance Tracker makes four different types of OpenAI calls. Each one has a different
evaluation approach based on how deterministic its output is.

| Call | Model | Input | Output | Eval strategy |
|------|-------|-------|--------|---------------|
| `parse_expense` | gpt-4o-mini | Free text | Structured expense JSON | Programmatic ✅ |
| `parse_report_request` | gpt-4o-mini | Date phrase + now | `{min_date, max_date}` | Programmatic ✅ |
| `parse_expense_from_image` | gpt-4o | Receipt image + caption | Structured expense JSON | LLM-as-judge (Phase 3) |
| `transcribe_audio` | whisper-1 | Voice note | Transcript string | WER (Phase 4) |

**Phases 1 and 2 are fully implemented.** Phases 3 and 4 are planned.

The reason `parse_expense` and `parse_report_request` can be evaluated
programmatically is that their outputs have a narrow, typed schema — numbers, fixed
category strings, ISO dates — where "correct" has an unambiguous definition. Image and
audio outputs are harder to compare mechanically, so they are deferred to later phases.

---

## Part 1 — Programmatic evals

### How the scoring works

The scoring logic lives in [`evals/scoring.py`](../evals/scoring.py). It is pure
Python — no network calls, no LLM calls — so it can be unit-tested offline.

#### Scoring `parse_expense`

The scorer receives the list of expense dicts returned by `parse_expense()` and
compares it against the expected values in the dataset entry.

**Rules per field:**

| Field | Comparison method | Notes |
|-------|------------------|-------|
| `amount` | Exact float match | `32000` and `32000.0` are equal |
| `currency` | Exact, case-insensitive | `"COP"` == `"cop"` |
| `category` | Exact, accent-insensitive | `"Comida"` == `"comida"` |
| `payment_method` | Exact, case-insensitive | `null` is scored if specified |
| `merchant` | Normalized (lowercase, strip accents) | `"Éxito"` == `"exito"` |
| `description` | **Not scored** | Deferred to Phase 3 LLM-as-judge |

**Important design decision:** only fields that are explicitly listed in the dataset
`expected` entry are scored. If you omit `merchant` from an expected entry, the scorer
ignores whatever the model returns for that field. This lets you write focused test
cases — for example, a case that only checks `amount` and `category` without caring
about `merchant`.

Including `"payment_method": null` in expected means "assert the model does not
invent a payment method here." Omitting `payment_method` entirely means "don't care."

For multi-expense messages, the scorer first checks that the count matches, then
scores each expense by index. If the count is wrong, the field-level scores are
skipped entirely.

#### Scoring `parse_report_request`

Both `min_date` and `max_date` are exact string comparisons (`YYYY-MM-DD`). Because
each dataset entry includes a fixed `now` timestamp, every answer is fully
deterministic — there is one correct answer and the model either gets it or it doesn't.

---

### The datasets

Datasets live in [`evals/datasets/`](../evals/datasets/). They are plain JSON files
you can edit in any text editor.

#### `parse_expense.json` — 15 cases

Covers the full range of real-world inputs you would actually send:

| Category | Cases |
|----------|-------|
| Simple text | `"almuerzo 32000"`, `"bus 2900"`, `"farmacia 18000"` |
| Colombian slang | `"uber 14 lukas"`, `"tinto 3 mil nequi"`, `"mercado 120 lukas efectivo"` |
| Payment methods | tarjeta, nequi, efectivo |
| Merchants | Uber, Netflix, ChatGPT |
| USD | `"suscripción ChatGPT 20 dólares"` |
| Multi-expense | `"almuerzo 20 luca y cine 40 mil"` |

#### `parse_report_request.json` — 12 cases

All anchored to `2026-06-25T10:00:00-05:00` (a Thursday in Colombia, UTC-5).

| Input | Expected result |
|-------|----------------|
| `"resumen"` (no qualifier) | First of current month → today |
| `"esta semana"` | Monday June 22 → today |
| `"la semana pasada"` | June 15 → June 21 |
| `"el mes pasado"` | May 1 → May 31 |
| `"últimos 7 días"` | June 18 → today |
| `"últimos 30 días"` | May 26 → today |
| `"mis gastos de mayo"` | May 1 → May 31 |
| `"gastos de abril"` | April 1 → April 30 |
| `"cuánto gasté hoy"` | Today → today |
| `"how much did I spend this week"` | June 22 → today |

#### Adding a new test case

Open the relevant JSON file and append an entry. The minimum required fields are `id`,
`input`, and `expected`.

For `parse_expense.json`:
```json
{
  "id": "my-new-case",
  "input": "tinto 2 luca efectivo",
  "expected": {
    "count": 1,
    "expenses": [
      {"amount": 2000, "currency": "COP", "category": "comida", "payment_method": "efectivo"}
    ]
  },
  "tags": ["text", "single", "slang"]
}
```

For `parse_report_request.json`:
```json
{
  "id": "my-date-case",
  "input": "últimos 15 días",
  "now": "2026-06-25T10:00:00-05:00",
  "expected": {"min_date": "2026-06-10", "max_date": "2026-06-25"},
  "notes": "15 days back from today"
}
```

---

### Running the evals

#### Prerequisites

**1. Install the eval dependencies** (only needed once):
```bash
uv sync --group evals
```

This installs `arize-phoenix` and `openinference-instrumentation-openai` into the
virtual environment. These packages are kept in a separate dependency group and are
never bundled into the Lambda ZIP — `deploy.sh` only installs the base project
dependencies.

**2. Set a real OpenAI API key in `.env`:**
```
OPENAI_API_KEY=sk-proj-your-real-key-here
```

The eval runner makes live API calls. It will fail immediately with an authentication
error if the key is a placeholder.

#### Running

```bash
uv run python -m evals.run
```

The runner:
1. Checks that `OPENAI_API_KEY` is set to a real value
2. Activates Phoenix tracing if `PHOENIX_TRACING=1` is in `.env`
3. Loads each dataset, calls the real parser, scores every result
4. Prints a metrics table with per-field accuracy bars
5. Lists every failure with the expected value and what the model actually returned

```bash
uv run python -m evals.run --check   # exit code 1 if any metric is below threshold
```

Use `--check` in CI to enforce minimum accuracy before deploying a prompt change.

#### Example output

```
Running evals against the live OpenAI API...

──────────────────────────────────────────────────────
  parse_expense  (n=15)
──────────────────────────────────────────────────────
  overall            ████████████████████  93%
  amount             ████████████████████  100%
  category           ███████████████████░  93%
  currency           ████████████████████  100%
  payment_method     ████████████████████  100%
  merchant           ████████████████████  100%

  Failures (1):
    [usd-subscription]
      category: expected='servicios'  got='educacion'

──────────────────────────────────────────────────────
  parse_report_request  (n=12)
──────────────────────────────────────────────────────
  overall            ████████████████████  100%
  min_date           ████████████████████  100%
  max_date           ████████████████████  100%

──────────────────────────────────────────────────────
Done.
```

#### Accuracy thresholds

These are enforced when you run with `--check`:

| Suite | Metric | Threshold |
|-------|--------|-----------|
| `parse_expense` | overall | 85% |
| `parse_expense` | amount | **95%** (critical — wrong amount = wrong data) |
| `parse_expense` | category | 85% |
| `parse_report_request` | overall | 90% |
| `parse_report_request` | min_date | 90% |
| `parse_report_request` | max_date | 90% |

#### Offline scoring tests (no API key needed)

The scoring functions themselves are unit-tested separately. These run as part of the
normal test suite and require no credentials or network access:

```bash
uv run pytest tests/test_eval_scoring.py -v
```

There are 19 tests covering exact match, wrong values, multi-expense scoring,
case/accent normalization, and date range scoring. If the scoring logic is broken,
these fail before you spend any API credits.

---

## Part 2 — Phoenix tracing

### What Phoenix does

Phoenix is an observability platform for LLM applications. Every time the app calls
OpenAI — whether from a real WhatsApp message or an eval run — Phoenix captures the
full span:

- The exact system prompt and user message sent to the model
- The raw response the model returned
- Model name, latency in milliseconds
- Input tokens, output tokens, estimated cost
- Timestamp and success/error status

All of this is stored in a local SQLite database and displayed in a web UI you can
browse at any time.

### How it is wired in

**[`src/tracing.py`](../src/tracing.py)** contains a single `setup_tracing()` function.
When called, it:

1. Checks whether `PHOENIX_TRACING=1` or `PHOENIX_COLLECTOR_ENDPOINT` is set in the
   environment. If neither is set, it returns immediately — nothing is imported, no
   overhead.
2. Imports `phoenix.otel.register` and `OpenAIInstrumentor` from the `evals` dependency
   group.
3. Registers an OpenTelemetry tracer provider pointing at the Phoenix collector endpoint.
4. Calls `OpenAIInstrumentor().instrument()`, which monkey-patches the OpenAI SDK at the
   class level. From that point on, every `client.chat.completions.create(...)` and
   `client.audio.transcriptions.create(...)` call anywhere in the codebase is
   automatically intercepted and traced — no changes needed in `parser.py`.

**[`src/handler.py`](../src/handler.py)** calls `setup_tracing()` at module load time,
before `parser.py` is imported. This ensures the OpenAI class is patched before the
`client = OpenAI(...)` instance is created.

**[`evals/run.py`](../evals/run.py)** also calls `setup_tracing()` before making any
API calls, so eval runs appear in Phoenix alongside real production traces.

Because `setup_tracing()` is a no-op when the env vars are absent, **the production
Lambda is completely unaffected** even though Phoenix is not installed in the Lambda
package. `deploy.sh` uses `uv pip install .` which only installs base project
dependencies, never the `evals` group.

### Trace persistence

Phoenix stores all traces in a SQLite database at:

```
~/.phoenix/phoenix.db
```

This file persists across server restarts, machine sleeps, and reboots. Stopping
Phoenix tonight and restarting it tomorrow shows all the same traces. The only way to
lose traces is to delete that file manually.

Every eval run and every real WhatsApp message (once the Lambda is pointed at Phoenix)
accumulates in the same `finance-tracker` project. Over time this builds a dataset of
real-world inputs you can use to catch regressions when you change a prompt.

---

### Starting Phoenix locally

#### Step 1 — Install eval dependencies (once)

```bash
uv sync --group evals
```

#### Step 2 — Start the server

```bash
uv run --group evals python scripts/start_phoenix.py
```

Leave this running in a dedicated terminal. You will see:

```
Starting Phoenix...
  UI     →  http://localhost:6006
  Traces →  http://localhost:6006/projects/finance-tracker/traces

Press Ctrl+C to stop.

🚀 Phoenix Server 🚀
  Phoenix UI: http://localhost:6006
  Log traces:
    - gRPC: http://localhost:4317
    - HTTP: http://localhost:6006/v1/traces
  Storage: sqlite:////Users/yourname/.phoenix/phoenix.db
```

#### Step 3 — Enable tracing in `.env`

These two lines should already be in your `.env`. If not, add them:

```
PHOENIX_TRACING=1
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces
```

#### Step 4 — Generate traces

In a second terminal, run the evals:

```bash
uv run python -m evals.run
```

27 OpenAI calls fire (15 expense parsing + 12 date range). Each one appears in Phoenix
in real time as the script runs.

#### Step 5 — Open the UI

```
http://localhost:6006/projects/finance-tracker/traces
```

The `finance-tracker` project is created automatically on first trace — it does not
need to be configured in advance. You will see a row per call. Click any row to see
the full prompt, response, latency, and token counts.

#### Troubleshooting: "port already in use" error

Phoenix uses two ports: `6006` (HTTP + UI) and `4317` (gRPC). If you try to start it
and see an address-in-use error, a previous Phoenix process is still running. Kill it:

```bash
lsof -ti tcp:6006 | xargs kill -9
lsof -ti tcp:4317 | xargs kill -9
```

Then run `scripts/start_phoenix.py` again. The script detects this automatically and
prints a clear error message with the exact commands to run before you see any
cryptic gRPC output.

---

### Sending Lambda traces to Phoenix (real WhatsApp messages)

The Lambda runs in AWS and cannot reach `localhost`. To see traces from real messages
you need Phoenix accessible from the internet. The practical approach for personal use
is **ngrok**.

#### Using ngrok (for testing sessions)

1. Install ngrok: [ngrok.com/download](https://ngrok.com/download)
2. Start Phoenix locally (Step 2 above)
3. In a third terminal:
   ```bash
   ngrok http 6006
   ```
   ngrok prints a public URL like `https://xxxx.ngrok-free.app`
4. In the Lambda environment variables (AWS Console → Lambda → Configuration →
   Environment variables), set:
   ```
   PHOENIX_TRACING=1
   PHOENIX_COLLECTOR_ENDPOINT=https://xxxx.ngrok-free.app/v1/traces
   ```
5. Send a WhatsApp message. It appears in your local Phoenix UI.

ngrok URLs change every time you restart it on the free plan, so this is best for
focused testing sessions rather than permanent monitoring.

---

## Roadmap

| Phase | Status | What it adds |
|-------|--------|-------------|
| Phase 1 — Programmatic evals | ✅ Done | Accuracy metrics for `parse_expense` and `parse_report_request` |
| Phase 2 — Phoenix tracing | ✅ Done | Full observability for every OpenAI call |
| Phase 3 — LLM-as-judge | Planned | `description` quality scoring; image expense evals |
| Phase 4 — Audio evals | Planned | Word Error Rate for `transcribe_audio` |
