from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


class GoogleSheetsLedgerWriter:
    def __init__(
        self,
        service_account_file: str,
        spreadsheet_id: str,
        ledger_worksheet_name: str,
        summary_worksheet_name: str,
    ) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(str(Path(service_account_file)), scopes=scopes)
        client = gspread.authorize(creds)
        self._spreadsheet = client.open_by_key(spreadsheet_id)
        self._ledger = self._spreadsheet.worksheet(ledger_worksheet_name)
        self._summary = self._spreadsheet.worksheet(summary_worksheet_name)

    def _ensure_header(self, worksheet, headers: list[str]) -> None:
        current = worksheet.row_values(1)
        if current == headers:
            return
        if not current:
            worksheet.append_row(headers)
            return
        worksheet.update("1:1", [headers])

    def append_ledger_entry(self, row: dict[str, str]) -> None:
        headers = list(row.keys())
        self._ensure_header(self._ledger, headers)
        self._ledger.append_row([row.get(column, "") for column in headers], value_input_option="USER_ENTERED")

    def upsert_monthly_summary_from_csv(self, ledger_csv_path: Path) -> None:
        buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
        with ledger_csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                month = (row.get("entry_date", "") or "")[:7]
                if len(month) != 7:
                    continue
                amount = float(row.get("amount", "0") or 0)
                entry_type = row.get("entry_type", "expense")
                if entry_type == "income":
                    buckets[month]["income"] += amount
                elif entry_type == "expense":
                    buckets[month]["expense"] += amount

        headers = ["month", "income", "expense", "net_profit_loss"]
        values = [headers]
        for month in sorted(buckets):
            income = buckets[month]["income"]
            expense = buckets[month]["expense"]
            values.append([month, f"{income:.2f}", f"{expense:.2f}", f"{income - expense:.2f}"])

        self._summary.clear()
        self._summary.update("A1", values)
