from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

from src.esign import DropboxSignClient, Signer
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
        costing_id="C-1",
        quote_reference="Q-1",
    )

    assert captured["data"]["test_mode"] == "1"
    assert captured["data"]["use_text_tags"] == "1"
    assert captured["data"]["signers[1][order]"] == "0"
    assert captured["data"]["signers[2][order]"] == "1"
    assert result["esign_status"] == "awaiting signatures"
    assert result["esign_request_id"] == "req-test"


def test_esign_pdf_contains_director_and_customer_tags() -> None:
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
        },
        esign_tags=True,
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert "[sig|req|signer1]" in text
    assert "[sig|req|signer2]" in text
    assert "Approved in costing tool by" in text
