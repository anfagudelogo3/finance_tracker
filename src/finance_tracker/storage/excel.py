import io
import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from finance_tracker.config import (
    S3_BUCKET_NAME,
    PRESIGNED_URL_EXPIRY_SECONDS,
    TIMEZONE,
)

logger = logging.getLogger(__name__)

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="2E75B6")
_HEADER_ALIGN = Alignment(horizontal="center")

# (header, field) per sheet — headers are user-facing, so Spanish
_EXPENSE_COLUMNS = [
    ("Monto", "amount"),
    ("Moneda", "currency"),
    ("Categoría", "category"),
    ("Fecha", "expense_date"),
    ("Medio de pago", "payment_method"),
    ("Comercio", "merchant"),
    ("Descripción", "description"),
    ("Origen", "source"),
]
_INCOME_COLUMNS = [
    ("Monto", "amount"),
    ("Moneda", "currency"),
    ("Categoría", "category"),
    ("Fecha", "income_date"),
    ("Descripción", "description"),
    ("Origen", "source"),
]
_SUMMARY_COLUMNS = ["Moneda", "Total gastos", "Total ingresos", "Neto"]


def _build_s3_key(phone: str, min_date: str, max_date: str) -> str:
    timestamp = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%dT%H%M%S")
    phone_clean = phone.lstrip("+").replace(":", "_")
    return f"processed/whatsapp/{phone_clean}/{timestamp}_{min_date}_{max_date}.xlsx"


def _auto_width(ws) -> None:
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=0)
        ws.column_dimensions[get_column_letter(col[0].column)].width = max(
            max_len + 2, 12
        )


def _header(ws, headers: list[str]) -> None:
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN


def _fill_records(ws, columns: list[tuple[str, str]], rows: list[dict]) -> None:
    _header(ws, [header for header, _ in columns])
    for row_idx, record in enumerate(rows, 2):
        for col, (_, field) in enumerate(columns, 1):
            value = record.get(field)
            if value is None:
                value = ""
            elif field == "amount":
                value = float(value)
            elif field.endswith("_date"):
                value = str(value)
            ws.cell(row=row_idx, column=col, value=value)
    _auto_width(ws)


def _totals(rows: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[str(row.get("currency", "COP")).upper()] += float(row["amount"])
    return totals


def create_excel_bytes(expenses: list[dict], incomes: list[dict]) -> bytes:
    """Build the .xlsx export and return raw bytes: a "Gastos" sheet, an "Ingresos"
    sheet, and a "Resumen" sheet with totals and the net per currency (never merged)."""
    wb = openpyxl.Workbook()
    ws_expenses = wb.active
    ws_expenses.title = "Gastos"
    _fill_records(ws_expenses, _EXPENSE_COLUMNS, expenses)
    _fill_records(wb.create_sheet("Ingresos"), _INCOME_COLUMNS, incomes)

    ws_summary = wb.create_sheet("Resumen")
    _header(ws_summary, _SUMMARY_COLUMNS)
    spent, received = _totals(expenses), _totals(incomes)
    for row_idx, currency in enumerate(sorted(set(spent) | set(received)), 2):
        values = [
            currency,
            spent[currency],
            received[currency],
            received[currency] - spent[currency],
        ]
        for col, value in enumerate(values, 1):
            ws_summary.cell(row=row_idx, column=col, value=value)
    _auto_width(ws_summary)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload_and_sign(
    excel_bytes: bytes,
    phone: str,
    min_date: str,
    max_date: str,
) -> str:
    """Upload Excel bytes to S3 and return a presigned URL valid for 60 seconds."""
    s3_key = _build_s3_key(phone, min_date, max_date)
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=s3_key,
        Body=excel_bytes,
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    logger.info(
        "Excel uploaded to s3://%s/%s (%d bytes)",
        S3_BUCKET_NAME,
        s3_key,
        len(excel_bytes),
    )

    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )
    logger.info(
        "Presigned URL generated (expires in %ds)", PRESIGNED_URL_EXPIRY_SECONDS
    )
    return presigned_url
