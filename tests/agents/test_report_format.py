from finance_tracker.agents.report_format import format_report


def _row(amount, category, currency="COP"):
    return {"amount": amount, "currency": currency, "category": category}


def test_expenses_then_incomes_then_net():
    report = format_report(
        [_row(480000, "comida"), _row(20000, "comida"), _row(220000, "transporte")],
        [_row(2000000, "salario")],
        "2026-10-01",
        "2026-10-22",
    )
    assert report.splitlines() == [
        "📊 Resumen 1 oct 2026 – 22 oct 2026:",
        "",
        "💸 Gastos",
        "  comida: COP 500.000 (2 gastos)",
        "  transporte: COP 220.000 (1 gasto)",
        "  Total: COP 720.000 — 3 gastos",
        "💰 Ingresos",
        "  salario: COP 2.000.000 (1 ingreso)",
        "  Total: COP 2.000.000 — 1 ingreso",
        "📈 Neto: COP 1.280.000",
    ]


def test_negative_net():
    report = format_report(
        [_row(300000, "comida")], [_row(100000, "regalo")], "2026-10-01", "2026-10-22"
    )
    assert report.splitlines()[-1] == "📉 Neto: COP -200.000"


def test_only_expenses_says_no_incomes():
    report = format_report([_row(10000, "comida")], [], "2026-10-01", "2026-10-22")
    assert "💰 Ingresos: ninguno" in report
    assert report.splitlines()[-1] == "📉 Neto: COP -10.000"


def test_only_incomes():
    report = format_report([], [_row(50000, "regalo")], "2026-10-01", "2026-10-22")
    assert "💸 Gastos: ninguno" in report
    assert report.splitlines()[-1] == "📈 Neto: COP 50.000"


def test_currencies_are_never_merged():
    report = format_report(
        [_row(10000, "comida"), _row(20, "comida", "USD")],
        [_row(100, "freelance", "USD")],
        "2026-10-01",
        "2026-10-22",
    )
    lines = report.splitlines()
    assert "*COP*" in lines and "*USD*" in lines
    assert "📉 Neto: COP -10.000" in lines
    assert "📈 Neto: USD 80" in lines


def test_nothing_recorded():
    assert format_report([], [], "2026-10-01", "2026-10-22") == (
        "📊 Sin movimientos registrados entre 1 oct 2026 y 22 oct 2026."
    )
