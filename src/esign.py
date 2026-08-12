from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any

import requests


API_BASE = "https://api.hellosign.com/v3"


class ESignError(RuntimeError):
    """A safe, user-facing Dropbox Sign integration error."""


@dataclass(frozen=True)
class Signer:
    name: str
    email: str
    order: int


def _request_error(response: requests.Response) -> ESignError:
    try:
        payload = response.json()
        message = (
            payload.get("error", {}).get("error_msg")
            or payload.get("error_name")
            or payload.get("message")
        )
    except (ValueError, AttributeError):
        message = None
    if response.status_code == 401:
        message = "Dropbox Sign rejected the API key. Check the Streamlit Secret."
    elif response.status_code == 402:
        message = "This request was not accepted as a test request."
    elif response.status_code == 409:
        message = "Dropbox Sign is still preparing the document. Try again shortly."
    return ESignError(str(message or "Dropbox Sign could not complete the request."))


class DropboxSignClient:
    """Small non-embedded Dropbox Sign client, deliberately locked to test mode."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 30):
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise ESignError("Dropbox Sign is not configured in Streamlit Secrets.")

    @property
    def auth(self) -> tuple[str, str]:
        return (self.api_key, "")

    def send_test_request(
        self,
        pdf: bytes,
        *,
        title: str,
        subject: str,
        message: str,
        director: Signer,
        customer: Signer,
        cc_email: str = "",
        costing_id: str,
        quote_reference: str,
    ) -> dict[str, Any]:
        if not pdf.startswith(b"%PDF"):
            raise ESignError("The quotation PDF could not be prepared.")
        if director.email.casefold() == customer.email.casefold():
            raise ESignError("Use different email addresses for the Director and Customer test signers.")
        data = {
            "title": title[:255],
            "subject": subject[:255],
            "message": message[:5000],
            "test_mode": "1",  # Never allow this app path to create a binding request.
            "use_text_tags": "1",
            "hide_text_tags": "0",
            "allow_decline": "1",
            "field_options[date_format]": "DD / MM / YYYY",
            "locale": "en-GB",
            "metadata[costing_id]": costing_id,
            "metadata[quote_reference]": quote_reference,
            "signers[1][name]": director.name,
            "signers[1][email_address]": director.email,
            "signers[1][order]": str(director.order),
            "signers[2][name]": customer.name,
            "signers[2][email_address]": customer.email,
            "signers[2][order]": str(customer.order),
        }
        cc_email = str(cc_email or "").strip()
        signer_emails = {director.email.casefold(), customer.email.casefold()}
        if cc_email and cc_email.casefold() not in signer_emails:
            data["cc_email_addresses[0]"] = cc_email
        response = requests.post(
            f"{API_BASE}/signature_request/send",
            auth=self.auth,
            data=data,
            files={"files[0]": (f"{quote_reference or costing_id}.pdf", BytesIO(pdf), "application/pdf")},
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise _request_error(response)
        return self.normalise(response.json().get("signature_request", {}))

    def get_request(self, signature_request_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{API_BASE}/signature_request/{signature_request_id}",
            auth=self.auth,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise _request_error(response)
        return self.normalise(response.json().get("signature_request", {}))

    def download_pdf(self, signature_request_id: str) -> bytes:
        response = requests.get(
            f"{API_BASE}/signature_request/files/{signature_request_id}",
            auth=self.auth,
            params={"file_type": "pdf"},
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise _request_error(response)
        return response.content

    @staticmethod
    def normalise(request: dict[str, Any]) -> dict[str, Any]:
        signatures = [
            {
                "name": str(item.get("signer_name", "")),
                "email": str(item.get("signer_email_address", "")),
                "status": str(item.get("status_code", "unknown")),
                "signed_at": item.get("signed_at"),
            }
            for item in request.get("signatures", [])
        ]
        statuses = {item["status"] for item in signatures}
        if request.get("is_complete"):
            status = "complete"
        elif "declined" in statuses:
            status = "declined"
        elif "expired" in statuses:
            status = "expired"
        elif any(value.startswith("error") for value in statuses):
            status = "error"
        elif "signed" in statuses:
            status = "partly signed"
        else:
            status = "awaiting signatures"
        return {
            "esign_request_id": str(request.get("signature_request_id", "")),
            "esign_status": status,
            "esign_is_complete": bool(request.get("is_complete", False)),
            "esign_is_declined": bool(request.get("is_declined", False)),
            "esign_signers": signatures,
            "esign_test_mode": bool(request.get("test_mode", True)),
        }
