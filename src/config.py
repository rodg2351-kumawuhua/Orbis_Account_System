from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args, **_kwargs):
        return False

load_dotenv(BASE_DIR / ".env")


HK_MINIMAL_LEDGER_HEADERS = [
    "entry_date",
    "entry_type",
    "category",
    "amount",
    "currency",
    "vendor",
    "purpose",
    "payment_method",
    "invoice_no",
    "description",
    "source",
    "source_message",
    "reference_no",
    "receipt_status",
    "receipt_file_path",
    "deductible_status",
    "notes",
    "created_by",
    "created_at",
]


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int | None
    telegram_api_hash: str
    telegram_session_name: str
    openai_api_key: str
    openai_model: str
    app_secret_key: str
    database_url: str
    database_path: str
    bootstrap_admin_username: str
    bootstrap_admin_password: str
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str
    google_service_account_file: str
    google_sheets_spreadsheet_id: str
    google_sheets_ledger_worksheet_name: str
    google_sheets_summary_worksheet_name: str


def _get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_accounting_settings() -> Settings:
    api_id = _get_env("TELEGRAM_API_ID")
    return Settings(
        telegram_api_id=int(api_id) if api_id else None,
        telegram_api_hash=_get_env("TELEGRAM_API_HASH"),
        telegram_session_name=_get_env("TELEGRAM_SESSION_NAME", "orbis_finance_bot"),
        openai_api_key=_get_env("OPENAI_API_KEY"),
        openai_model=_get_env("OPENAI_MODEL", "gpt-4.1-mini"),
        app_secret_key=_get_env("APP_SECRET_KEY", "change-this-in-production"),
        database_url=_get_env("DATABASE_URL"),
        database_path=_get_env("DATABASE_PATH", "data/orbis_finance.db"),
        bootstrap_admin_username=_get_env("BOOTSTRAP_ADMIN_USERNAME", "admin"),
        bootstrap_admin_password=_get_env("BOOTSTRAP_ADMIN_PASSWORD", "ChangeMe123!"),
        cloudinary_cloud_name=_get_env("CLOUDINARY_CLOUD_NAME"),
        cloudinary_api_key=_get_env("CLOUDINARY_API_KEY"),
        cloudinary_api_secret=_get_env("CLOUDINARY_API_SECRET"),
        google_service_account_file=_get_env("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/google-service-account.json"),
        google_sheets_spreadsheet_id=_get_env("GOOGLE_SHEETS_SPREADSHEET_ID"),
        google_sheets_ledger_worksheet_name=_get_env("GOOGLE_SHEETS_LEDGER_WORKSHEET_NAME", "ledger_entries"),
        google_sheets_summary_worksheet_name=_get_env("GOOGLE_SHEETS_SUMMARY_WORKSHEET_NAME", "monthly_summary"),
    )
