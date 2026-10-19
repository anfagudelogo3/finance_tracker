"""First-time setup flow (Phase 6b) — design: docs/design/phase-6-users-onboarding.md.

A fixed, deterministic state machine, not an LLM chat. The state lives in the latest
user_prefs snapshot under "onboarding" (every step appends a row). Closed questions are
answered with numbered options matched in code; only open answers (business details, an
unknown currency, category customization) go to Claude, one forced tool call each.

Onboarding blocks everything else: until it's done or skipped, no message is recorded.
The user's very first message is held and offered back at the end, keeping its original
timestamp.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from finance_tracker.config import (
    CATEGORY_HINTS,
    CLAUDE_EXTRACTION_MODEL,
    DEFAULT_BUSINESS_EXPENSE_CATEGORIES,
    DEFAULT_BUSINESS_INCOME_CATEGORIES,
    DEFAULT_CURRENCY,
    DEFAULT_EXPENSE_CATEGORIES,
    DEFAULT_INCOME_CATEGORIES,
    KEYWORD_RESTART,
    KEYWORD_SKIP,
    KEYWORD_SKIP_ALL,
    MSG_ONBOARDING_CATEGORIES_SET,
    MSG_ONBOARDING_DONE,
    MSG_ONBOARDING_HELD,
    MSG_ONBOARDING_HELD_DISCARDED,
    MSG_ONBOARDING_HELD_IMAGE,
    MSG_ONBOARDING_Q_BUSINESS,
    MSG_ONBOARDING_Q_CATEGORIES,
    MSG_ONBOARDING_Q_CURRENCY,
    MSG_ONBOARDING_Q_CUSTOMIZE,
    MSG_ONBOARDING_Q_PROFILE,
    MSG_ONBOARDING_REMINDER,
    MSG_ONBOARDING_RESTART,
    MSG_ONBOARDING_SKIP_HINT,
    MSG_ONBOARDING_WELCOME,
    ONBOARDING_ABANDON_HOURS,
    ONBOARDING_CURRENCY_OPTIONS,
    ONBOARDING_MAX_CATEGORIES,
    TIMEZONE,
)
from finance_tracker.core.claude_client import client
from finance_tracker.core.text import normalize_answer
from finance_tracker.storage.database import append_user_prefs, set_user_categories

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(TIMEZONE)

# Step order. Restarts skip the profile steps (the profile type is fixed).
_STEPS = ["profile", "business", "currency", "expense_categories", "income_categories"]
_CUSTOM_STEPS = {
    "expense_categories_custom": "expense",
    "income_categories_custom": "income",
}

_KIND_LABELS = {
    "expense": {
        "icon": "🧾",
        "label": "gastos",
        "example": "quita ropa y agrega mascotas",
    },
    "income": {
        "icon": "💰",
        "label": "ingresos",
        "example": "agrega arriendos y quita regalo",
    },
}

# Closed answers, matched on normalized text (lowercase, no accents, no punctuation)
_OPTION_WORDS = {
    1: {"1", "uno", "la uno", "opcion 1", "primera", "la primera", "one"},
    2: {"2", "dos", "la dos", "opcion 2", "segunda", "la segunda", "two"},
    3: {"3", "tres", "la tres", "opcion 3", "tercera", "la tercera", "three"},
}
_PROFILE_WORDS = {
    "personal": {"personal", "personales", "finanzas personales", "para mi", "yo"},
    "business": {"negocio", "empresa", "emprendimiento", "mi negocio", "business"},
}
_YES_WORDS = {
    "si",
    "s",
    "dale",
    "claro",
    "ok",
    "listo",
    "yes",
    "de una",
    "si claro",
    "usar estas",
    "usar",
    "estas",
    "esas",
    "las de siempre",
    "mantener",
    "me sirven",
}
_NO_WORDS = {"no", "n", "nop", "nel", "no gracias"}
_CUSTOMIZE_WORDS = {
    "personalizar",
    "personalizarlas",
    "cambiar",
    "cambiarlas",
    "editar",
    "modificar",
}
_CURRENCY_WORDS = {
    "COP": {"cop", "pesos", "peso", "peso colombiano", "pesos colombianos"},
    "USD": {"usd", "dolar", "dolares", "dollar", "dollars", "us"},
    "EUR": {"eur", "euro", "euros"},
    "MXN": {"mxn", "peso mexicano", "pesos mexicanos"},
}


@dataclass
class OnboardingOutcome:
    """What the handler does with one message while onboarding is involved.

    handled=False: onboarding has nothing to do with this message; route it normally.
    replay: the held first message to process now, as if it had just arrived (but with
    its original message_id and timestamp).
    """

    replies: list[str]
    prefs: dict
    handled: bool = True
    replay: dict | None = None


@dataclass
class _Reply:
    texts: list[str] = field(default_factory=list)
    customize: bool = False  # the user chose "personalizar": ask what to change next


# ── Entry point ──────────────────────────────────────────────────────────────────


def handle(
    user_id: int, prefs: dict, text: str, message_type: str, message_id: int, now: str
) -> OnboardingOutcome:
    """Decide whether this message belongs to onboarding and advance it.

    `prefs` is the user's latest user_prefs snapshot; `now` the Bogota ISO timestamp of
    this message.
    """
    state = prefs.get("onboarding")
    norm = _norm(text)

    if norm == KEYWORD_RESTART:
        restart = (
            "profile" in prefs
        )  # the profile is fixed once the first onboarding set it
        held = (state or {}).get("held")
        return _start(user_id, prefs, now, restart=restart, held=held)

    if state is None:
        return _start(
            user_id,
            prefs,
            now,
            restart=False,
            held=_hold(text, message_type, message_id, now),
        )

    if state.get("status") in ("done", "skipped"):
        if state.get("step") == "confirm_held":
            return _resolve_held(user_id, prefs, state, norm)
        return OnboardingOutcome(replies=[], prefs=prefs, handled=False)

    if _is_abandoned(state, now):
        logger.info("Onboarding abandoned by user=%d, starting over", user_id)
        return _start(
            user_id,
            prefs,
            now,
            restart=state.get("restart", False),
            held=state.get("held"),
        )

    return _answer(user_id, prefs, state, text, norm, now)


# ── Start / finish ───────────────────────────────────────────────────────────────


def _start(
    user_id, prefs, now, *, restart: bool, held: dict | None
) -> OnboardingOutcome:
    draft = {"profile": prefs["profile"]} if restart else {}
    state = {
        "status": "in_progress",
        "restart": restart,
        "draft": draft,
        "held": held,
        "started_at": now,
        "updated_at": now,
    }
    state["step"] = _next_step(None, state)
    prefs = _save_state(user_id, prefs, state)
    intro = MSG_ONBOARDING_RESTART if restart else MSG_ONBOARDING_WELCOME
    logger.info("Onboarding started for user=%d (restart=%s)", user_id, restart)
    return OnboardingOutcome(replies=[intro, _question(state)], prefs=prefs)


def _finish(user_id, prefs, state, now, replies: list[str]) -> OnboardingOutcome:
    """Apply the answers (defaults for anything skipped) as a complete new configuration."""
    draft = state["draft"]
    profile = draft.get("profile") or {"type": "personal"}
    currency = draft.get("default_currency") or DEFAULT_CURRENCY
    source = "onboarding_restart" if state.get("restart") else "onboarding"

    categories = {}
    for kind in ("expense", "income"):
        names = draft.get(f"{kind}_categories") or _default_categories(profile, kind)
        renames = [tuple(pair) for pair in draft.get(f"{kind}_renames", [])]
        categories[kind] = set_user_categories(
            user_id, kind, names, source, renames=renames
        )

    held = state.get("held")
    onboarding = {
        "status": "skipped" if state.get("skipped_all") else "done",
        "completed_at": now,
        "restart": state.get("restart", False),
    }
    if held and held.get("text"):
        onboarding.update(step="confirm_held", held=held)

    # A complete new snapshot (not merged), so each run is one full, countable record
    snapshot = {
        "profile": profile,
        "default_currency": currency,
        "onboarding": onboarding,
    }
    append_user_prefs(user_id, snapshot, source=source, replace=True)
    logger.info("Onboarding %s for user=%d", onboarding["status"], user_id)

    replies = replies + [
        MSG_ONBOARDING_DONE.format(
            profile="Negocio" if profile["type"] == "business" else "Personal",
            currency=currency,
            expense_categories=", ".join(categories["expense"]),
            income_categories=", ".join(categories["income"]),
        )
    ]
    if held and held.get("text"):
        replies.append(MSG_ONBOARDING_HELD.format(text=held["text"]))
    elif held and held.get("message_type") == "image":
        replies.append(MSG_ONBOARDING_HELD_IMAGE)
    return OnboardingOutcome(replies=replies, prefs=snapshot)


def _resolve_held(user_id, prefs, state, norm) -> OnboardingOutcome:
    """The question after onboarding: "¿Lo registro?". Anything but yes/no cancels it
    and the message is processed normally."""
    held = state["held"]
    cleared = {k: v for k, v in state.items() if k not in ("step", "held")}
    prefs = _save_state(user_id, prefs, cleared, source="onboarding")
    if _option(norm) == 1 or norm in _YES_WORDS:
        logger.info(
            "Replaying held message id=%s for user=%d", held["message_id"], user_id
        )
        return OnboardingOutcome(replies=[], prefs=prefs, replay=held)
    if _option(norm) == 2 or norm in _NO_WORDS:
        return OnboardingOutcome(replies=[MSG_ONBOARDING_HELD_DISCARDED], prefs=prefs)
    return OnboardingOutcome(replies=[], prefs=prefs, handled=False)


# ── Answers ──────────────────────────────────────────────────────────────────────


def _answer(user_id, prefs, state, text, norm, now) -> OnboardingOutcome:
    step = state["step"]
    draft = dict(state["draft"])
    out = _Reply()

    if norm == KEYWORD_SKIP_ALL:
        state = {**state, "draft": draft, "skipped_all": True}
        return _finish(user_id, prefs, state, now, out.texts)

    if norm == KEYWORD_SKIP:
        _apply_default(step, draft)
        ok = True
    else:
        ok = _apply_answer(step, text, norm, draft, out)

    if not ok:
        reminder = f"{MSG_ONBOARDING_REMINDER}\n\n{_question(state)}"
        return OnboardingOutcome(replies=[reminder], prefs=prefs)

    state = {**state, "draft": draft, "updated_at": now}
    if out.customize:
        state["step"] = f"{step}_custom"
    else:
        state["step"] = _next_step(step, state)
    if state["step"] is None:
        return _finish(user_id, prefs, state, now, out.texts)

    prefs = _save_state(user_id, prefs, state)
    return OnboardingOutcome(replies=out.texts + [_question(state)], prefs=prefs)


def _apply_default(step: str, draft: dict) -> None:
    """`omitir`: store nothing for this question, so its default applies at the end."""
    if step == "profile":
        draft["profile"] = {"type": "personal"}


def _apply_answer(step, text, norm, draft, out: _Reply) -> bool:
    """Interpret one answer into `draft`. False when it isn't an answer to the question."""
    if step == "profile":
        choice = _option(norm)
        if choice == 1 or norm in _PROFILE_WORDS["personal"]:
            draft["profile"] = {"type": "personal"}
            return True
        if choice == 2 or norm in _PROFILE_WORDS["business"]:
            draft["profile"] = {"type": "business"}
            return True
        return False

    if step == "business":
        details = _interpret_business(text)
        if not details:
            return False
        draft["profile"] = {"type": "business", "business": details}
        return True

    if step == "currency":
        code = _match_currency(norm) or _interpret_currency(text)
        if not code:
            return False
        draft["default_currency"] = code
        return True

    if step in ("expense_categories", "income_categories"):
        kind = step.split("_")[0]
        choice = _option(norm)
        if choice == 1 or norm in _YES_WORDS:
            return True  # keep the defaults
        if choice == 2 or norm in _CUSTOMIZE_WORDS:
            out.customize = True
            return True
        return _customize(kind, text, draft, out)

    if step in _CUSTOM_STEPS:
        kind = _CUSTOM_STEPS[step]
        if _option(norm) == 1 or norm in _YES_WORDS:
            return True
        return _customize(kind, text, draft, out)

    return False


