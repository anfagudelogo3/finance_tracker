# User Guide

Everything happens inside a normal WhatsApp chat with the bot. There are three things you
can do: **log an expense**, **ask for a report**, and **export to Excel**.

## Logging an expense

Send a short, natural message describing what you spent. The amount can be written many
ways — Colombian slang like "luca"/"lukas" (thousands) is understood.

| You send | Interpreted as |
|----------|----------------|
| `almuerzo 32000` | COP 32,000 · comida |
| `uber 14 lukas` | COP 14,000 · transporte · merchant Uber |
| `mercado 120000 tarjeta` | COP 120,000 · mercado · paid by card |
| `cine 25 mil y palomitas 12 mil` | two expenses in one message |
| `gasté 20 dólares en un libro` | USD 20 · educacion |

The bot replies with a confirmation, for example:

```
✅ Registré COP 14.000 en transporte para 2026-04-03
```

For multiple expenses it lists each line and a total (when all share one currency).

### What gets extracted

For each expense the model fills in:

- **amount** — the numeric value (no currency symbol)
- **currency** — defaults to `COP` when not stated; understands `USD`, etc.
- **category** — one of: `comida`, `transporte`, `mercado`, `salud`,
  `entretenimiento`, `hogar`, `educacion`, `ropa`, `servicios`, `otro`
- **payment_method** — e.g. `tarjeta`, `efectivo`, `nequi` (optional)
- **merchant** — a brand or place if mentioned (optional)
- **description** — a short summary of the expense

If the bot can't make sense of the message it replies:

> No entendí ese mensaje. Intenta describir el gasto con monto y categoría.

### Voice notes 🎙️

Send a WhatsApp voice note describing the expense. It is transcribed with OpenAI Whisper
(Spanish) and then parsed exactly like a text message. Any caption you add is combined
with the transcript.

### Photos of receipts 📷

Send a photo (e.g. a receipt) and the image is read by a vision model, which extracts the
expense(s) from it. A caption is used as extra context. Supported image types: JPEG, PNG,
WebP.

## Asking for a report

Send a message asking about your spending and the bot replies with an in-chat summary
grouped by category (and by currency when more than one is present).

Examples that trigger a report:

- `resumen` · `reporte` · `mis gastos`
- `cuánto gasté esta semana`
- `gastos de marzo`
- `cuánto llevo este mes`
- `how much did I spend last week`

Date ranges are understood naturally:

| Phrase | Range |
|--------|-------|
| *(no date given)* | 1st of the current month → today |
| `esta semana` | Monday of this week → today |
| `la semana pasada` | Monday–Sunday of last week |
| `este mes` | 1st of this month → today |
| `el mes pasado` | the full previous calendar month |
| `últimos N días` | today − N days → today |
| a month name (`marzo`, `abril`) | that whole month (capped at today if current) |

Example reply:

```
📊 Resumen 1 abr 2026 – 15 abr 2026:

  comida: COP 240.000 (6 gastos)
  transporte: COP 58.000 (4 gastos)
Total: COP 298.000 — 10 gastos
```

## Exporting to Excel

Ask for a spreadsheet and the bot generates an `.xlsx` file and sends it back as a
WhatsApp document.

Trigger words include: `excel`, `xlsx`, `exportar`, `exportame`, `descargar`,
`hoja de cálculo`, `spreadsheet`. You can combine them with a date range the same way as
reports, e.g. `exportame mis gastos de marzo`.

The file contains one row per expense with columns: Amount, Currency, Category, Expense
Date, Payment Method, Merchant, Description, Source.

> The download link is a presigned S3 URL valid for a short time, but Twilio fetches and
> delivers the file to you immediately, so you receive it as a normal WhatsApp attachment.

## Notes and limits

- Only numbers in the **allowlist** (`ALLOWED_PHONE_NUMBERS`) are answered; messages from
  anyone else are ignored.
- Each message is processed independently — there is no conversational memory.
- Categories are fixed (see the list above); anything that doesn't fit becomes `otro`.
- See [development.md](development.md#known-limitations-and-caveats) for current
  behavioral caveats (e.g. how dates are determined).
