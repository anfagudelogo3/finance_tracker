---
name: new-agent
description: Reusable pattern for adding a new specialist agent (income, reporting, onboarding, etc.) to this codebase's orchestrator architecture, extracted from expense_agent.py — the contract shape, Claude tool-use extraction, orchestrator wiring, test template, and eval parity comparison. Use when implementing a new src/*_agent.py module.
---

# Adding a new agent

This codebase has two specialist agents so far: `expense_agent.py` (Phase 1) and
`income_agent.py` (Phase 2) of `docs/agent-architecture.md`. This skill extracts the
pattern they established so `reporting_agent.py`, etc. follow it exactly instead of
reinventing shape or conventions per agent. `income_agent.py` is also the concrete
reference for what happens once a *second* agent exists — read it alongside
`expense_agent.py` wherever this skill says "once the second agent lands."

Read `docs/agent-architecture.md` first for the full architectural context (deviations
from the original design, what's shipped vs. planned). This skill is the tactical
"how do I build one" companion to that document.

## 1. The contract — `src/agent_types.py`

Every agent implements `handle(request: AgentRequest) -> AgentResponse`. Don't touch
`agent_types.py` to add agent-specific fields — the contract is deliberately generic and
reuses existing dict shapes instead of inventing parallel types:

```python
@dataclass
class AgentRequest:
    user_id: int
    message_id: int
    phone: str
    text: str
    media: list[dict]          # media.store_all_media() shape; [] for text/audio
    message_type: str          # "text" | "audio" | "image"
    now: str                   # ISO datetime, America/Bogota
    conversation: list[dict]   # database.load_recent_turns() shape; [] if unused


@dataclass
class AgentResponse:
    ok: bool
    reply_text: str
    reply_attachment: dict | None = None   # {"url": str, "filename": str}
    data: list[dict] | None = None         # persisted row(s), for logging
    error: str | None = None               # not populated by any agent yet — see §3
```

**No `categories` field — deliberately.** Phase 1 had one, populated by `handler.py`
before any routing decision. Phase 2 (income) exposed why that's wrong: which category
`kind` applies depends on which agent ends up handling the request, decided by
`orchestrator.py` *after* this object is already built. So each agent loads its own via
`database.get_user_categories(request.user_id, kind="<your_kind>")` — first line inside
`handle()`, not something the caller passes in. See `income_agent.handle()` for the real
example. Don't reintroduce a `categories` field on `AgentRequest` for a new agent; load it
yourself the same way.

## 2. Implementing the agent — pattern from `expense_agent.py`

**Model tier:** `claude-haiku-4-5-20251001` for narrow structured extraction (matches the
expense agent's tier — see `docs/agent-architecture.md` §2 for when Sonnet is warranted
instead, e.g. image/vision work). Add the model constant to `config.py` next to
`CLAUDE_EXTRACTION_MODEL`, don't hardcode it in the agent module.

**Extraction: forced tool-use, not prompt-described JSON.** This is the load-bearing
pattern — it's what closes the "malformed LLM response" gap that `parser.py`'s
`json.loads(content)` (zero error handling) has. For every new agent:

1. Define a tool with a JSON-schema `input_schema` matching exactly the fields you
   persist — no more, no less. If a field is dynamic per-user (categories, in expense's
   case), build the schema per-request with an `enum` constraint from the real data, the
   same way `expense_agent._build_expense_tool(categories)` does:
   ```python
   def _build_<x>_tool(categories: list[str]) -> dict:
       return {
           "name": "record_<x>",
           "description": "...",
           "input_schema": {
               "type": "object",
               "properties": {"<items>": {"type": "array", "items": {...}}},
               "required": [...],
           },
       }
   ```
2. Call with `tool_choice={"type": "tool", "name": "record_<x>"}` — never leave the model
   free to choose whether to call the tool or not; the response must always be
   schema-validated structured data, not prose you have to parse.
3. Pull the result straight from the response, no `json.loads`:
   ```python
   tool_use = next(block for block in response.content if block.type == "tool_use")
   return tool_use.input["<key>"]
   ```
4. Build the system prompt per-request via a `_build_system_prompt(categories)`-style
   function (not a module-level constant) if it needs to inject per-user data. Port the
   *domain rules* (field semantics, defaults, examples) from the equivalent OpenAI prompt
   in `parser.py` if one exists — the extraction logic doesn't change with the provider,
   only the transport.

**Reuse, don't reimplement.** Before writing any helper, check whether it already exists
and is provider-agnostic:
- Categories → `database.get_user_categories(request.user_id, kind="<your_kind>")`. Check
  `categories.kind` values already in use (`expense`, `income`) before assuming you need a
  new one, and check `config.py` for a matching `DEFAULT_<X>_CATEGORIES` list — Phase 0
  seeded both expense and income defaults already.
- Confidence scoring → `parser._estimate_confidence` (pure heuristic, no LLM involved) —
  reusable as long as your fields are a subset of what it checks (`amount`/`category`/
  `description`). `income_agent.py` reuses it unchanged despite having no
  `payment_method`/`merchant`.
- Persistence → the matching `database.py` function (`save_expense`, `save_income`, or add
  a sibling following the exact same signature convention: `(message_id, row: dict,
  user_id: int) -> int`, mutating `row` in place). **Check the target table's actual
  columns first** — don't assume expense's field set transfers. `incomes` has no
  `payment_method`/`merchant`; if your table has a `source`-named column, check what it
  actually means before writing `row["source"] = request.message_type` — `expenses.source`
  is the WhatsApp channel, but `incomes.source` is the money's origin
  (`manual`/`gmail`/`bank`), a real naming collision `income_agent.py` had to avoid by
  hardcoding `"manual"` instead.
- Reply formatting → the matching `whatsapp.py` formatter (`format_confirmation`,
  `format_income_confirmation`, or add a sibling). Pure string formatting is fine for an
  agent to import and call directly, even though agents shouldn't call
  `whatsapp.send_message`/`send_document` (the actual network send) — see §3.
- Date stamping → compute `date`/`expense_date`-equivalent in Python
  (`datetime.now(ZoneInfo("America/Bogota"))`), never ask the model for "today's date."

**Module-scope client:** import the shared singleton from `src/claude_client.py`
(`from claude_client import client`) — don't instantiate a new `anthropic.Anthropic(...)`
per agent.

**Error handling:** as shipped, `expense_agent.py` has zero internal try/except — API
errors, missing tool_use blocks, etc. all bubble up to `handler.py`'s top-level
`try/except` → `MSG_ERROR` → still-200 contract, exactly like every `parser.py` OpenAI call
does today. Match this unless a project-wide decision changes it (see
`docs/agent-architecture.md`'s deviation 4) — don't add ad-hoc try/except in a new agent
that no other agent has.

**`handle()` shape:**
```python
def handle(request: AgentRequest) -> AgentResponse:
    categories = get_user_categories(request.user_id, kind="<your_kind>")
    items = _extract(request.text, categories)   # or vision variant
    if not items:
        return AgentResponse(ok=True, reply_text=MSG_EMPTY_<X>, data=None)

    today = datetime.now(_BOGOTA).date().isoformat()
    for item in items:
        item["date"] = today
        item["confidence"] = _estimate_confidence(item)
        item["source"] = ...  # check what this column actually means on YOUR table first
        item["<row>_id"] = save_<x>(request.message_id, item, request.user_id)

    return AgentResponse(ok=True, reply_text=format_<x>_confirmation(items), data=items)
```

## 3. Wiring into `orchestrator.py`

Phase 1's `orchestrator.py` was single-agent dispatch, no routing decision at all. Phase 2
(income) is the real, concrete example of what happens once a second agent needs one —
copy this pattern, don't design a new one:

```python
# orchestrator.py, as of Phase 2
from parser import _normalize

_INCOME_KEYWORDS_PHRASE = [...]   # phrase-level, see the real list for why

def is_income_request(text: str) -> bool:
    normalized = _normalize(text)
    return any(kw in normalized for kw in _INCOME_KEYWORDS_PHRASE)

def handle_message(request: AgentRequest) -> AgentResponse:
    if is_income_request(request.text):
        return income_agent.handle(request)
    return expense_agent.handle(request)
```

**A routing signal has to come from somewhere. Options, cheapest first:**
1. **Deterministic/keyword signal**, if one exists — this is what income used.
   `is_income_request` lives in `orchestrator.py` itself (not `parser.py`): `parser.py`
   owns intent routing for what `handler.py` dispatches (`is_excel_request`/
   `is_report_request`, evaluated before the orchestrator is ever called);
   `orchestrator.py` now owns it for what *it* dispatches. Reuse `parser._normalize` (the
   accent-stripping helper) rather than duplicating it.
   - If the new domain's vocabulary can collide with an existing agent's (income vs.
     expense: "pagué" = I paid, vs. "me pagaron" = I was paid — shared stem), use
     phrase-level substring matching only, no fuzzy single-word matching like
     `is_report_request` uses — fuzzy matching on colliding stems produces false
     positives. Exclude bare generic phrases too ("pago de" alone matches both "pago de
     sueldo" [income] and "pago de arriendo" [expense]) in favor of specific compounds.
   - Accept that a keyword signal is heuristic, not exhaustive: an unmatched message just
     falls through to whatever agent handled it before yours existed — not a regression.
2. **A real classification call**, only once a deterministic signal genuinely can't
   distinguish the cases — income didn't need one. Per `docs/agent-architecture.md`
   deviation 1 and CLAUDE.md's cost-awareness constraint, don't add this speculatively —
   it's a new LLM call on every message that reaches the orchestrator. If you do add it,
   keep it a single cheap Haiku call producing a small enum, not per-agent classification
   calls.

Whichever it is, keep `handler.py` — not `orchestrator.py` — doing the actual
`whatsapp.send_message`/`send_document` call, matching the current deviation 3 (revisit
only when the orchestrator owns every branch, not one at a time).

## 4. Tests — template from `tests/test_expense_agent.py`

For the agent module itself, one test file per agent:

- Patch `<agent>.client` (the Claude call), `<agent>.get_user_categories` (categories are
  self-loaded now — see §1), and `<agent>.save_<x>` (persistence) — never hit real network
  or DB in these tests.
- A `_tool_use_response(items)` helper building a `MagicMock(content=[MagicMock(type=
  "tool_use", input={"<key>": items})])` — copy this verbatim, it mirrors the real SDK
  response shape.
- A `_request(text)` helper building a valid `AgentRequest` (no `categories` param — it's
  not a field; set the desired categories via the `get_user_categories` mock's
  `return_value` instead).
- Minimum coverage, mirroring `TestHandle` in `test_expense_agent.py`/`test_income_agent.py`:
  - simple single-item parse
  - a case exercising a field expense has that your domain might not (a nullable field,
    or — if your fields don't have one, like income's — a non-default category/currency
    case instead; adapt the slot to what's actually meaningful for your domain, don't
    force a nullable-field test that doesn't apply)
  - multiple items in one message
  - the empty-result fallback (`AgentResponse(ok=True, data=None, reply_text=MSG_EMPTY_...)`)
  - categories (or other per-user data) actually reaching the system prompt **and** the
    tool schema's `enum` — assert on `mock_client.messages.create.call_args`, assert
    `get_user_categories` was called with the right `kind`, and assert `tool_choice` is
    the forced form, not left optional.

For the orchestrator dispatch, extend `tests/test_orchestrator.py`'s pattern — patch the
new agent module, assert `handle_message` reaches it under the right routing condition.

For `handler.py` wiring, extend `tests/test_handler.py`: at minimum, a test that the new
message path reaches the orchestrator/agent and persists+replies, and a test that
`orchestrator.handle_message` is **not** called for message shapes that should route
elsewhere (mirrors `TestExcelAndReportBypassOrchestrator`). Keep the error-contract test
(`TestErrorContract`) and duplicate-message test (`TestDuplicateMessage`) passing — these
assert the pipeline-wide guarantees that must hold regardless of which agent handled the
message.

## 5. Eval parity — template from `evals/run.py`

Before merging a new agent, prove it's at least as accurate as whatever it's replacing (or
establish a fresh baseline if there's no prior path — income had no old-pipeline
equivalent, so `_run_income_agent` is a fresh baseline, not a parity comparison). Pattern
from `_run_scored_dataset`/`_run_parse_expense`/`_run_expense_agent`/`_run_income_agent`:

1. If an eval dataset already exists for the equivalent old-path function (e.g.
   `evals/datasets/parse_expense.json`), reuse it — don't build a new one just because the
   provider changed, the input/output shape is the same. Only add a new dataset
   (`evals/datasets/parse_income.json`, etc.) when the domain is genuinely new. Its
   `expected` blocks must still use the literal key `"expenses"` even for non-expense
   domains — `evals/scoring.py`'s `score_expense` hardcodes that key, and it's reused
   as-is (it only scores fields actually present in `expected`, so omitting
   `payment_method`/`merchant` for income "just worked," zero scoring-code changes).
2. Use the shared, side-effect-free `_run_scored_dataset(dataset_name, extract_fn)` helper
   — it takes any `extract_fn: (text) -> list[dict]` and any dataset name, so a new agent
   is one new call, not a new copy of the run/score/aggregate loop:
   ```python
   def _run_income_agent() -> tuple[list[dict], dict]:
       return _run_scored_dataset(
           "parse_income",
           lambda text: _extract_income_claude(text, DEFAULT_INCOME_CATEGORIES),
       )
   ```
3. Gate the new section on the relevant API key actually being real (see
   `has_anthropic_key` in `evals/run.py`) and **skip with a clear printed message** rather
   than failing `--check` outright when it's absent — a missing key shouldn't block
   developers running the rest of the suite.
4. **Do not pre-guess `THRESHOLDS[<agent>]`.** Run `uv run python -m evals.run` with a real
   key, read the printed metrics, then set the threshold with headroom below the observed
   number (see the comment above `THRESHOLDS` in `evals/run.py` for the exact reasoning —
   small sample sizes shouldn't make `--check` brittle). Confirm
   `uv run python -m evals.run --check` passes after adding the entry.
5. Print the new section side-by-side with the old path's section in the same run, so the
   comparison is visible in one invocation, not two separate commands.

## Checklist for a new `<x>_agent.py`

- [ ] `config.py`: add the Claude model constant if a new tier is needed
- [ ] `database.py`: `save_<x>` following `save_expense`'s signature convention, reusing
      the relevant existing table if one already exists (check `scripts/setup_db.sql` —
      e.g. `incomes` already exists from Phase 0, don't re-propose it)
- [ ] `whatsapp.py`: `format_<x>_confirmation` if the reply shape differs from expense's
- [ ] `<x>_agent.py`: tool schema + forced tool-use extraction, `handle()`, reusing
      `_estimate_confidence` and date-stamping in Python, zero internal try/except
- [ ] `orchestrator.py`: wire in dispatch — deterministic signal first, classification
      call only if genuinely needed (see §3)
- [ ] `handler.py`: route the relevant message path through the orchestrator; leave
      unrelated branches untouched
- [ ] `tests/test_<x>_agent.py`, extend `test_orchestrator.py` and `test_handler.py`
- [ ] `evals/run.py`: shared dataset-runner helper, `_run_<x>_agent`, key-gated section,
      threshold set from an observed live run — not guessed
- [ ] `docs/agent-architecture.md`: move the agent from "Planned" to "Shipped" in the
      module/model tables, note any deviations the way Phase 1's are documented
