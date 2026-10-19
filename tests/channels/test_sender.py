from finance_tracker.channels.whatsapp.sender import (
    format_confirmation,
    format_income_confirmation,
)


def _record(amount, category, **extra):
    return {
        "amount": amount,
        "currency": "COP",
        "category": category,
        "date": "2026-10-20",
        **extra,
    }


class TestExpenseConfirmation:
    def test_single_says_it_is_an_expense(self):
        text = format_confirmation([_record(200000, "comida")])
        assert text == "💸 Gasto registrado: COP 200.000 en comida (2026-10-20)"

    def test_payment_method_goes_with_the_date(self):
        text = format_confirmation(
            [_record(12000, "mercado", payment_method="tarjeta")]
        )
        assert (
            text == "💸 Gasto registrado: COP 12.000 en mercado (2026-10-20, tarjeta)"
        )

    def test_several(self):
        text = format_confirmation(
            [_record(20000, "comida"), _record(40000, "entretenimiento")]
        )
        assert text.splitlines() == [
            "💸 Registré 2 gastos:",
            "  • COP 20.000 en comida",
            "  • COP 40.000 en entretenimiento",
            "Total: COP 60.000",
        ]


class TestIncomeConfirmation:
    def test_single_says_it_is_an_income(self):
        text = format_income_confirmation([_record(50000, "regalo")])
        assert text == "💰 Ingreso registrado: COP 50.000 de regalo (2026-10-20)"

    def test_several(self):
        text = format_income_confirmation(
            [_record(3000000, "salario"), _record(200000, "regalo")]
        )
        assert text.splitlines()[0] == "💰 Registré 2 ingresos:"


def test_expense_and_income_together_are_distinguishable():
    """The router sends both confirmations in one reply: each line must say what it is."""
    reply = "\n\n".join(
        [
            format_confirmation([_record(200000, "comida")]),
            format_income_confirmation([_record(50000, "regalo")]),
        ]
    )
    expense_line, income_line = reply.split("\n\n")
    assert "Gasto" in expense_line and "Ingreso" not in expense_line
    assert "Ingreso" in income_line and "Gasto" not in income_line