def _customize(kind: str, text: str, draft: dict, out: _Reply) -> bool:
    current = draft.get(f"{kind}_categories") or _default_categories(
        draft.get("profile") or {"type": "personal"}, kind
    )
    changes = _interpret_categories(kind, text, current)
    if not changes:
        return False
    names, renames = _apply_category_changes(current, changes)
    draft[f"{kind}_categories"] = names
    draft[f"{kind}_renames"] = draft.get(f"{kind}_renames", []) + [
        list(r) for r in renames
    ]
    out.texts.append(
        MSG_ONBOARDING_CATEGORIES_SET.format(
            label=_KIND_LABELS[kind]["label"], categories=", ".join(names)
        )
    )
    return True


def _apply_category_changes(
    current: list[str], changes: dict
) -> tuple[list[str], list]:
    """Apply {add, remove, rename} to a category list. 'otro' always stays."""
    names = list(current)
    keys = {_norm(n): n for n in names}
    renames = []
    for pair in changes.get("rename", []):
        old, new = keys.get(_norm(pair.get("from", ""))), _clean(pair.get("to", ""))
        if old and new and _norm(new) not in keys:
            names[names.index(old)] = new
            keys.pop(_norm(old))
            keys[_norm(new)] = new
            renames.append((old, new))
    for name in changes.get("remove", []):
        existing = keys.get(_norm(name))
        if existing and _norm(existing) != "otro":
            names.remove(existing)
            keys.pop(_norm(existing))
    for name in changes.get("add", []):
        name = _clean(name)
        if name and _norm(name) not in keys and len(names) < ONBOARDING_MAX_CATEGORIES:
            names.insert(-1 if "otro" in keys else len(names), name)
            keys[_norm(name)] = name
    if "otro" not in keys:
        names.append("otro")
    return names, renames


