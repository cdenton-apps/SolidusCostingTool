from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw
from pypdf import PdfReader

import pytest

from src.esign import (
    DropboxSignClient,
    ESignError,
    Signer,
    append_commercial_signature_page,
    commercial_approval_recipient,
)
from src.exports import quote_pdf


class _Response:
    ok = True
    status_code = 200

    def json(self):
        return {
            "signature_request": {
                "signature_request_id": "req-test",
                "test_mode": True,
                "is_complete": False,
                "signatures": [
                    {
                        "signer_name": "Director",
                        "signer_email_address": "director@example.com",
                        "status_code": "awaiting_signature",
                    },
                    {
                        "signer_name": "Customer",
                        "signer_email_address": "customer@example.com",
                        "status_code": "awaiting_signature",
                    },
                ],
            }
        }


def _signature_png() -> bytes:
    image = Image.new("RGB", (240, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.line((20, 55, 80, 20, 135, 55, 215, 25), fill="black", width=4)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_send_is_forced_to_test_mode_and_orders_signers(monkeypatch) -> None:
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr("src.esign.requests.post", fake_post)
    result = DropboxSignClient("secret").send_test_request(
        b"%PDF-test",
        title="Quote",
        subject="Please sign",
        message="Test",
        director=Signer("Director", "director@example.com", 0),
        customer=Signer("Customer", "customer@example.com", 1),
        cc_email="sales.rep@example.com",
        costing_id="C-1",
        quote_reference="Q-1",
    )

    assert captured["data"]["test_mode"] == "1"
    assert captured["data"]["use_text_tags"] == "1"
    assert captured["data"]["field_options[date_format]"] == "DD / MM / YYYY"
    assert captured["data"]["locale"] == "en-GB"
    assert captured["data"]["signers[1][order]"] == "0"
    assert captured["data"]["signers[2][order]"] == "1"
    assert captured["data"]["cc_email_addresses[0]"] == "sales.rep@example.com"
    assert result["esign_status"] == "awaiting signatures"
    assert result["esign_request_id"] == "req-test"


def test_customer_only_request_uses_signer_one_and_ccs_sales_rep(monkeypatch) -> None:
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr("src.esign.requests.post", fake_post)
    DropboxSignClient("secret").send_test_request(
        b"%PDF-test",
        title="Quote",
        subject="Please sign",
        message="Test",
        customer=Signer("Customer", "customer@example.com", 0),
        cc_email="sales.rep@example.com",
        costing_id="C-2",
        quote_reference="Q-2",
    )

    assert captured["data"]["signers[1][name]"] == "Customer"
    assert captured["data"]["signers[1][order]"] == "0"
    assert "signers[2][name]" not in captured["data"]
    assert captured["data"]["cc_email_addresses[0]"] == "sales.rep@example.com"


def test_commercial_approval_routes_and_absence_cover() -> None:
    settings = {
        "director_name": "Director",
        "director_email": "director@example.com",
        "amber_approver_name": "Amber Approver",
        "amber_approver_email": "amber@example.com",
    }

    assert commercial_approval_recipient(settings, "green") is None
    assert commercial_approval_recipient(settings, "amber").email == "amber@example.com"
    assert commercial_approval_recipient(settings, "red").email == "director@example.com"

    settings["amber_approver_absent"] = True
    amber_cover = commercial_approval_recipient(settings, "amber")
    assert amber_cover.email == "director@example.com"
    assert amber_cover.is_cover is True

    settings["amber_approver_absent"] = False
    settings["director_absent"] = True
    red_cover = commercial_approval_recipient(settings, "red")
    assert red_cover.email == "amber@example.com"
    assert red_cover.is_cover is True


def test_both_commercial_approvers_absent_blocks_sending() -> None:
    with pytest.raises(ESignError, match="Both commercial approvers"):
        commercial_approval_recipient(
            {"amber_approver_absent": True, "director_absent": True}, "amber"
        )


def test_green_esign_pdf_contains_saved_rep_signature_and_customer_tags() -> None:
    pdf = quote_pdf(
        {
            "quote_reference": "Q-TAGS",
            "customer_name": "Customer",
            "customer_contact": "Buyer",
            "item_code": "ITEM",
            "description": "Description",
            "fulfilment_type": "MTO",
            "order_quantity": 1000,
            "order_pallets": 1,
            "selling_price_per_1000": 100,
            "selling_price_per_item": 0.1,
            "esign_approved_by_name": "Sales Rep",
            "esign_approved_at_utc": "2026-08-11T10:00:00+00:00",
            "created_at_utc": "2026-08-11T09:30:00+00:00",
            "traffic_light_status": "green",
            "sales_rep_signature_name": "Sales Rep",
            "sales_rep_signature_applied_at_utc": "2026-08-11T10:00:00+00:00",
            "_sales_rep_signature_png": _signature_png(),
        },
        esign_tags=True,
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert "[sig|req|signer1]" in text
    assert "[sig|req|signer2]" not in text
    assert "[text|req|signer1|Full name]" in text
    assert "[text|req|signer2|Full name]" not in text
    assert "[date|req|signer1|Signing date]" in text
    assert "[date|req|signer2|Signing date]" not in text
    assert "Sales Representative" in text
    assert "Name: Sales Rep" in text
    assert "Sales Director or delegated individual" not in text
    assert "11/08/2026" in text
    assert "11/08/2026 11:00" not in text
    assert "Quotation date" in text
    assert "11/08/2026" in text
    assert text.count("Date:") >= 2
    assert "Time:" not in text
    assert "SUBJECT TO FINAL COMMERCIAL APPROVAL" not in text


def test_red_esign_pdf_contains_director_and_customer_layout() -> None:
    pdf = quote_pdf(
        {
            "quote_reference": "Q-RED",
            "customer_name": "Customer",
            "customer_contact": "Buyer",
            "item_code": "ITEM",
            "description": "Description",
            "fulfilment_type": "MTO",
            "order_quantity": 1000,
            "order_pallets": 1,
            "selling_price_per_1000": 100,
            "selling_price_per_item": 0.1,
            "traffic_light_status": "red",
            "sales_rep_signature_name": "Sales Rep",
            "sales_rep_signature_applied_at_utc": "2026-08-11T10:00:00+00:00",
            "_sales_rep_signature_png": _signature_png(),
        },
        esign_tags=True,
    )
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )

    assert "Sales Director or delegated individual" in text
    assert "Sales Representative approval" in text
    assert "Name: Sales Rep" in text
    assert "Customer" in text
    assert "[sig|req|signer1]" in text
    assert "[sig|req|signer2]" in text


def test_amber_esign_pdf_contains_amber_approver_and_customer_layout() -> None:
    quotation = quote_pdf(
        {
            "quote_reference": "Q-AMBER",
            "customer_name": "Customer",
            "customer_contact": "Buyer",
            "item_code": "ITEM",
            "description": "Description",
            "fulfilment_type": "MTO",
            "order_quantity": 1000,
            "order_pallets": 1,
            "selling_price_per_1000": 100,
            "selling_price_per_item": 0.1,
            "traffic_light_status": "amber",
            "approval_recipient_role": "Amber commercial approver",
            "sales_rep_signature_name": "Sales Rep",
            "sales_rep_signature_applied_at_utc": "2026-08-11T10:00:00+00:00",
            "_sales_rep_signature_png": _signature_png(),
        },
        esign_tags=False,
    )
    original_pages = len(PdfReader(BytesIO(quotation)).pages)
    pdf = append_commercial_signature_page(
        quotation,
        approval_role="Amber commercial approver",
        customer_role="Buyer",
    )
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )

    assert "Amber commercial approver" in text
    assert "[sig|req|signer1]" in text
    assert "[sig|req|signer2]" in text
    assert len(PdfReader(BytesIO(pdf)).pages) == original_pages + 1


def test_green_pdf_without_saved_signature_keeps_legacy_two_signer_tags() -> None:
    pdf = quote_pdf(
        {
            "quote_reference": "Q-LEGACY-GREEN",
            "customer_name": "Customer",
            "customer_contact": "Buyer",
            "item_code": "ITEM",
            "description": "Description",
            "fulfilment_type": "MTO",
            "order_quantity": 1000,
            "order_pallets": 1,
            "selling_price_per_1000": 100,
            "selling_price_per_item": 0.1,
            "traffic_light_status": "green",
        },
        esign_tags=True,
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert "Sales Representative" in text
    assert "[sig|req|signer1]" in text
    assert "[sig|req|signer2]" in text


def test_red_pdf_without_saved_signature_keeps_director_customer_layout() -> None:
    pdf = quote_pdf(
        {
            "quote_reference": "Q-LEGACY-RED",
            "customer_name": "Customer",
            "customer_contact": "Buyer",
            "item_code": "ITEM",
            "description": "Description",
            "fulfilment_type": "MTO",
            "order_quantity": 1000,
            "order_pallets": 1,
            "selling_price_per_1000": 100,
            "selling_price_per_item": 0.1,
            "traffic_light_status": "red",
        },
        esign_tags=True,
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert "Sales Representative" not in text
    assert "Sales Director or delegated individual" in text
    assert "[sig|req|signer1]" in text
    assert "[sig|req|signer2]" in text
