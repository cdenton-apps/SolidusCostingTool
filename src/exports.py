from __future__ import annotations

import html
from io import BytesIO
from typing import Any

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


INK = colors.HexColor("#000000")
YELLOW = colors.HexColor("#FDD615")
PALE = colors.HexColor("#F3F5F2")
GREY = colors.HexColor("#D4DEDD")


def _money(value: Any) -> str:
    try:
        return f"£{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def quote_pdf(record: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Quotation {record.get('quote_reference', '')}",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Right", parent=styles["BodyText"], alignment=TA_RIGHT))

    fulfilment_type = str(record.get("fulfilment_type", "MTO") or "MTO").upper()
    fulfilment_label = (
        "MTC - Make to Contract" if fulfilment_type == "MTC" else "MTO - Make to Order"
    )
    order_rows = [
        ["Quote reference", html.escape(str(record.get("quote_reference", "Draft")))],
        ["Customer", html.escape(str(record.get("customer_name", "")))],
        ["For the attention of", html.escape(str(record.get("customer_contact", "")))],
        ["Item", html.escape(str(record.get("item_code", "")))],
        ["Description", html.escape(str(record.get("description", "")))],
        ["Fulfilment", fulfilment_label],
        ["Order / agreement quantity", f"{_number(record.get('order_quantity')):,.0f} units"],
        ["Equivalent pallets", f"{_number(record.get('order_pallets')):,.0f}"],
    ]
    commercial_terms: list[str] = []
    if fulfilment_type == "MTC":
        agreement_months = _number(record.get("agreement_term_months"), 12)
        holding_percent = _number(record.get("stock_holding_percent"))
        holding_pallets = _number(record.get("stock_holding_pallets"))
        calloff_pallets = _number(record.get("delivery_pallets_per_calloff"))
        delivery_count = _number(record.get("estimated_delivery_count"), 1)
        holding_charge = _number(
            record.get("pallet_holding_charge_per_pallet_per_week")
        )
        calloff_unit = "pallet" if calloff_pallets == 1 else "pallets"
        delivery_unit = "delivery" if delivery_count == 1 else "deliveries"
        order_rows.extend(
            [
                ["Agreement term", f"{agreement_months:,.0f} months"],
                [
                    "Stock holding target",
                    f"{holding_percent:,.1f}% (approximately {holding_pallets:,.0f} pallets)",
                ],
                [
                    "Planned call-off",
                    f"Up to {calloff_pallets:,.0f} {calloff_unit} per delivery; approximately {delivery_count:,.0f} {delivery_unit}",
                ],
            ]
        )
        commercial_terms.append(
            f"This quotation assumes a {agreement_months:,.0f}-month MTC agreement and the stated call-off profile. Changes to delivery frequency or pallet quantities may change transport pricing."
        )
        commercial_terms.append(
            f"The planned finished-goods stock holding is {holding_percent:,.1f}% of the agreement volume (approximately {holding_pallets:,.0f} pallets)."
        )
        if holding_charge > 0:
            commercial_terms.append(
                f"Pallets held beyond the agreed stock and call-off profile may be charged at £{holding_charge:,.2f} per pallet per week."
            )
        else:
            commercial_terms.append(
                "Pallets held beyond the agreed stock and call-off profile may attract a holding charge; the rate will be confirmed in the final contract."
            )
    else:
        commercial_terms.append(
            "MTO pricing assumes the quoted order quantity is released as one delivery event. A changed delivery profile may change transport pricing."
        )

    story = [
        Paragraph("Solidus", styles["Title"]),
        Paragraph("Your circular packaging partner", styles["Heading3"]),
        Spacer(1, 2 * mm),
        Paragraph("COSTING QUOTATION", styles["Heading1"]),
        Spacer(1, 5 * mm),
        Table(
            order_rows,
            colWidths=[48 * mm, 105 * mm],
            style=[
                ("BACKGROUND", (0, 0), (0, -1), PALE),
                ("TEXTCOLOR", (0, 0), (0, -1), INK),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.35, GREY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 7),
            ],
        ),
        Spacer(1, 8 * mm),
        Table(
            [
                ["Selling price per 1,000", _money(record.get("selling_price_per_1000"))],
                ["Selling price per item", _money(record.get("selling_price_per_item"))],
                ["Delivery", html.escape(str(record.get("delivery_method", "")))],
                ["Haulier", html.escape(str(record.get("transport_vendor", "")))],
                ["Service", html.escape(str(record.get("transport_service", "")))],
                ["Delivery postcode", html.escape(str(record.get("delivery_postcode", "")))],
            ],
            colWidths=[95 * mm, 58 * mm],
            style=[
                ("BACKGROUND", (0, 0), (-1, 0), YELLOW),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.35, GREY),
                ("PADDING", (0, 0), (-1, -1), 8),
            ],
        ),
        Spacer(1, 8 * mm),
        Paragraph("Notes", styles["Heading2"]),
        Paragraph(html.escape(str(record.get("notes", "No additional notes."))), styles["BodyText"]),
        Spacer(1, 6 * mm),
        Paragraph("Commercial terms", styles["Heading2"]),
        *[
            Paragraph(f"- {html.escape(term)}", styles["BodyText"])
            for term in commercial_terms
        ],
        Spacer(1, 12 * mm),
        Paragraph(
            "This quotation is generated from the costing tool and remains subject to final commercial approval.",
            styles["Italic"],
        ),
    ]
    document.build(story)
    return buffer.getvalue()


