from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TelegramMessageRecord:
    message_id: int
    sent_at: datetime
    sender_name: str
    sender_id: int | None
    text: str


@dataclass(frozen=True)
class LedgerEntryCreate:
    entry_date: str
    entry_type: str
    category: str
    amount: str
    currency: str
    vendor: str
    purpose: str
    payment_method: str
    invoice_no: str
    description: str
    source: str
    source_message: str
    reference_no: str
    receipt_status: str
    receipt_file_path: str
    deductible_status: str
    notes: str
    created_by: str
    created_at: str