# ── Questions ────────────────────────────────────────────────────────────────────


def _next_step(current: str | None, state: dict) -> str | None:
    if current in _CUSTOM_STEPS:
        current = current.removesuffix("_custom")
    start = 0 if current is None else _STEPS.index(current) + 1
    for step in _STEPS[start:]:
        if step in ("profile", "business") and state.get("restart"):
            continue
        if (
            step == "business"
            and (state["draft"].get("profile") or {}).get("type") != "business"
        ):
            continue
        return step
    return None


def _question(state: dict) -> str:
    step = state["step"]
    draft = state["draft"]
    if step == "profile":
        text = MSG_ONBOARDING_Q_PROFILE
    elif step == "business":
        text = MSG_ONBOARDING_Q_BUSINESS
    elif step == "currency":
        text = MSG_ONBOARDING_Q_CURRENCY
    elif step in _CUSTOM_STEPS:
        kind = _CUSTOM_STEPS[step]
        text = MSG_ONBOARDING_Q_CUSTOMIZE.format(**_KIND_LABELS[kind])
    else:
        kind = step.split("_")[0]
        names = draft.get(f"{kind}_categories") or _default_categories(
            draft.get("profile") or {"type": "personal"}, kind
        )
        text = MSG_ONBOARDING_Q_CATEGORIES.format(
            categories="\n".join(_describe(name) for name in names),
            **_KIND_LABELS[kind],
        )
    return f"{text}\n\n{MSG_ONBOARDING_SKIP_HINT}"


