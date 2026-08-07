from __future__ import annotations

import csv as csv_module
from io import BytesIO

from pypdf import PdfReader

from src.exports import SAGE_STOCK_COLUMNS, quote_pdf, sage_stock_import_csv


def test_quote_and_sage_exports() -> None:
    record = {
        "quote_reference": "Q-TEST",
        "customer_name": "Customer",
        "item_code": "BOX-TEST",
        "description": (
            "A long customer-facing product description for a printed solid board "
            "packaging item which must wrap safely within the quotation table"
        ),
        "order_quantity": 10_000,
        "order_pallets": 10,
        "fulfilment_type": "MTC",
        "agreement_term_months": 12,
        "delivery_pallets_per_calloff": 1,
        "estimated_delivery_count": 10,
        "pallet_holding_charge_per_pallet_per_week": 3,
        "selling_price_per_1000": 123.4567,
        "selling_price_per_item": 0.1234567,
        "delivery_method": "Haulier",
        "transport_vendor": "Joda",
        "transport_service": "Economy",
        "transport_booking": "AM/PM",
        "delivery_postcode": "BD20 0AA",
        "product_group": "Finished goods",
        "manufacturing_site": 101,
        "net_mass_kg": 0.6,
        "length_mm": 574,
        "width_mm": 376,
        "height_mm": 149,
        "board_gsm": 1250,
        "pallet_quantity": 1000,
        "board_width_mm": 620,
        "board_length_mm": 850,
        "board_code": "4-15614",
        "number_of_colours": 3,
        "fsc": "FSC Mix",
        "notes": "Customer artwork approval is required before manufacture.",
    }

    pdf = quote_pdf(record)
    csv = sage_stock_import_csv(record).decode("utf-8-sig")
    pages = PdfReader(BytesIO(pdf)).pages
    quote_text = pages[0].extract_text()
    terms_text = "\n".join(page.extract_text() or "" for page in pages[1:])

    assert pdf.startswith(b"%PDF")
    assert len(pages) == 4
    assert "0.12346" in quote_text
    assert "0.1234567" not in quote_text
    assert "NOTES" in quote_text
    assert "Customer artwork approval is required" in quote_text
    assert "attached Solidus General Terms and Conditions" in quote_text
    assert (
        "This quotation is generated from the costing tool and remains subject to final commercial approval."
        in quote_text
    )
    assert "General Terms and Condition of Sale" in terms_text
    assert "Stock item code" in csv
    assert "BOX-TEST" in csv
    assert "AnalysisName\\18" in csv
    assert "MTC" in csv
    rows = list(csv_module.reader(csv.splitlines()))
    assert rows[0] == SAGE_STOCK_COLUMNS
    assert len(rows[0]) == 72
    exported = dict(zip(rows[0], rows[1], strict=True))
    assert exported["Asset of stock - account number"] == "10260361"
    assert exported["Revenue - account number"] == "10210065"
    assert exported["AnalysisName\\20"] == "Market Segment"
