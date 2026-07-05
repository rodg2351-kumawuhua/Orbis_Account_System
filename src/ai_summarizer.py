from __future__ import annotations

import base64
import json


class AIFinanceAssistant:
    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model

    def suggest_ledger_fields(self, raw_text: str) -> dict[str, str]:
        prompt = f"""
You are an accounting assistant for a very small Hong Kong information-services company.

Convert the following raw bookkeeping note into a compact JSON object.
Rules:
- Return valid JSON only.
- Use conservative assumptions.
- entry_type must be one of: income, expense, transfer, owner_funding.
- category should be simple and practical for a small company.
- receipt_status should be one of: received, pending, missing.
- deductible_status should be one of: yes, no, review.
- If a field is unknown, use empty string, except receipt_status default pending and deductible_status default review.

Required JSON keys:
entry_date, entry_type, category, amount, currency, vendor, purpose, payment_method, invoice_no, description, receipt_status, deductible_status, notes

Raw note:
{raw_text}
""".strip()
        response = self._client.responses.create(model=self._model, input=prompt)
        return json.loads(response.output_text)

    def extract_receipt_fields(
        self,
        file_bytes: bytes,
        mime_type: str,
        filename: str,
    ) -> dict[str, str]:
        data_url = f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode('ascii')}"
        prompt = f"""
You are an accounting assistant for a very small Hong Kong information-services company.

Read this receipt or invoice image and return a compact JSON object only.

Rules:
- Return valid JSON only.
- Be conservative. If unreadable, leave fields empty.
- currency should prefer HKD, USD, CNY when visible.
- payment_method should be one of: cash, credit_card, bank_transfer, fps, other, or empty.
- receipt_status should be one of: received, pending, missing.
- deductible_status should be one of: yes, no, review.

Required JSON keys:
entry_date, entry_type, category, amount, currency, vendor, purpose, payment_method, invoice_no, description, receipt_status, deductible_status, notes

Filename:
{filename}
""".strip()
        response = self._client.responses.create(
            model=self._model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
        )
        return json.loads(response.output_text)
