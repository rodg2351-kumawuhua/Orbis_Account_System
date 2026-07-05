from __future__ import annotations

import json

from src.config import SUMMARY_FIELDS
from src.models import TelegramMessageRecord


def build_summary_prompt(chat_reference: str, messages: list[TelegramMessageRecord]) -> str:
    transcript_lines = []
    for item in messages:
        transcript_lines.append(
            f"[{item.sent_at.isoformat()}] {item.sender_name}: {item.text}"
        )

    output_schema = {field: "" for field in SUMMARY_FIELDS}
    output_schema["follow_up_date"] = "YYYY-MM-DD or empty string"

    return f"""
You are a CRM assistant.

Your task is to read a Telegram customer conversation and produce a structured JSON summary.

Rules:
- Return valid JSON only.
- Use concise business language.
- If information is missing, return an empty string.
- Preserve commercially important details such as budget, objections, timing, and next steps.
- `follow_up_date` should be in YYYY-MM-DD format when inferable, otherwise empty.

Chat reference:
{chat_reference}

Expected JSON shape:
{json.dumps(output_schema, ensure_ascii=False, indent=2)}

Conversation transcript:
{chr(10).join(transcript_lines)}
""".strip()
