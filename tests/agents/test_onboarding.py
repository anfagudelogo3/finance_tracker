from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.agents import onboarding
from finance_tracker.config import (
    DEFAULT_BUSINESS_EXPENSE_CATEGORIES,
    DEFAULT_EXPENSE_CATEGORIES,
    DEFAULT_INCOME_CATEGORIES,
    MSG_ONBOARDING_HELD_DISCARDED,
    MSG_ONBOARDING_REMINDER,
    MSG_ONBOARDING_RESTART,
    MSG_ONBOARDING_WELCOME,
)

NOW = "2026-10-20T09:00:00-05:00"


@pytest.fixture
def db():
    """The two database writers onboarding uses, recorded instead of executed."""
    with (
        patch.object(onboarding, "append_user_prefs") as append,
        patch.object(
            onboarding,
            "set_user_categories",
            side_effect=lambda u, k, names, s, renames=None: names,
        ) as set_categories,
    ):
        yield MagicMock(append=append, set_categories=set_categories)


@pytest.fixture
def claude():
    with patch.object(onboarding, "client") as client:
        yield client


def _tool_result(claude, payload: dict) -> None:
    block = MagicMock(type="tool_use", input=payload)
    claude.messages.create.return_value = MagicMock(content=[block])


def _send(prefs, text, message_type="text", message_id=1, now=NOW):
    return onboarding.handle(7, prefs, text, message_type, message_id, now)


def _walk(prefs, *answers):
    """Send several answers in a row, returning the last outcome."""
    outcome = None
    for answer in answers:
        outcome = _send(prefs, answer)
        prefs = outcome.prefs
    return outcome


class TestStart:
    def test_first_message_starts_onboarding_and_is_held(self, db):
        outcome = _send({}, "almuerzo 20000", message_id=41)

        assert outcome.handled
        assert outcome.replies[0] == MSG_ONBOARDING_WELCOME
        assert "Personal" in outcome.replies[1] and "Negocio" in outcome.replies[1]
        state = outcome.prefs["onboarding"]
        assert state["status"] == "in_progress" and state["step"] == "profile"
        assert state["held"] == {
            "text": "almuerzo 20000",
            "message_type": "text",
            "message_id": 41,
            "now": NOW,
        }
        db.append.assert_called_once()

    def test_greeting_is_not_held(self, db):
        outcome = _send({}, "hola")
        assert outcome.prefs["onboarding"]["held"] is None

    def test_first_photo_is_remembered_but_not_replayable(self, db):
        outcome = _send({}, "", message_type="image")
        assert outcome.prefs["onboarding"]["held"]["message_type"] == "image"

    def test_onboarded_user_is_not_handled(self, db):
        outcome = _send({"onboarding": {"status": "done"}}, "almuerzo 20000")
        assert not outcome.handled
        db.append.assert_not_called()


class TestClosedAnswers:
    def test_personal_skips_business_question(self, db):
        outcome = _walk({}, "hola", "1")
        assert outcome.prefs["onboarding"]["step"] == "currency"

    def test_business_asks_for_details(self, db):
        outcome = _walk({}, "hola", "negocio")
        assert outcome.prefs["onboarding"]["step"] == "business"

    def test_spoken_number_and_accents_match(self, db):
        outcome = _walk({}, "hola", "La uno.")
        assert outcome.prefs["onboarding"]["draft"]["profile"] == {"type": "personal"}

    def test_unrelated_message_repeats_the_question(self, db):
        prefs = _send({}, "hola").prefs
        db.append.reset_mock()

        outcome = _send(prefs, "almuerzo 5000")

        assert outcome.replies[0].startswith(MSG_ONBOARDING_REMINDER)
        assert "Personal" in outcome.replies[0]
        assert outcome.prefs == prefs  # nothing advanced, nothing written
        db.append.assert_not_called()

    @pytest.mark.parametrize(
        "answer,code",
        [("1", "COP"), ("pesos", "COP"), ("3", "EUR"), ("Dólares", "USD")],
    )
    def test_currency_without_llm(self, db, claude, answer, code):
        outcome = _walk({}, "hola", "1", answer)
        assert outcome.prefs["onboarding"]["draft"]["default_currency"] == code
        claude.messages.create.assert_not_called()

    def test_unknown_currency_asks_claude(self, db, claude):
        _tool_result(claude, {"code": "pen"})
        outcome = _walk({}, "hola", "1", "soles peruanos")
        assert outcome.prefs["onboarding"]["draft"]["default_currency"] == "PEN"


