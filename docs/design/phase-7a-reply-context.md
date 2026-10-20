# Phase 7a — Reply context (WhatsApp swipe-replies)

Status: **built** (`feature/reply-context`). Scoped in conversation before Phase 8.

## Problem

WhatsApp users reply to a specific earlier message by swiping on it. Twilio then adds two
parameters to the webhook, `OriginalRepliedMessageSid` and `OriginalRepliedMessageSender`,
but **not the quoted content**. Until now the app ignored them, so "eran 15000" or
"regístralo" sent as a reply lost the message it referred to — often older than the 30-minute
conversation window.

## Decisions

| Topic | Decision |
|---|---|
| Scope | A reply to **anything**: the user's own text, voice note or photo, and any bot message (confirmation, question, report, Excel document). |
| Where the content comes from | **Our database first.** Inbound messages are already stored by Twilio SID (`messages.whatsapp_message_id`). Every outbound message now stores its SID too (`conversation_turns.external_id`). |
| Twilio fallback | `client.messages(sid).fetch()` only when the SID isn't in our database ("in a perfect world never taken"). Short timeout, and the fetched message must be from or to this user's phone. On any failure the message is processed without reply context. |
| Linking to records | **Deterministic, no LLM guessing.** Every expense/income already has `message_id` (the inbound message that created it). Each bot turn stores the `message_id` of the inbound message it answered. So a reply resolves: quoted message → inbound message → the records created from it. |
| Photos | Not re-read: the records created from the photo's message describe it ("photo → 💸 COP 85.000 en mercado"). A photo that produced no record shows its caption, or "nothing recorded". |
| Several records | One message can create several records ("almuerzo 20k y me pagaron 2M"). All are passed on; the agent asks which one when it matters ("ask, don't assume"). |
| Onboarding and pending confirmations | Ignore the reply: they follow their own fixed flows, and "sí" means the same with or without a quote. |
| Fast paths | Skipped for replies: the meaning depends on the quoted message ("200000" replying to "¿gasto o ingreso?"). |
| Schema | No migration — the database is rebuilt (test data only). |

## Design

- `webhook.extract_message` adds `replied_to_sid` / `replied_to_sender`. Signature
  verification is unchanged (Twilio signs every parameter it sends).
- `storage/database.py`:
  - `save_turn(..., external_id=None, message_id=None)`: inbound turns carry their message id;
    outbound turns carry the Twilio SID and the id of the inbound message they answer.
  - `find_replied_message(user_id, sid)`: looks in `messages` (the user's own) then
    `conversation_turns` (the bot's), always scoped by `user_id`.
  - `get_message_records(user_id, message_id)`: live expenses and incomes created from a
    message, with category names.
- `channels/whatsapp/sender.fetch_message(sid)`: the Twilio fallback.
- `channels/whatsapp/reply_context.resolve(user_id, phone, sid)` → `replied_to` dict:
  `{role, text, date, records: [{kind, id, amount, currency, category, date}], source}`, text
  truncated to `REPLIED_TO_MAX_CHARS`.
- `AgentRequest.replied_to`. The router receives it as tagged data
  (`<replied_to role="…">…</replied_to>`) and writes self-contained tasks for the specialists.
  Record ids are passed along now and become the exact target for Phase 8 corrections.

## Phase 8 note

The message-id link answers "which records did this message create?". A correction
confirmation ("✅ Actualicé el gasto…") creates nothing — it changes an existing record — so
Phase 8 will add an explicit link from such bot messages to the record they changed.