def history_pdf(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="Costing audit history",
    )
    styles = getSampleStyleSheet()
    columns = [
        "created_at_utc",
        "created_by_name",
        "item_code",
        "revision",
        "customer_name",
        "pricing_base_per_1000",
        "selling_price_per_1000",
        "spread_percent",
    ]
    available = [column for column in columns if column in frame.columns]
    headings = [column.replace("_", " ").title() for column in available]
    rows = [headings]
    for _, row in frame[available].iterrows():
        rows.append([str(row[column])[:38] for column in available])
    story = [
        Paragraph("Solidus costing audit history", styles["Title"]),
        Spacer(1, 4 * mm),
    ]
    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), YELLOW),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)
    document.build(story)
    return buffer.getvalue()


def sage_stock_import_csv(record: dict[str, Any]) -> bytes:
    """Create an indicative Sage row using headings present in the supplied item feed."""
    analysis = [
        ("Legacy Code", record.get("legacy_code", "")),
        ("Doublestack", record.get("double_stack", "N")),
        ("Pallet Size", record.get("pallet_size", "")),
        ("MRP Type", record.get("fulfilment_type") or record.get("mrp_type", "MTO")),
        ("Length", record.get("length_mm", "")),
        ("Width", record.get("width_mm", "")),
        ("Height", record.get("height_mm", "")),
        ("Grade / Gram", record.get("board_gsm", "")),
        ("Boardwidth/Reel Width", record.get("board_width_mm", "")),
        ("Boardlength/Chop", record.get("board_length_mm", "")),
        ("BundleQty / Reel Core ID", record.get("bundle_quantity", "")),
        ("Bundles Per Layer / Bundle Type", record.get("bundles_per_layer", "")),
        ("Layers Per Pallet", record.get("layers_per_pallet", "")),
        ("Pallet Height", record.get("pallet_height_mm", "")),
        ("Product State", record.get("product_state", "FG Box")),
        ("Number Of Colours", record.get("number_of_colours", "")),
        ("FSC", record.get("fsc", "")),
        ("Pallet Qty", record.get("pallet_quantity", "")),
        ("Board Code", record.get("board_code", "")),
        ("Market Segment", record.get("market_segment", "")),
    ]
    row: dict[str, Any] = {
        "Stock item code": record.get("item_code", ""),
        "Stock item name": record.get("item_name") or record.get("description", ""),
        "Product group": record.get("product_group", ""),
        "Tax code": 1,
        "Stock item description": record.get("description", ""),
        "Manufacturer's name": record.get("manufacturing_site", ""),
        "Net mass": record.get("net_mass_kg", ""),
        "Allow Sales order": 1,
    }
    for index, (name, value) in enumerate(analysis, start=1):
        row[f"AnalysisName\\{index}"] = name
        row[f"AnalysisValue\\{index}"] = value
    frame = pd.DataFrame(
        [row]
    )
    return frame.to_csv(index=False).encode("utf-8-sig")