class TestOpenAnswers:
    def test_business_details_are_interpreted(self, db, claude):
        _tool_result(
            claude,
            {
                "is_answer": True,
                "name": "La Sazón",
                "industry": "restaurante",
                "sells": "almuerzos",
                "main_costs": "insumos y nómina",
            },
        )
        outcome = _walk({}, "hola", "2", "La Sazón, restaurante, vendo almuerzos")

        profile = outcome.prefs["onboarding"]["draft"]["profile"]
        assert profile["type"] == "business"
        assert profile["business"]["industry"] == "restaurante"
        assert outcome.prefs["onboarding"]["step"] == "currency"

    def test_business_categories_are_offered_to_businesses(self, db, claude):
        _tool_result(
            claude,
            {
                "is_answer": True,
                "name": None,
                "industry": "panadería",
                "sells": "pan",
                "main_costs": "harina",
            },
        )
        outcome = _walk({}, "hola", "2", "una panadería", "1")
        assert "insumos" in outcome.replies[-1] and "nómina" in outcome.replies[-1]

    def test_not_a_business_description_repeats_the_question(self, db, claude):
        _tool_result(
            claude,
            {
                "is_answer": False,
                "name": None,
                "industry": None,
                "sells": None,
                "main_costs": None,
            },
        )
        outcome = _walk({}, "hola", "2", "qué es esto?")
        assert outcome.replies[0].startswith(MSG_ONBOARDING_REMINDER)
        assert outcome.prefs["onboarding"]["step"] == "business"

    def test_keep_default_categories_needs_no_llm(self, db, claude):
        outcome = _walk({}, "hola", "1", "1", "1")
        assert outcome.prefs["onboarding"]["step"] == "income_categories"
        claude.messages.create.assert_not_called()

    def test_customize_option_asks_what_to_change(self, db):
        outcome = _walk({}, "hola", "1", "1", "2")
        assert outcome.prefs["onboarding"]["step"] == "expense_categories_custom"
        assert "agregar, quitar o renombrar" in outcome.replies[-1]

    def test_direct_customization_is_applied(self, db, claude):
        _tool_result(
            claude,
            {
                "is_answer": True,
                "add": ["mascotas"],
                "remove": ["ropa"],
                "rename": [{"from": "hogar", "to": "casa"}],
            },
        )
        outcome = _walk(
            {}, "hola", "1", "1", "quita ropa, agrega mascotas y hogar es casa"
        )

        names = outcome.prefs["onboarding"]["draft"]["expense_categories"]
        assert "ropa" not in names and "mascotas" in names and "casa" in names
        assert names[-1] == "otro"
        assert outcome.prefs["onboarding"]["draft"]["expense_renames"] == [
            ["hogar", "casa"]
        ]
        assert outcome.replies[0].startswith("✅ Tus categorías de gastos")
        assert outcome.prefs["onboarding"]["step"] == "income_categories"


class TestCategoryChanges:
    def test_otro_can_not_be_removed(self):
        names, _ = onboarding._apply_category_changes(
            ["comida", "otro"], {"remove": ["otro"]}
        )
        assert names == ["comida", "otro"]

    def test_added_names_go_before_otro_and_are_cleaned(self):
        names, _ = onboarding._apply_category_changes(
            ["comida", "otro"], {"add": ["  Mascotas "]}
        )
        assert names == ["comida", "mascotas", "otro"]

    def test_duplicates_are_ignored(self):
        names, _ = onboarding._apply_category_changes(
            ["comida", "otro"], {"add": ["Comida"]}
        )
        assert names == ["comida", "otro"]

    def test_rename_onto_an_existing_name_is_ignored(self):
        names, renames = onboarding._apply_category_changes(
            ["comida", "mercado", "otro"],
            {"rename": [{"from": "mercado", "to": "comida"}]},
        )
        assert names == ["comida", "mercado", "otro"] and renames == []


