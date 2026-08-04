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


NAVY = colors.HexColor("#16324F")
TEAL = colors.HexColor("#1F7A6D")
PALE = colors.HexColor("#EAF4F1")


def _money(value: Any) -> str:
    try:
        return f"£{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


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

    story = [
        Paragraph("COSTING QUOTATION", styles["Title"]),
        Spacer(1, 5 * mm),
        Table(
            [
                ["Quote reference", html.escape(str(record.get("quote_reference", "Draft")))],
                ["Customer", html.escape(str(record.get("customer_name", "")))],
                ["For the attention of", html.escape(str(record.get("customer_contact", "")))],
                ["Item", html.escape(str(record.get("item_code", "")))],
                ["Description", html.escape(str(record.get("description", "")))],
                ["Order quantity", f"{float(record.get('order_quantity', 0)):,.0f}"],
            ],
            colWidths=[48 * mm, 105 * mm],
            style=[
                ("BACKGROUND", (0, 0), (0, -1), PALE),
                ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C5C1")),
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
                ["Delivery postcode", html.escape(str(record.get("delivery_postcode", "")))],
            ],
            colWidths=[95 * mm, 58 * mm],
            style=[
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C5C1")),
                ("PADDING", (0, 0), (-1, -1), 8),
            ],
        ),
        Spacer(1, 8 * mm),
        Paragraph("Notes", styles["Heading2"]),
        Paragraph(html.escape(str(record.get("notes", "No additional notes."))), styles["BodyText"]),
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
        "total_cost_per_1000",
        "selling_price_per_1000",
        "preferred_margin_percent",
    ]
    available = [column for column in columns if column in frame.columns]
    headings = [column.replace("_", " ").title() for column in available]
    rows = [headings]
    for _, row in frame[available].iterrows():
        rows.append([str(row[column])[:38] for column in available])
    story = [Paragraph("Costing audit history", styles["Title"]), Spacer(1, 4 * mm)]
    table = Table(rows, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
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
    """Create an indicative Sage import row; headings must be mapped before production."""
    frame = pd.DataFrame(
        [
            {
                "StockItemCode": record.get("item_code", ""),
                "Name": record.get("description", ""),
                "ProductGroup": record.get("product_group", ""),
                "StockUnit": "Each",
                "WeightKg": round(float(record.get("net_weight_kg_per_1000", 0)) / 1_000, 6),
                "PalletQuantity": record.get("pallet_quantity", ""),
                "SellingPricePer1000": record.get("selling_price_per_1000", ""),
            }
        ]
    )
    return frame.to_csv(index=False).encode("utf-8-sig")

