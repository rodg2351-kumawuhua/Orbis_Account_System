from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.parser import BytesParser
from email.policy import default
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.ai_summarizer import AIFinanceAssistant
from src.config import BASE_DIR, HK_MINIMAL_LEDGER_HEADERS, load_accounting_settings
from src.models import LedgerEntryCreate


RECEIPT_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}


def safe_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def guess_mime_type(filename: str) -> str:
    return RECEIPT_EXTENSIONS.get(Path(filename).suffix.lower(), "application/octet-stream")


@dataclass(frozen=True)
class ParsedExpenseDraft:
    entry_date: str
    amount: str
    currency: str
    vendor: str
    purpose: str
    category: str
    payment_method: str
    invoice_no: str
    description: str
    source: str
    source_message: str
    reference_no: str


@dataclass(frozen=True)
class UploadedReceipt:
    filename: str
    content_type: str
    content: bytes


class ReceiptStorage:
    def save_receipt(self, receipt: UploadedReceipt, reference_no: str, entry_date: str) -> str:
        raise NotImplementedError


class LocalReceiptStorage(ReceiptStorage):
    def __init__(self, receipts_dir: Path) -> None:
        self.receipts_dir = receipts_dir
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def save_receipt(self, receipt: UploadedReceipt, reference_no: str, entry_date: str) -> str:
        extension = Path(receipt.filename).suffix.lower() or ".bin"
        dated_dir = self.receipts_dir / entry_date
        dated_dir.mkdir(exist_ok=True)
        output_path = dated_dir / f"{reference_no}{extension}"
        output_path.write_bytes(receipt.content)
        return str(output_path.relative_to(BASE_DIR))


class CloudinaryReceiptStorage(ReceiptStorage):
    def __init__(self, cloud_name: str, api_key: str, api_secret: str) -> None:
        import cloudinary
        import cloudinary.uploader

        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        self._uploader = cloudinary.uploader

    def save_receipt(self, receipt: UploadedReceipt, reference_no: str, entry_date: str) -> str:
        upload_result = self._uploader.upload(
            receipt.content,
            folder=f"orbis-receipts/{entry_date}",
            public_id=reference_no,
            resource_type="auto",
            overwrite=True,
            filename_override=receipt.filename,
        )
        return str(upload_result.get("secure_url", upload_result.get("url", "")))


class AuthManager:
    def __init__(self, secret_key: str) -> None:
        self._secret_key = secret_key.encode("utf-8")

    def hash_password(self, password: str, salt: str | None = None) -> str:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            200_000,
        ).hex()
        return f"{salt}${digest}"

    def verify_password(self, password: str, stored_hash: str) -> bool:
        if "$" not in stored_hash:
            return False
        salt, expected = stored_hash.split("$", 1)
        actual = self.hash_password(password, salt).split("$", 1)[1]
        return hmac.compare_digest(actual, expected)

    def create_session_token(self, username: str, role: str) -> str:
        expires_at = int((datetime.now(UTC) + timedelta(days=7)).timestamp())
        payload = f"{username}|{role}|{expires_at}"
        signature = hmac.new(self._secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}|{signature}"

    def verify_session_token(self, token: str) -> dict[str, str] | None:
        parts = token.split("|")
        if len(parts) != 4:
            return None
        username, role, expires_at, signature = parts
        payload = f"{username}|{role}|{expires_at}"
        expected = hmac.new(self._secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_at) < int(datetime.now(UTC).timestamp()):
            return None
        return {"username": username, "role": role}


