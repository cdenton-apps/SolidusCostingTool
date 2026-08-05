from __future__ import annotations

from src.exports import quote_pdf, sage_stock_import_csv


def test_quote_and_sage_exports() -> None:
    record = {
        "quote_reference": "Q-TEST",
        "customer_name": "Customer",
        "item_code": "BOX-TEST",
        "description": "Test box",
        "order_quantity": 10_000,
        "order_pallets": 10,
        "fulfilment_type": "MTC",
        "agreement_term_months": 12,
        "stock_holding_percent": 20,
        "stock_holding_pallets": 2,
        "delivery_pallets_per_calloff": 1,
        "estimated_delivery_count": 10,
        "pallet_holding_charge_per_pallet_per_week": 3,
        "selling_price_per_1000": 750,
        "selling_price_per_item": 0.75,
        "delivery_method": "Haulier",
        "transport_vendor": "Joda",
        "transport_service": "Economy",
        "delivery_postcode": "BD20 0AA",
        "product_group": "Finished goods",
        "manufacturing_site": 101,
        "net_mass_kg": 0.6,
        "length_mm": 574,
        "width_mm": 376,
        "height_mm": 149,
        "board_gsm": 1250,
        "pallet_quantity": 1000,
    }

    pdf = quote_pdf(record)
    csv = sage_stock_import_csv(record).decode("utf-8-sig")

    assert pdf.startswith(b"%PDF")
    assert "Stock item code" in csv
    assert "BOX-TEST" in csv
    assert "AnalysisName\\18" in csv
    assert "MTC" in csv