def _describe(name: str) -> str:
    hint = CATEGORY_HINTS.get(name)
    return f"• *{name}* — {hint}" if hint else f"• *{name}*"


def _default_categories(profile: dict, kind: str) -> list[str]:
    business = profile.get("type") == "business"
    if kind == "income":
        return list(
            DEFAULT_BUSINESS_INCOME_CATEGORIES
            if business
            else DEFAULT_INCOME_CATEGORIES
        )
    return list(
        DEFAULT_BUSINESS_EXPENSE_CATEGORIES if business else DEFAULT_EXPENSE_CATEGORIES
    )


# ── Helpers ──────────────────────────────────────────────────────────────────────


def _norm(text: str | None) -> str:
    return normalize_answer(text)


def _clean(name: str) -> str:
    return " ".join((name or "").lower().split())[:50]


def _option(norm: str) -> int | None:
    for number, words in _OPTION_WORDS.items():
        if norm in words:
            return number
    return None


def _match_currency(norm: str) -> str | None:
    choice = _option(norm)
    if choice and choice <= len(ONBOARDING_CURRENCY_OPTIONS):
        return ONBOARDING_CURRENCY_OPTIONS[choice - 1]
    for code, words in _CURRENCY_WORDS.items():
        if norm in words:
            return code
    return None


