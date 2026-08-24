from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any

import requests
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


API_BASE = "https://api.hellosign.com/v3"


class ESignError(RuntimeError):
    """A safe, user-facing Dropbox Sign integration error."""


@dataclass(frozen=True)
class Signer:
    name: str
    email: str
    order: int


@dataclass(frozen=True)
class ApprovalRecipient:
    name: str
    email: str
    role: str
    is_cover: bool = False


def _setting_flag(settings: dict[str, Any], key: str) -> bool:
    value = settings.get(key, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def commercial_approval_recipient(
    settings: dict[str, Any], traffic_status: str
) -> ApprovalRecipient | None:
    """Resolve the internal signer, including explicit absence cover."""
    status = str(traffic_status or "").strip().casefold()
    if status not in {"amber", "red"}:
        return None

    amber_absent = _setting_flag(settings, "amber_approver_absent")
    director_absent = _setting_flag(settings, "director_absent")
    if amber_absent and director_absent:
        raise ESignError(
            "Both commercial approvers are marked absent in Streamlit Secrets. "
            "Update the cover settings before sending this quotation."
        )

    if status == "amber" and not amber_absent:
        return ApprovalRecipient(
            str(settings.get("amber_approver_name", "") or "").strip(),
            str(settings.get("amber_approver_email", "") or "").strip(),
            "Amber commercial approver",
        )
    if status == "red" and not director_absent:
        return ApprovalRecipient(
            str(settings.get("director_name", "") or "").strip(),
            str(settings.get("director_email", "") or "").strip(),
            "Sales Director or delegated individual",
        )
    if status == "amber":
        return ApprovalRecipient(
            str(settings.get("director_name", "") or "").strip(),
            str(settings.get("director_email", "") or "").strip(),
            "Sales Director covering amber approval",
            True,
        )
    return ApprovalRecipient(
        str(settings.get("amber_approver_name", "") or "").strip(),
        str(settings.get("amber_approver_email", "") or "").strip(),
        "Amber approver covering Sales Director",
        True,
    )


def append_commercial_signature_page(
    pdf: bytes,
    *,
    approval_role: str,
    customer_role: str = "",
) -> bytes:
    """Append generic signer-one and customer signer-two Dropbox Sign fields."""
    if not pdf.startswith(b"%PDF"):
        raise ESignError("The quotation PDF could not be prepared.")

    page = BytesIO()
    document = canvas.Canvas(page, pagesize=A4)
    page_width, page_height = A4
    yellow = HexColor("#FFDD00")
    ink = HexColor("#1A1A1A")
    grey = HexColor("#666666")

    document.setFillColor(yellow)
    document.rect(18 * mm, page_height - 38 * mm, page_width - 36 * mm, 16 * mm, fill=1, stroke=0)
    document.setFillColor(ink)
    document.setFont("Helvetica-Bold", 16)
    document.drawString(24 * mm, page_height - 32 * mm, "Quotation approval and acceptance")
    document.setFont("Helvetica", 9)
    document.setFillColor(grey)
    document.drawString(
        18 * mm,
        page_height - 49 * mm,
        "The Solidus commercial approver signs first. The Customer follows after that approval.",
    )

    def signer_panel(top: float, heading: str, role: str, signer: int) -> None:
        left = 18 * mm
        width = page_width - 36 * mm
        height = 55 * mm
        document.setStrokeColor(HexColor("#A8A8A8"))
        document.rect(left, top - height, width, height, fill=0, stroke=1)
        document.setFillColor(yellow)
        document.rect(left, top - 10 * mm, width, 10 * mm, fill=1, stroke=0)
        document.setFillColor(ink)
        document.setFont("Helvetica-Bold", 11)
        document.drawString(left + 5 * mm, top - 6.5 * mm, heading)
        document.setFont("Helvetica", 9)
        document.drawString(left + 5 * mm, top - 18 * mm, f"Role: {role}")
        document.drawString(left + 5 * mm, top - 29 * mm, "Name:")
        document.drawString(left + 5 * mm, top - 41 * mm, "Signature:")
        document.drawString(left + 105 * mm, top - 41 * mm, "Date:")
        # Dropbox Sign recognises the white text tags and replaces them with fields.
        document.setFillColor(white)
        document.drawString(
            left + 24 * mm,
            top - 29 * mm,
            f"[text|req|signer{signer}|Full name]",
        )
        document.drawString(
            left + 24 * mm, top - 41 * mm, f"[sig|req|signer{signer}]"
        )
        document.drawString(
            left + 119 * mm,
            top - 41 * mm,
            f"[date|req|signer{signer}|Signing date]",
        )

    signer_panel(
        page_height - 61 * mm,
        "Solidus commercial approval",
        str(approval_role or "Commercial approver"),
        1,
    )
    signer_panel(
        page_height - 124 * mm,
        "Customer acceptance",
        str(customer_role or "Customer"),
        2,
    )
    document.setFillColor(grey)
    document.setFont("Helvetica", 8)
    document.drawString(
        18 * mm,
        16 * mm,
        "This signature page forms part of the attached Solidus quotation.",
    )
    document.save()
    page.seek(0)

    writer = PdfWriter()
    for existing_page in PdfReader(BytesIO(pdf)).pages:
        writer.add_page(existing_page)
    writer.add_page(PdfReader(page).pages[0])
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


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
        customer: Signer,
        director: Signer | None = None,
        cc_email: str = "",
        costing_id: str,
        quote_reference: str,
    ) -> dict[str, Any]:
        if not pdf.startswith(b"%PDF"):
            raise ESignError("The quotation PDF could not be prepared.")
        if director and director.email.casefold() == customer.email.casefold():
            raise ESignError(
                "Use different email addresses for the Solidus and Customer signers."
            )
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
        }
        if director:
            data.update(
                {
                    "signers[1][name]": director.name,
                    "signers[1][email_address]": director.email,
                    "signers[1][order]": str(director.order),
                    "signers[2][name]": customer.name,
                    "signers[2][email_address]": customer.email,
                    "signers[2][order]": str(customer.order),
                }
            )
        else:
            data.update(
                {
                    "signers[1][name]": customer.name,
                    "signers[1][email_address]": customer.email,
                    "signers[1][order]": str(customer.order),
                }
            )
        cc_email = str(cc_email or "").strip()
        signer_emails = {customer.email.casefold()}
        if director:
            signer_emails.add(director.email.casefold())
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
