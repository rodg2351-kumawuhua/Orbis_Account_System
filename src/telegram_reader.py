from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telethon import TelegramClient

from src.models import TelegramMessageRecord


class TelegramReader:
    def __init__(self, api_id: int, api_hash: str, session_name: str) -> None:
        self._client = TelegramClient(session_name, api_id, api_hash)

    async def fetch_messages(
        self,
        chat_reference: str,
        limit: int = 100,
        days: int | None = None,
    ) -> list[TelegramMessageRecord]:
        await self._client.start()

        cutoff: datetime | None = None
        if days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=days)

        records: list[TelegramMessageRecord] = []
        async for message in self._client.iter_messages(chat_reference, limit=limit):
            if not message.message:
                continue
            if cutoff and message.date < cutoff:
                continue

            sender = await message.get_sender()
            sender_name = (
                getattr(sender, "first_name", None)
                or getattr(sender, "title", None)
                or "Unknown"
            )

            records.append(
                TelegramMessageRecord(
                    message_id=message.id,
                    sent_at=message.date,
                    sender_name=sender_name,
                    sender_id=getattr(sender, "id", None),
                    text=message.message.strip(),
                )
            )

        records.reverse()
        return records

    async def close(self) -> None:
        await self._client.disconnect()
