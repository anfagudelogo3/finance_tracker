from dataclasses import dataclass


@dataclass
class AgentRequest:
    """Everything a specialist agent needs to handle one inbound message.

    Reuses existing dict shapes rather than inventing parallel types: `media`
    matches media.store_all_media()'s output, `conversation` matches
    database.load_recent_turns()'s {role, content} rows.
    """

    user_id: int
    message_id: int
    phone: str
    text: str
    media: list[dict]
    message_type: str  # "text" | "audio" | "image"
    now: str  # ISO datetime, America/Bogota
    categories: list[str]
    conversation: list[dict]


@dataclass
class AgentResponse:
    """What an agent hands back to the orchestrator to send."""

    ok: bool
    reply_text: str
    reply_attachment: dict | None = None  # {"url": str, "filename": str}
    data: list[dict] | None = None
    error: str | None = None