class TestSkip:
    def test_omitir_profile_means_personal(self, db):
        outcome = _walk({}, "hola", "omitir")
        assert outcome.prefs["onboarding"]["draft"]["profile"] == {"type": "personal"}
        assert outcome.prefs["onboarding"]["step"] == "currency"

    def test_omitir_currency_stores_nothing(self, db):
        outcome = _walk({}, "hola", "1", "Omitir")
        assert "default_currency" not in outcome.prefs["onboarding"]["draft"]

    def test_omitir_todo_finishes_with_defaults(self, db):
        outcome = _walk({}, "hola", "omitir todo")

        snapshot = db.append.call_args.args[1]
        assert db.append.call_args.kwargs == {"source": "onboarding", "replace": True}
        assert snapshot["profile"] == {"type": "personal"}
        assert snapshot["default_currency"] == "COP"
        assert snapshot["onboarding"]["status"] == "skipped"
        db.set_categories.assert_any_call(
            7, "expense", DEFAULT_EXPENSE_CATEGORIES, "onboarding", renames=[]
        )
        db.set_categories.assert_any_call(
            7, "income", DEFAULT_INCOME_CATEGORIES, "onboarding", renames=[]
        )
        assert outcome.replies[-1].startswith("🎉")


class TestFinishAndHeldMessage:
    def _complete(self, db, first="almuerzo 20000"):
        prefs = _send({}, first, message_id=41, now="2026-10-19T22:00:00-05:00").prefs
        return _walk(prefs, "1", "1", "1", "1")

    def test_finish_writes_a_complete_snapshot_and_offers_the_held_message(self, db):
        outcome = self._complete(db)

        snapshot = db.append.call_args.args[1]
        assert db.append.call_args.kwargs["replace"] is True
        assert snapshot["onboarding"]["status"] == "done"
        assert snapshot["onboarding"]["step"] == "confirm_held"
        assert outcome.replies[-2].startswith("🎉")
        assert "almuerzo 20000" in outcome.replies[-1]

    def test_yes_replays_the_held_message_with_its_original_timestamp(self, db):
        prefs = self._complete(db).prefs

        outcome = _send(prefs, "sí")

        assert outcome.replay == {
            "text": "almuerzo 20000",
            "message_type": "text",
            "message_id": 41,
            "now": "2026-10-19T22:00:00-05:00",
        }
        assert "held" not in outcome.prefs["onboarding"]

    def test_no_discards_it(self, db):
        prefs = self._complete(db).prefs
        outcome = _send(prefs, "2")
        assert (
            outcome.replies == [MSG_ONBOARDING_HELD_DISCARDED]
            and outcome.replay is None
        )

    def test_anything_else_cancels_and_is_processed_normally(self, db):
        prefs = self._complete(db).prefs
        outcome = _send(prefs, "uber 15000")
        assert not outcome.handled
        assert "step" not in outcome.prefs["onboarding"]

    def test_nothing_held_means_no_question(self, db):
        outcome = self._complete(db, first="hola")
        assert outcome.replies[-1].startswith("🎉")
        assert "step" not in db.append.call_args.args[1]["onboarding"]


class TestRestartAndAbandon:
    def test_restart_keeps_the_profile_and_skips_its_questions(self, db):
        business = {"type": "business", "business": {"industry": "panadería"}}
        prefs = {
            "profile": business,
            "default_currency": "USD",
            "onboarding": {"status": "done"},
        }

        outcome = _send(prefs, "Reiniciar configuración")

        assert outcome.replies[0] == MSG_ONBOARDING_RESTART
        assert outcome.prefs["onboarding"]["step"] == "currency"

        _walk(outcome.prefs, "omitir todo")
        snapshot = db.append.call_args.args[1]
        assert db.append.call_args.kwargs == {
            "source": "onboarding_restart",
            "replace": True,
        }
        assert snapshot["profile"] == business
        db.set_categories.assert_any_call(
            7,
            "expense",
            DEFAULT_BUSINESS_EXPENSE_CATEGORIES,
            "onboarding_restart",
            renames=[],
        )

    def test_restart_keyword_must_be_the_whole_message(self, db):
        outcome = _send(
            {"onboarding": {"status": "done"}},
            "quiero reiniciar configuración de categorías",
        )
        assert not outcome.handled

    def test_abandoned_onboarding_starts_over(self, db):
        prefs = _walk({}, "almuerzo 20000", "1").prefs
        assert prefs["onboarding"]["step"] == "currency"

        outcome = _send(prefs, "1", now="2026-10-22T09:00:00-05:00")

        assert outcome.replies[0] == MSG_ONBOARDING_WELCOME
        assert outcome.prefs["onboarding"]["step"] == "profile"
        assert outcome.prefs["onboarding"]["held"]["text"] == "almuerzo 20000"