def _hold(text: str, message_type: str, message_id: int, now: str) -> dict | None:
    """The first message is held for later if it could be a record: it mentions a number
    (an amount), or it's a photo. A plain greeting isn't held."""
    if message_type == "image":
        return {"message_type": "image", "message_id": message_id, "now": now}
    if text and re.search(r"\d", text):
        return {
            "text": text.strip(),
            "message_type": message_type,
            "message_id": message_id,
            "now": now,
        }
    return None


def _is_abandoned(state: dict, now: str) -> bool:
    try:
        updated = datetime.fromisoformat(state.get("updated_at") or "")
        current = datetime.fromisoformat(now)
    except ValueError:
        return False
    return current - updated > timedelta(hours=ONBOARDING_ABANDON_HOURS)


def _save_state(
    user_id: int, prefs: dict, state: dict, source: str = "onboarding"
) -> dict:
    append_user_prefs(user_id, {"onboarding": state}, source=source)
    return {**prefs, "onboarding": state}


# ── Claude: open answers only ────────────────────────────────────────────────────


def _call_tool(system: str, text: str, tool: dict) -> dict:
    response = client.messages.create(
        model=CLAUDE_EXTRACTION_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": text}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    block = next(b for b in response.content if b.type == "tool_use")
    logger.debug("Onboarding %s → %s", tool["name"], block.input)
    return block.input


def _interpret_business(text: str) -> dict | None:
    result = _call_tool(
        "The user is describing their business while setting up a finance assistant. "
        "Extract the details in Spanish, as the user said them. If the message is not a "
        "description of a business, set is_answer to false.",
        text,
        {
            "name": "business_details",
            "description": "The business details from the user's answer.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "is_answer": {"type": "boolean"},
                    "name": {
                        "type": ["string", "null"],
                        "description": "Business name, if given.",
                    },
                    "industry": {
                        "type": ["string", "null"],
                        "description": "What kind of business, e.g. restaurante.",
                    },
                    "sells": {
                        "type": ["string", "null"],
                        "description": "What it sells (its income).",
                    },
                    "main_costs": {
                        "type": ["string", "null"],
                        "description": "What it pays for (its expenses).",
                    },
                },
                "required": ["is_answer", "name", "industry", "sells", "main_costs"],
            },
        },
    )
    if not result.get("is_answer") or not (
        result.get("industry") or result.get("sells")
    ):
        return None
    return {key: result.get(key) for key in ("name", "industry", "sells", "main_costs")}


def _interpret_currency(text: str) -> str | None:
    result = _call_tool(
        "The user was asked for their main currency. Return its ISO 4217 code, or null if "
        "the message doesn't name a currency.",
        text,
        {
            "name": "currency",
            "description": "The currency the user named.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": ["string", "null"],
                        "description": "ISO 4217, e.g. MXN.",
                    }
                },
                "required": ["code"],
            },
        },
    )
    code = (result.get("code") or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _interpret_categories(kind: str, text: str, current: list[str]) -> dict | None:
    label = _KIND_LABELS[kind]["label"]
    result = _call_tool(
        f"The user is customizing their {label} categories ({kind} categories) in a finance "
        f"assistant. Current categories: {', '.join(current)}. Turn their message into "
        "category changes. Category names are short, lowercase Spanish nouns. Use the exact "
        "current names in 'remove' and 'rename.from'. If the message isn't about changing "
        "these categories, set is_answer to false.",
        text,
        {
            "name": "category_changes",
            "description": "Changes to the category list.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "is_answer": {"type": "boolean"},
                    "add": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                    "rename": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                            },
                            "required": ["from", "to"],
                        },
                    },
                },
                "required": ["is_answer", "add", "remove", "rename"],
            },
        },
    )
    if not result.get("is_answer"):
        return None
    if not (result.get("add") or result.get("remove") or result.get("rename")):
        return None
    return result