class DatabaseStore:
    def __init__(
        self,
        database_path: Path,
        output_dir: Path,
        auth: AuthManager,
        database_url: str = "",
        receipt_storage: ReceiptStorage | None = None,
    ) -> None:
        self.database_path = database_path
        self.database_url = database_url
        self.is_postgres = self.database_url.startswith(("postgres://", "postgresql://"))
        self.output_dir = output_dir
        self.receipts_dir = self.output_dir / "receipts"
        self.output_dir.mkdir(exist_ok=True)
        self.receipts_dir.mkdir(exist_ok=True)
        if not self.is_postgres:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth = auth
        self.receipt_storage = receipt_storage or LocalReceiptStorage(self.receipts_dir)
        self._initialize()

    def _connect(self):
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self.database_url, row_factory=dict_row)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _sql(self, query: str) -> str:
        return query.replace("?", "%s") if self.is_postgres else query

    def _initialize(self) -> None:
        with self._connect() as connection:
            schema = """
                CREATE TABLE IF NOT EXISTS users (
                    id {user_id_type},
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id {ledger_id_type},
                    entry_date TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    vendor TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    invoice_no TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_message TEXT NOT NULL,
                    reference_no TEXT NOT NULL UNIQUE,
                    receipt_status TEXT NOT NULL,
                    receipt_file_path TEXT NOT NULL,
                    deductible_status TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            schema = schema.format(
                user_id_type="BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT",
                ledger_id_type="BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT",
            )
            if self.is_postgres:
                connection.execute(schema)
            else:
                connection.executescript(schema)

    def bootstrap_admin(self, username: str, password: str) -> None:
        if self.get_user(username):
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                self._sql("""
                INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
                VALUES (?, ?, 'admin', 1, ?, ?)
                """),
                (username, self.auth.hash_password(password), now, now),
            )

    def get_user(self, username: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                self._sql("SELECT username, password_hash, role, is_active, created_at, updated_at FROM users WHERE username = ?"),
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, role, is_active, created_at, updated_at FROM users ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_user(self, username: str, password: str, role: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                self._sql("""
                INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """),
                (username, self.auth.hash_password(password), role, now, now),
            )

    def update_user_password(self, username: str, password: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                self._sql("UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?"),
                (self.auth.hash_password(password), now, username),
            )

    def set_user_active(self, username: str, is_active: bool) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                self._sql("UPDATE users SET is_active = ?, updated_at = ? WHERE username = ?"),
                (1 if is_active else 0, now, username),
            )

    def migrate_from_csv_if_needed(self) -> None:
        csv_path = self.output_dir / "ledger_entries.csv"
        if not csv_path.exists():
            return
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM ledger_entries").fetchone()
            count_value = count["count"] if self.is_postgres else count[0]
            if count_value > 0:
                return
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            normalized = {header: row.get(header, "") for header in HK_MINIMAL_LEDGER_HEADERS}
            if not normalized.get("reference_no"):
                continue
            with self._connect() as connection:
                if self.is_postgres:
                    query = self._sql("""
                    INSERT INTO ledger_entries (
                        entry_date, entry_type, category, amount, currency, vendor, purpose,
                        payment_method, invoice_no, description, source, source_message, reference_no,
                        receipt_status, receipt_file_path, deductible_status, notes, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (reference_no) DO NOTHING
                    """)
                else:
                    query = """
                    INSERT OR IGNORE INTO ledger_entries (
                        entry_date, entry_type, category, amount, currency, vendor, purpose,
                        payment_method, invoice_no, description, source, source_message, reference_no,
                        receipt_status, receipt_file_path, deductible_status, notes, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                connection.execute(
                    query,
                    (
                        normalized["entry_date"],
                        normalized["entry_type"],
                        normalized["category"],
                        normalized["amount"],
                        normalized["currency"],
                        normalized["vendor"],
                        normalized["purpose"],
                        normalized["payment_method"],
                        normalized["invoice_no"],
                        normalized["description"],
                        normalized["source"],
                        normalized["source_message"],
                        normalized["reference_no"],
                        normalized["receipt_status"],
                        normalized["receipt_file_path"],
                        normalized["deductible_status"],
                        normalized["notes"],
                        normalized.get("created_by", "") or "system",
                        normalized["created_at"],
                    ),
                )

    def append_entry(self, entry: LedgerEntryCreate) -> None:
        with self._connect() as connection:
            connection.execute(
                self._sql("""
                INSERT INTO ledger_entries (
                    entry_date, entry_type, category, amount, currency, vendor, purpose,
                    payment_method, invoice_no, description, source, source_message, reference_no,
                    receipt_status, receipt_file_path, deductible_status, notes, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (
                    entry.entry_date,
                    entry.entry_type,
                    entry.category,
                    entry.amount,
                    entry.currency,
                    entry.vendor,
                    entry.purpose,
                    entry.payment_method,
                    entry.invoice_no,
                    entry.description,
                    entry.source,
                    entry.source_message,
                    entry.reference_no,
                    entry.receipt_status,
                    entry.receipt_file_path,
                    entry.deductible_status,
                    entry.notes,
                    entry.created_by,
                    entry.created_at,
                ),
            )

    def list_entries(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT entry_date, entry_type, category, amount, currency, vendor, purpose,
                       payment_method, invoice_no, description, source, source_message, reference_no,
                       receipt_status, receipt_file_path, deductible_status, notes,
                       COALESCE(NULLIF(created_by, ''), 'system') AS created_by,
                       created_at
                FROM ledger_entries
                ORDER BY entry_date DESC, created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def summarize_by_month(self) -> list[dict[str, str]]:
        buckets: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {"income": Decimal("0"), "expense": Decimal("0")}
        )
        for row in self.list_entries():
            month = (row.get("entry_date", "") or "")[:7]
            if len(month) != 7:
                continue
            try:
                amount = Decimal(row.get("amount", "0") or "0")
            except InvalidOperation:
                continue
            if row.get("entry_type") == "income":
                buckets[month]["income"] += amount
            elif row.get("entry_type") == "expense":
                buckets[month]["expense"] += amount
        return [
            {
                "month": month,
                "income": f"{values['income']:.2f}",
                "expense": f"{values['expense']:.2f}",
                "net_profit_loss": f"{(values['income'] - values['expense']):.2f}",
            }
            for month, values in sorted(buckets.items())
        ]

    def next_reference_no(self, entry_date: str) -> str:
        prefix = f"TXN-{entry_date.replace('-', '')}-"
        with self._connect() as connection:
            rows = connection.execute(
                self._sql("SELECT reference_no FROM ledger_entries WHERE reference_no LIKE ?"),
                (f"{prefix}%",),
            ).fetchall()
        counter = 0
        for row in rows:
            suffix = row["reference_no"].removeprefix(prefix)
            if suffix.isdigit():
                counter = max(counter, int(suffix))
        return f"{prefix}{counter + 1:04d}"

    def next_invoice_no(self, entry_date: str) -> str:
        prefix = f"RCPT-{entry_date.replace('-', '')}-"
        with self._connect() as connection:
            rows = connection.execute(
                self._sql("SELECT invoice_no FROM ledger_entries WHERE invoice_no LIKE ?"),
                (f"{prefix}%",),
            ).fetchall()
        counter = 0
        for row in rows:
            suffix = row["invoice_no"].removeprefix(prefix)
            if suffix.isdigit():
                counter = max(counter, int(suffix))
        return f"{prefix}{counter + 1:04d}"

    def save_receipt(self, receipt: UploadedReceipt, reference_no: str, entry_date: str) -> str:
        return self.receipt_storage.save_receipt(receipt, reference_no, entry_date)


class SimpleExpenseParser:
    CURRENCY_AMOUNT_PATTERNS = [
        re.compile(r"(?P<currency>HKD|USD|RMB|CNY|EUR|PKR|AED|¥|\$)\s*(?P<amount>\d+(?:\.\d{1,2})?)", re.I),
        re.compile(r"(?P<amount>\d+(?:\.\d{1,2})?)\s*(?P<currency>HKD|USD|RMB|CNY|EUR|PKR|AED)", re.I),
    ]

    def parse(self, text: str, source: str, reference_no: str = "") -> ParsedExpenseDraft:
        cleaned = safe_text(text)
        amount, raw_currency = self._extract_amount_and_currency(cleaned)
        return ParsedExpenseDraft(
            entry_date=self._extract_date(cleaned) or date.today().isoformat(),
            amount=amount,
            currency=self._normalize_currency(raw_currency),
            vendor=self._extract_vendor(cleaned),
            purpose=self._guess_purpose(cleaned),
            category=self._guess_category(cleaned),
            payment_method=self._guess_payment_method(cleaned),
            invoice_no="",
            description=cleaned,
            source=source,
            source_message=text,
            reference_no=reference_no,
        )

    def _extract_amount_and_currency(self, text: str) -> tuple[str, str]:
        for pattern in self.CURRENCY_AMOUNT_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group("amount"), match.groupdict().get("currency", "") or ""
        return "", ""

    def _normalize_currency(self, value: str | None) -> str:
        value = (value or "").upper().strip()
        if value in {"¥", "CNY", "RMB"}:
            return "CNY"
        if value == "$":
            return "HKD"
        if value in {"EUR", "PKR", "AED", "USD", "HKD"}:
            return value
        return value or "HKD"

    def _extract_date(self, text: str) -> str | None:
        for pattern in [r"(20\d{2}-\d{2}-\d{2})", r"(20\d{2}/\d{2}/\d{2})"]:
            match = re.search(pattern, text)
            if match:
                return match.group(1).replace("/", "-")
        return None

    def _extract_vendor(self, text: str) -> str:
        tokens = text.split()
        if len(tokens) >= 3 and re.match(r"20\d{2}[-/]\d{2}[-/]\d{2}", tokens[0]):
            return tokens[1][:60]
        return ""

    def _guess_purpose(self, text: str) -> str:
        lower_text = text.lower()
        if "subscription" in lower_text or "订阅" in text:
            return "company subscription"
        if "meeting" in lower_text or "客户开会" in text:
            return "client meeting"
        return ""

    def _guess_category(self, text: str) -> str:
        lower_text = text.lower()
        if any(keyword in lower_text for keyword in ["received", "收到", "payment from", "client paid", "客户付款"]):
            return "sales_income"
        mapping = {
            "software": ["openai", "aws", "google workspace", "notion", "cursor", "vps", "domain", "hosting", "saas"],
            "transport": ["taxi", "uber", "mtr", "train", "flight"],
            "meals": ["lunch", "dinner", "coffee", "meal", "餐", "咖啡"],
            "office": ["stationery", "office", "printer", "desk"],
            "bank_fee": ["bank fee", "手续费", "monthly fee"],
            "marketing": ["ads", "advertising", "facebook", "google ads"],
        }
        for category, keywords in mapping.items():
            if any(keyword in lower_text for keyword in keywords):
                return category
        return "general_expense"

    def _guess_payment_method(self, text: str) -> str:
        lower_text = text.lower()
        if "crypto" in lower_text or "usdt" in lower_text or "btc" in lower_text or "eth" in lower_text:
            return "crypto"
        if "fps" in lower_text:
            return "fps"
        if "cash" in lower_text or "现金" in text:
            return "cash"
        if "credit card" in lower_text or "信用卡" in text or "visa" in lower_text or "master" in lower_text:
            return "credit_card"
        if "bank" in lower_text or "transfer" in lower_text or "转账" in text:
            return "bank_transfer"
        return "other"


class FinanceService:
    def __init__(self, use_ai: bool = False) -> None:
        self.settings = load_accounting_settings()
        self.auth = AuthManager(self.settings.app_secret_key)
        database_path = BASE_DIR / self.settings.database_path
        receipt_storage: ReceiptStorage
        if (
            self.settings.cloudinary_cloud_name
            and self.settings.cloudinary_api_key
            and self.settings.cloudinary_api_secret
        ):
            receipt_storage = CloudinaryReceiptStorage(
                self.settings.cloudinary_cloud_name,
                self.settings.cloudinary_api_key,
                self.settings.cloudinary_api_secret,
            )
        else:
            receipt_storage = LocalReceiptStorage(BASE_DIR / "output" / "receipts")
        self.store = DatabaseStore(
            database_path,
            BASE_DIR / "output",
            self.auth,
            database_url=self.settings.database_url,
            receipt_storage=receipt_storage,
        )
        self.store.bootstrap_admin(
            self.settings.bootstrap_admin_username,
            self.settings.bootstrap_admin_password,
        )
        self.store.migrate_from_csv_if_needed()
        self.parser = SimpleExpenseParser()
        self.ai = None
        if use_ai and self.settings.openai_api_key:
            self.ai = AIFinanceAssistant(
                api_key=self.settings.openai_api_key,
                model=self.settings.openai_model,
            )

    def authenticate_user(self, username: str, password: str) -> dict[str, str] | None:
        user = self.store.get_user(username)
        if not user or str(user["is_active"]) != "1":
            return None
        if not self.auth.verify_password(password, user["password_hash"]):
            return None
        return user

    def create_entry_from_text(
        self,
        text: str,
        source: str,
        reference_no: str = "",
        created_by: str = "system",
    ) -> LedgerEntryCreate:
        draft = self.parser.parse(text=text, source=source, reference_no=reference_no)
        ai_suggestion = self.ai.suggest_ledger_fields(text) if self.ai else {}
        entry_date = ai_suggestion.get("entry_date", draft.entry_date) or draft.entry_date
        amount = ai_suggestion.get("amount", draft.amount) or draft.amount
        reference_no = reference_no or self.store.next_reference_no(entry_date)
        invoice_no = ai_suggestion.get("invoice_no", "") or self.store.next_invoice_no(entry_date)
        self._validate_amount(amount)
        entry_type = ai_suggestion.get("entry_type", "") or self._guess_entry_type(text)

        return LedgerEntryCreate(
            entry_date=entry_date,
            entry_type=entry_type,
            category=ai_suggestion.get("category", draft.category) or draft.category,
            amount=amount,
            currency=ai_suggestion.get("currency", draft.currency) or draft.currency,
            vendor=ai_suggestion.get("vendor", draft.vendor) or draft.vendor,
            purpose=ai_suggestion.get("purpose", draft.purpose) or draft.purpose,
            payment_method=ai_suggestion.get("payment_method", draft.payment_method) or draft.payment_method,
            invoice_no=invoice_no,
            description=ai_suggestion.get("description", draft.description) or draft.description,
            source=source,
            source_message=draft.source_message,
            reference_no=reference_no,
            receipt_status=ai_suggestion.get("receipt_status", "pending") or "pending",
            receipt_file_path="",
            deductible_status=ai_suggestion.get("deductible_status", "review") or "review",
            notes=ai_suggestion.get("notes", ""),
            created_by=created_by,
            created_at=datetime.now(UTC).isoformat(),
        )

    def create_entry_from_form(
        self,
        fields: dict[str, str],
        receipt: UploadedReceipt | None,
        created_by: str,
    ) -> tuple[LedgerEntryCreate, dict[str, str]]:
        normalized = {key: safe_text(value) for key, value in fields.items()}
        entry_date = normalized.get("entry_date") or date.today().isoformat()
        reference_no = normalized.get("reference_no") or self.store.next_reference_no(entry_date)
        invoice_no = normalized.get("invoice_no") or self.store.next_invoice_no(entry_date)
        ai_suggestion: dict[str, str] = {}

        if receipt and self.ai and receipt.content_type.startswith("image/"):
            ai_suggestion = self.ai.extract_receipt_fields(
                file_bytes=receipt.content,
                mime_type=receipt.content_type,
                filename=receipt.filename,
            )

        merged = {
            "entry_date": normalized.get("entry_date") or ai_suggestion.get("entry_date", "") or date.today().isoformat(),
            "entry_type": normalized.get("entry_type") or ai_suggestion.get("entry_type", "") or "expense",
            "category": normalized.get("category") or ai_suggestion.get("category", "") or "general_expense",
            "amount": normalized.get("amount") or ai_suggestion.get("amount", "") or "0",
            "currency": normalized.get("currency") or ai_suggestion.get("currency", "") or "HKD",
            "vendor": normalized.get("vendor") or ai_suggestion.get("vendor", ""),
            "purpose": normalized.get("purpose") or ai_suggestion.get("purpose", ""),
            "payment_method": normalized.get("payment_method") or ai_suggestion.get("payment_method", "") or "other",
            "invoice_no": invoice_no,
            "description": normalized.get("description") or ai_suggestion.get("description", ""),
            "source": "web_form",
            "source_message": normalized.get("description") or json.dumps(fields, ensure_ascii=False),
            "reference_no": reference_no,
            "receipt_status": "received" if receipt else (ai_suggestion.get("receipt_status", "") or "pending"),
            "receipt_file_path": "",
            "deductible_status": normalized.get("deductible_status") or ai_suggestion.get("deductible_status", "") or "review",
            "notes": normalized.get("notes") or ai_suggestion.get("notes", ""),
            "created_by": created_by,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._validate_amount(merged["amount"])
        entry = LedgerEntryCreate(**merged)
        if receipt:
            receipt_path = self.store.save_receipt(receipt, reference_no, merged["entry_date"])
            entry = LedgerEntryCreate(**{**asdict(entry), "receipt_file_path": receipt_path})
        return entry, ai_suggestion

    def save_entry(self, entry: LedgerEntryCreate) -> None:
        self.store.append_entry(entry)

    def list_entries(self) -> list[dict[str, str]]:
        return self.store.list_entries()

    def list_users(self) -> list[dict[str, str]]:
        return self.store.list_users()

    def create_user(self, username: str, password: str, role: str) -> None:
        self.store.create_user(username, password, role)

    def update_user_password(self, username: str, password: str) -> None:
        self.store.update_user_password(username, password)

    def set_user_active(self, username: str, is_active: bool) -> None:
        self.store.set_user_active(username, is_active)

    def build_dashboard(self) -> dict[str, object]:
        entries = self.list_entries()
        summary_rows = self.store.summarize_by_month()
        income_total = Decimal("0")
        expense_total = Decimal("0")
        for row in entries:
            try:
                amount = Decimal(row.get("amount", "0") or "0")
            except InvalidOperation:
                continue
            if row.get("entry_type") == "income":
                income_total += amount
            elif row.get("entry_type") == "expense":
                expense_total += amount
        return {
            "entry_count": len(entries),
            "income_total": f"{income_total:.2f}",
            "expense_total": f"{expense_total:.2f}",
            "net_profit_loss": f"{(income_total - expense_total):.2f}",
            "recent_entries": entries[:20],
            "monthly_summary": summary_rows,
        }

    def _guess_entry_type(self, text: str) -> str:
        lower_text = text.lower()
        if any(keyword in lower_text for keyword in ["received", "收到", "payment from", "client paid", "客户付款"]):
            return "income"
        if any(keyword in lower_text for keyword in ["owner top up", "shareholder loan", "董事垫支", "老板垫付"]):
            return "owner_funding"
        if "transfer" in lower_text and "bank" in lower_text:
            return "transfer"
        return "expense"

    def _validate_amount(self, amount: str) -> None:
        try:
            Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid amount: {amount}") from exc


class MultipartFormData:
    def __init__(self, fields: dict[str, str], files: dict[str, UploadedReceipt]) -> None:
        self.fields = fields
        self.files = files


def parse_multipart(headers, body: bytes) -> MultipartFormData:
    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        text_fields = {key: values[0] for key, values in parse_qs(body.decode("utf-8")).items()}
        return MultipartFormData(text_fields, {})

    raw_message = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    message = BytesParser(policy=default).parsebytes(raw_message)
    fields: dict[str, str] = {}
    files: dict[str, UploadedReceipt] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition") or ""
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = UploadedReceipt(
                filename=filename,
                content_type=part.get_content_type() or guess_mime_type(filename),
                content=payload,
            )
        else:
            fields[name] = payload.decode("utf-8", errors="ignore")
    return MultipartFormData(fields, files)


class LedgerFormHandler(BaseHTTPRequestHandler):
    service = FinanceService(use_ai=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._send_html(self._render_login_page())
            return
        if parsed.path == "/logout":
            self._clear_session_and_redirect("/login")
            return
        user = self._require_user()
        if not user:
            return
        if parsed.path == "/":
            self._send_html(self._render_form_page(user))
            return
        if parsed.path == "/reports":
            self._send_html(self._render_reports_page(user))
            return
        if parsed.path == "/admin/users":
            if user["role"] != "admin":
                self.send_error(403, "Forbidden")
                return
            self._send_html(self._render_users_page(user))
            return
        if parsed.path == "/export/ledger.csv":
            self._send_csv("ledger_entries.csv", HK_MINIMAL_LEDGER_HEADERS, self.service.list_entries())
            return
        if parsed.path == "/export/monthly_summary.csv":
            self._send_csv(
                "monthly_summary.csv",
                ["month", "income", "expense", "net_profit_loss"],
                self.service.store.summarize_by_month(),
            )
            return
        self.send_error(404, "Page not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(content_length)
        form_data = parse_multipart(self.headers, payload)

        if parsed.path == "/login":
            username = safe_text(form_data.fields.get("username", ""))
            password = form_data.fields.get("password", "")
            user = self.service.authenticate_user(username, password)
            if not user:
                self._send_html(self._render_login_page("用户名或密码错误"))
                return
            self._set_session_and_redirect(user["username"], user["role"], "/")
            return

        user = self._require_user()
        if not user:
            return

        if parsed.path == "/":
            receipt = form_data.files.get("receipt_file")
            entry, ai_suggestion = self.service.create_entry_from_form(
                form_data.fields,
                receipt=receipt,
                created_by=user["username"],
            )
            self.service.save_entry(entry)
            self._send_html(self._render_success_page(user, entry, ai_suggestion))
            return

        if parsed.path == "/admin/users":
            if user["role"] != "admin":
                self.send_error(403, "Forbidden")
                return
            action = form_data.fields.get("action", "")
            target_username = safe_text(form_data.fields.get("username", ""))
            message = "操作完成"
            try:
                if action == "create":
                    password = form_data.fields.get("password", "")
                    role = form_data.fields.get("role", "staff") or "staff"
                    self.service.create_user(target_username, password, role)
                    message = f"已创建用户：{target_username}"
                elif action == "reset_password":
                    password = form_data.fields.get("password", "")
                    self.service.update_user_password(target_username, password)
                    message = f"已重置密码：{target_username}"
                elif action == "disable":
                    self.service.set_user_active(target_username, False)
                    message = f"已停用用户：{target_username}"
                elif action == "enable":
                    self.service.set_user_active(target_username, True)
                    message = f"已启用用户：{target_username}"
            except sqlite3.IntegrityError:
                message = "用户名已存在"
            self._send_html(self._render_users_page(user, message))
            return

        self.send_error(404, "Page not found")

    def _parse_cookies(self) -> cookies.SimpleCookie[str]:
        jar: cookies.SimpleCookie[str] = cookies.SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        return jar

    def _current_user(self) -> dict[str, str] | None:
        jar = self._parse_cookies()
        session_cookie = jar.get("orbis_session")
        if not session_cookie:
            return None
        return self.service.auth.verify_session_token(session_cookie.value)

    def _require_user(self) -> dict[str, str] | None:
        user = self._current_user()
        if user:
            return user
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()
        return None

    def _set_session_and_redirect(self, username: str, role: str, location: str) -> None:
        token = self.service.auth.create_session_token(username, role)
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", f"orbis_session={token}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _clear_session_and_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", "orbis_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.end_headers()

    def _send_html(self, body: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _send_csv(self, filename: str, headers: list[str], rows: list[dict[str, str]]) -> None:
        output = [",".join(headers)]
        for row in rows:
            output.append(",".join(self._csv_escape(row.get(header, "")) for header in headers))
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write("\n".join(output).encode("utf-8"))

    def _csv_escape(self, value: str) -> str:
        escaped = str(value).replace('"', '""')
        return f'"{escaped}"'

    def _render_layout(self, title: str, content: str, user: dict[str, str] | None = None) -> str:
        nav = ""
        if user:
            admin_link = '<a href="/admin/users">用户管理</a>' if user["role"] == "admin" else ""
            nav = f"""
            <nav>
              <a href="/">结构化录入</a>
              <a href="/reports">财务报表</a>
              <a href="/export/ledger.csv">导出明细 CSV</a>
              <a href="/export/monthly_summary.csv">导出月报 CSV</a>
              {admin_link}
              <span class="nav-user">当前用户：{html.escape(user['username'])}（{html.escape(user['role'])}）</span>
              <a href="/logout">退出登录</a>
            </nav>
            """
        return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 1180px; margin: 32px auto; padding: 0 16px; color: #111827; background: #f9fafb; }}
    nav {{ display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; align-items: center; }}
    nav a {{ color: #1d4ed8; text-decoration: none; font-weight: 600; }}
    .nav-user {{ color: #4b5563; font-size: 14px; }}
    .card {{ background: #fff; border-radius: 14px; padding: 20px; box-shadow: 0 6px 24px rgba(15, 23, 42, 0.06); margin-bottom: 20px; }}
    .hint {{ color: #4b5563; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }}
    .metric {{ background: #eff6ff; border-radius: 12px; padding: 16px; }}
    .metric h3 {{ margin: 0 0 8px; font-size: 14px; color: #1e3a8a; }}
    .metric strong {{ font-size: 24px; }}
    label {{ font-size: 14px; font-weight: 600; }}
    input, select, textarea {{ width: 100%; padding: 10px; margin: 8px 0 16px; border: 1px solid #d1d5db; border-radius: 10px; box-sizing: border-box; }}
    button {{ padding: 12px 18px; background: #111827; color: white; border: 0; border-radius: 10px; cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
    th {{ background: #f3f4f6; }}
    .actions {{ display: flex; gap: 12px; flex-wrap: wrap; }}
    .pill {{ display: inline-block; background: #e5e7eb; border-radius: 999px; padding: 6px 10px; font-size: 12px; color: #374151; margin: 4px 8px 0 0; }}
    .message {{ background: #ecfeff; color: #155e75; padding: 12px 14px; border-radius: 10px; margin-bottom: 16px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #0f172a; color: #e5e7eb; padding: 16px; border-radius: 12px; }}
    @media (max-width: 820px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  {nav}
  {content}
</body>
</html>
""".strip()

    def _render_login_page(self, error: str = "") -> str:
        message = f'<div class="message">{html.escape(error)}</div>' if error else ""
        content = f"""
<div class="card" style="max-width: 480px; margin: 80px auto;">
  <h1>员工登录</h1>
  <p class="hint">公司员工通过用户名和密码登录；账号由后台统一管理。</p>
  {message}
  <form method="post" action="/login">
    <label>用户名</label>
    <input name="username" autocomplete="username" />
    <label>密码</label>
    <input type="password" name="password" autocomplete="current-password" />
    <button type="submit">登录</button>
  </form>
</div>
""".strip()
        return self._render_layout("员工登录", content)

    def _render_form_page(self, user: dict[str, str]) -> str:
        content = """
<div class="card">
  <h1>结构化财务录入</h1>
  <p class="hint">数据已改为数据库存储；员工登录后可录入，公司管理员可统一管理账号。</p>
  <form method="post" enctype="multipart/form-data">
    <div class="grid">
      <div><label>日期</label><input type="date" name="entry_date" /></div>
      <div><label>类型</label><select name="entry_type"><option value="expense">支出</option><option value="income">收入</option><option value="transfer">转账</option><option value="owner_funding">董事垫资</option></select></div>
      <div><label>金额</label><input name="amount" placeholder="156.30" /></div>
      <div><label>币种</label><select name="currency"><option value="HKD">HKD</option><option value="USD">USD</option><option value="CNY">CNY</option><option value="EUR">EUR</option><option value="PKR">PKR</option><option value="AED">AED</option></select></div>
      <div><label>商户 / 对方名称</label><input name="vendor" placeholder="OpenAI" /></div>
      <div><label>用途</label><input name="purpose" placeholder="API 订阅 / 客户付款 / 办公用品" /></div>
      <div><label>分类</label><input name="category" placeholder="software / sales_income / office" /></div>
      <div><label>支付方式</label><select name="payment_method"><option value="credit_card">信用卡</option><option value="bank_transfer">银行转账</option><option value="fps">FPS</option><option value="cash">现金</option><option value="crypto">Crypto</option><option value="other">其他</option></select></div>
      <div><label>发票号（可留空自动生成）</label><input name="invoice_no" placeholder="RCPT-20260705-0001" /></div>
      <div><label>参考编号（可留空自动生成）</label><input name="reference_no" placeholder="TXN-20260705-0001" /></div>
    </div>
    <label>说明 / 备注</label>
    <textarea name="description" rows="4" placeholder="例如：OpenAI API 七月订阅，公司业务使用"></textarea>
    <label>原始单据上传</label>
    <input type="file" name="receipt_file" accept=".png,.jpg,.jpeg,.webp,.pdf" />
    <p class="hint">支持图片和 PDF；如已配置 `OPENAI_API_KEY`，上传图片时会尝试自动识别单据内容。</p>
    <button type="submit">保存记录</button>
  </form>
</div>
""".strip()
        return self._render_layout("结构化财务录入", content, user)

    def _render_success_page(self, user: dict[str, str], entry: LedgerEntryCreate, ai_suggestion: dict[str, str]) -> str:
        saved_json = html.escape(json.dumps(asdict(entry), ensure_ascii=False, indent=2))
        ai_json = html.escape(json.dumps(ai_suggestion, ensure_ascii=False, indent=2)) if ai_suggestion else "未使用自动识别或未识别出可用字段。"
        content = f"""
<div class="card">
  <h1>已保存</h1>
  <p class="hint">记录已写入数据库，并绑定录入员工 `{html.escape(entry.created_by)}`。</p>
  <div class="actions"><a href="/">继续录入</a><a href="/reports">查看报表</a></div>
</div>
<div class="card"><h2>保存结果</h2><pre>{saved_json}</pre></div>
<div class="card"><h2>自动识别结果</h2><pre>{ai_json}</pre></div>
""".strip()
        return self._render_layout("已保存", content, user)

    def _render_reports_page(self, user: dict[str, str], message: str = "") -> str:
        dashboard = self.service.build_dashboard()
        summary_rows = self._render_summary_rows(dashboard["monthly_summary"])
        recent_rows = self._render_entry_rows(dashboard["recent_entries"])
        banner = f'<div class="message">{html.escape(message)}</div>' if message else ""
        content = f"""
<div class="card">
  <h1>财务报表</h1>
  {banner}
  <p class="hint">报表数据已存入数据库，适合后续部署到线上并集中管理。</p>
  <div class="metrics">
    <div class="metric"><h3>记录笔数</h3><strong>{dashboard["entry_count"]}</strong></div>
    <div class="metric"><h3>总收入</h3><strong>{dashboard["income_total"]}</strong></div>
    <div class="metric"><h3>总支出</h3><strong>{dashboard["expense_total"]}</strong></div>
    <div class="metric"><h3>净利润 / 净亏损</h3><strong>{dashboard["net_profit_loss"]}</strong></div>
  </div>
</div>
<div class="card">
  <h2>月度汇总</h2>
  <div class="actions"><a href="/export/monthly_summary.csv">下载月报 CSV</a></div>
  <table><thead><tr><th>月份</th><th>收入</th><th>支出</th><th>净额</th></tr></thead><tbody>{summary_rows}</tbody></table>
</div>
<div class="card">
  <h2>最近 20 笔明细</h2>
  <div class="actions"><a href="/export/ledger.csv">下载明细 CSV</a></div>
  <table>
    <thead><tr><th>日期</th><th>类型</th><th>分类</th><th>金额</th><th>商户</th><th>用途</th><th>支付方式</th><th>发票号</th><th>参考号</th><th>录入人</th></tr></thead>
    <tbody>{recent_rows}</tbody>
  </table>
</div>
""".strip()
        return self._render_layout("财务报表", content, user)

    def _render_users_page(self, user: dict[str, str], message: str = "") -> str:
        rows = []
        for item in self.service.list_users():
            next_action = "disable" if str(item["is_active"]) == "1" else "enable"
            next_label = "停用" if next_action == "disable" else "启用"
            rows.append(
                "<tr>"
                f"<td>{html.escape(item['username'])}</td>"
                f"<td>{html.escape(item['role'])}</td>"
                f"<td>{'启用' if str(item['is_active']) == '1' else '停用'}</td>"
                f"<td>{html.escape(item['created_at'])}</td>"
                f"<td>"
                f"<form method='post' style='display:inline-block; margin-right:8px;'>"
                f"<input type='hidden' name='action' value='{next_action}' />"
                f"<input type='hidden' name='username' value='{html.escape(item['username'])}' />"
                f"<button type='submit'>{next_label}</button></form>"
                f"</td>"
                "</tr>"
            )
        banner = f'<div class="message">{html.escape(message)}</div>' if message else ""
        content = f"""
<div class="card">
  <h1>用户管理</h1>
  {banner}
  <p class="hint">管理员可统一创建员工账号、重置密码、启用/停用账号。</p>
</div>
<div class="card">
  <h2>创建新用户</h2>
  <form method="post">
    <input type="hidden" name="action" value="create" />
    <div class="grid">
      <div><label>用户名</label><input name="username" /></div>
      <div><label>角色</label><select name="role"><option value="staff">staff</option><option value="admin">admin</option></select></div>
      <div><label>初始密码</label><input type="password" name="password" /></div>
    </div>
    <button type="submit">创建用户</button>
  </form>
</div>
<div class="card">
  <h2>重置密码</h2>
  <form method="post">
    <input type="hidden" name="action" value="reset_password" />
    <div class="grid">
      <div><label>用户名</label><input name="username" /></div>
      <div><label>新密码</label><input type="password" name="password" /></div>
    </div>
    <button type="submit">重置密码</button>
  </form>
</div>
<div class="card">
  <h2>现有用户</h2>
  <table>
    <thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
""".strip()
        return self._render_layout("用户管理", content, user)

    def _render_entry_rows(self, rows: list[dict[str, str]]) -> str:
        if not rows:
            return '<tr><td colspan="10">暂无数据。</td></tr>'
        rendered = []
        for row in rows:
            rendered.append(
                "<tr>"
                f"<td>{html.escape(row.get('entry_date', ''))}</td>"
                f"<td>{html.escape(row.get('entry_type', ''))}</td>"
                f"<td>{html.escape(row.get('category', ''))}</td>"
                f"<td>{html.escape(row.get('currency', ''))} {html.escape(row.get('amount', ''))}</td>"
                f"<td>{html.escape(row.get('vendor', ''))}</td>"
                f"<td>{html.escape(row.get('purpose', ''))}</td>"
                f"<td>{html.escape(row.get('payment_method', ''))}</td>"
                f"<td>{html.escape(row.get('invoice_no', ''))}</td>"
                f"<td>{html.escape(row.get('reference_no', ''))}</td>"
                f"<td>{html.escape(row.get('created_by', ''))}</td>"
                "</tr>"
            )
        return "".join(rendered)

    def _render_summary_rows(self, rows: list[dict[str, str]]) -> str:
        if not rows:
            return '<tr><td colspan="4">暂无数据。</td></tr>'
        return "".join(
            "<tr>"
            f"<td>{html.escape(row['month'])}</td>"
            f"<td>{html.escape(row['income'])}</td>"
            f"<td>{html.escape(row['expense'])}</td>"
            f"<td>{html.escape(row['net_profit_loss'])}</td>"
            "</tr>"
            for row in rows
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deployable HK company bookkeeping assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add-entry", help="Add one ledger entry from simple text")
    add_parser.add_argument("--text", required=True, help="Expense or income description")
    add_parser.add_argument("--source", default="manual", help="manual / telegram / web_form")
    add_parser.add_argument("--reference-no", default="", help="Invoice or receipt reference")
    add_parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to improve classification")
    add_parser.add_argument("--created-by", default="system")

    create_user_parser = subparsers.add_parser("create-user", help="Create or update a user")
    create_user_parser.add_argument("--username", required=True)
    create_user_parser.add_argument("--password", required=True)
    create_user_parser.add_argument("--role", default="staff", choices=["staff", "admin"])

    web_parser = subparsers.add_parser("serve-form", help="Serve the app")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")))
    return parser


async def main() -> None:
    args = build_parser().parse_args()

    if args.command == "add-entry":
        service = FinanceService(use_ai=args.use_ai)
        entry = service.create_entry_from_text(
            text=args.text,
            source=args.source,
            reference_no=args.reference_no,
            created_by=args.created_by,
        )
        service.save_entry(entry)
        print(json.dumps(asdict(entry), ensure_ascii=False, indent=2))
        return

    if args.command == "create-user":
        service = FinanceService(use_ai=False)
        try:
            service.create_user(args.username, args.password, args.role)
            print(f"User created: {args.username}")
        except sqlite3.IntegrityError:
            service.update_user_password(args.username, args.password)
            print(f"User password updated: {args.username}")
        return

    if args.command == "serve-form":
        server = ThreadingHTTPServer((args.host, args.port), LedgerFormHandler)
        print(f"Serving ledger form on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
