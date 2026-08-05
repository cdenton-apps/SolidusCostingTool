from __future__ import annotations

import html
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
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
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAND_HEADER_PATH = PROJECT_ROOT / "assets" / "solidus-brand-header.jpg"
TERMS_PATH = PROJECT_ROOT / "assets" / "solidus-terms-and-conditions.pdf"


def _money(value: Any) -> str:
    try:
        return f"£{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _unit_money(value: Any) -> str:
    """Format unit prices with all useful sub-penny precision."""
    try:
        rendered = f"{float(value):,.7f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"
    if "." not in rendered:
        rendered += ".00"
    elif len(rendered.rsplit(".", 1)[1]) < 2:
        rendered += "0"
    return f"£{rendered}"


def _append_terms(quotation: bytes) -> bytes:
    if not TERMS_PATH.exists():
        return quotation
    writer = PdfWriter()
    for page in PdfReader(BytesIO(quotation)).pages:
        writer.add_page(page)
    for page in PdfReader(str(TERMS_PATH)).pages:
        writer.add_page(page)
    combined = BytesIO()
    writer.write(combined)
    return combined.getvalue()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _display(value: Any, default: str = "Not specified") -> str:
    text = str(value or "").strip()
    return html.escape(text or default)


def _whole_number(value: Any, suffix: str = "") -> str:
    number = _number(value)
    return f"{number:,.0f}{suffix}" if number > 0 else "Not specified"


def _delivery_basis(method: Any) -> str:
    return {
        "Haulier": "Delivered",
        "Customer collection": "Ex-works / customer collection",
        "Included elsewhere": "Delivery included elsewhere",
    }.get(str(method or ""), str(method or "Not specified"))


def quote_pdf(record: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=17 * mm,
        title=f"Quotation {record.get('quote_reference', '')}",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="QuoteTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=20,
            alignment=TA_RIGHT,
            textColor=INK,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="QuoteMeta",
            parent=styles["BodyText"],
            fontSize=9,
            leading=12,
            alignment=TA_RIGHT,
            textColor=colors.HexColor("#4A5050"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionLabel",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CellLabel",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#3F4545"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="CellValue",
            parent=styles["BodyText"],
            fontSize=8.5,
            leading=10.5,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CardValue",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            alignment=TA_RIGHT,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Terms",
            parent=styles["BodyText"],
            fontSize=7.8,
            leading=9.6,
            textColor=colors.HexColor("#303434"),
            spaceAfter=2,
        )
    )
    def paragraph(value: Any, style_name: str = "CellValue") -> Paragraph:
        return Paragraph(_display(value), styles[style_name])

    def section(title: str) -> Table:
        return Table(
            [[Paragraph(title.upper(), styles["SectionLabel"])]],
            colWidths=[180 * mm],
            rowHeights=[7 * mm],
            style=[
                ("BACKGROUND", (0, 0), (-1, -1), YELLOW),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ],
        )

    def details_table(rows: list[tuple[str, Any]]) -> Table:
        data = [
            [paragraph(label, "CellLabel"), paragraph(value)] for label, value in rows
        ]
        return Table(
            data,
            colWidths=[45 * mm, 135 * mm],
            style=[
                ("BACKGROUND", (0, 0), (0, -1), PALE),
                ("GRID", (0, 0), (-1, -1), 0.3, GREY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ],
        )

    fulfilment_type = str(record.get("fulfilment_type", "MTO") or "MTO").upper()
    fulfilment_label = (
        "MTC - Make to Contract" if fulfilment_type == "MTC" else "MTO - Make to Order"
    )
    order_rows: list[tuple[str, Any]] = [
        ("Customer", record.get("customer_name")),
        ("For the attention of", record.get("customer_contact")),
        ("Item code", record.get("item_code")),
        ("Description", record.get("description")),
        ("Fulfilment", fulfilment_label),
        ("Order / agreement quantity", f"{_number(record.get('order_quantity')):,.0f} units"),
        ("Equivalent pallets", f"{_number(record.get('order_pallets')):,.0f}"),
    ]
    commercial_terms: list[str] = [
        "This quotation is subject to the attached Solidus General Terms and Conditions of Sale and Delivery, which form part of this quotation."
    ]
    if fulfilment_type == "MTC":
        agreement_months = _number(record.get("agreement_term_months"), 12)
        calloff_pallets = _number(record.get("delivery_pallets_per_calloff"))
        delivery_count = _number(record.get("estimated_delivery_count"), 1)
        holding_charge = _number(
            record.get("pallet_holding_charge_per_pallet_per_week")
        )
        calloff_unit = "pallet" if calloff_pallets == 1 else "pallets"
        delivery_unit = "delivery" if delivery_count == 1 else "deliveries"
        order_rows.extend(
            [
                ("Agreement term", f"{agreement_months:,.0f} months"),
                (
                    "Planned call-off",
                    f"Up to {calloff_pallets:,.0f} {calloff_unit} per delivery; approximately {delivery_count:,.0f} {delivery_unit}",
                ),
            ]
        )
        commercial_terms.append(
            f"This quotation assumes a {agreement_months:,.0f}-month MTC agreement and the stated call-off profile. Changes to delivery frequency or pallet quantities may change transport pricing."
        )
        if holding_charge > 0:
            commercial_terms.append(
                f"Pallet stock held beyond the agreed call-off profile may be charged at £{holding_charge:,.2f} per pallet per week."
            )
        else:
            commercial_terms.append(
                "Pallet stock held beyond the agreed call-off profile may attract a holding charge; the rate will be confirmed in the final contract."
            )
    else:
        commercial_terms.append(
            "MTO pricing assumes the quoted order quantity is released as one delivery event. A changed delivery profile may change transport pricing."
        )

    delivery_method = str(record.get("delivery_method", ""))
    transport_vendor = str(record.get("transport_vendor", "") or "").strip()
    transport_service = str(record.get("transport_service", "") or "").strip()
    if delivery_method == "Haulier" and transport_vendor not in {"", "Manual override"}:
        ordinary_service = " ".join(
            part for part in [transport_vendor, transport_service] if part
        )
        commercial_terms.append(
            f"The delivery allowance is based on {ordinary_service}. This will ordinarily be the service used."
        )

    dimensions = [_number(record.get(key)) for key in ("length_mm", "width_mm", "height_mm")]
    finished_size = (
        f"{dimensions[0]:,.0f} x {dimensions[1]:,.0f} x {dimensions[2]:,.0f} mm"
        if all(value > 0 for value in dimensions)
        else "Not specified"
    )
    board_dimensions = [
        _number(record.get("board_width_mm")),
        _number(record.get("board_length_mm")),
    ]
    board_size = (
        f"{board_dimensions[0]:,.0f} x {board_dimensions[1]:,.0f} mm"
        if all(value > 0 for value in board_dimensions)
        else "Not specified"
    )
    material = str(record.get("material", "") or "").strip()
    gsm = _number(record.get("board_gsm"))
    material_grade = " / ".join(
        value for value in [material, f"{gsm:,.0f} GSM" if gsm > 0 else ""] if value
    ) or "Not specified"
    net_mass = _number(record.get("net_mass_kg"))
    net_mass_display = f"{net_mass:,.4f} kg" if net_mass > 0 else "Not specified"
    technical_items = [
        ("Finished size", finished_size),
        ("Material / GSM", material_grade),
        ("Board size", board_size),
        ("Board code", record.get("board_code")),
        ("Pallet quantity", _whole_number(record.get("pallet_quantity"))),
        ("Pallet size", record.get("pallet_size")),
        ("Print colours", _whole_number(record.get("number_of_colours"))),
        ("FSC", record.get("fsc")),
        ("Net mass / item", net_mass_display),
        ("Product group", record.get("product_group")),
    ]
    technical_rows: list[list[Paragraph]] = []
    for index in range(0, len(technical_items), 2):
        left_label, left_value = technical_items[index]
        right_label, right_value = technical_items[index + 1]
        technical_rows.append(
            [
                paragraph(left_label, "CellLabel"),
                paragraph(left_value),
                paragraph(right_label, "CellLabel"),
                paragraph(right_value),
            ]
        )
    technical_table = Table(
        technical_rows,
        colWidths=[31 * mm, 59 * mm, 31 * mm, 59 * mm],
        style=[
            ("BACKGROUND", (0, 0), (0, -1), PALE),
            ("BACKGROUND", (2, 0), (2, -1), PALE),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ],
    )

    price_card = Table(
        [
            [paragraph("PRICE", "SectionLabel"), ""],
            [paragraph("Per 1,000", "CellLabel"), paragraph(_money(record.get("selling_price_per_1000")), "CardValue")],
            [paragraph("Per item", "CellLabel"), paragraph(_unit_money(record.get("selling_price_per_item")), "CardValue")],
        ],
        colWidths=[45 * mm, 42 * mm],
        style=[
            ("SPAN", (0, 0), (1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), YELLOW),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ],
    )
    booking = str(record.get("transport_booking", "Standard") or "Standard")
    profile_pallets = _number(record.get("delivery_pallets_per_calloff"))
    profile_unit = "pallet" if profile_pallets == 1 else "pallets"
    delivery_profile = (
        f"Up to {profile_pallets:,.0f} {profile_unit} per call-off"
        if fulfilment_type == "MTC"
        else "One delivery event"
    )
    delivery_card = Table(
        [
            [paragraph("DELIVERY", "SectionLabel"), ""],
            [paragraph("Supply basis", "CellLabel"), paragraph(_delivery_basis(delivery_method))],
            [paragraph("Postcode", "CellLabel"), paragraph(record.get("delivery_postcode"))],
            [paragraph("Booking", "CellLabel"), paragraph(booking)],
            [paragraph("Profile", "CellLabel"), paragraph(delivery_profile)],
        ],
        colWidths=[34 * mm, 53 * mm],
        style=[
            ("SPAN", (0, 0), (1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ],
    )

    logo = (
        Image(str(BRAND_HEADER_PATH), width=88 * mm, height=27.5 * mm)
        if BRAND_HEADER_PATH.exists()
        else Paragraph("Solidus", styles["QuoteTitle"])
    )
    header = Table(
        [
            [
                logo,
                [
                    Paragraph("CUSTOMER QUOTATION", styles["QuoteTitle"]),
                    Paragraph(
                        f"Reference<br/><b>{_display(record.get('quote_reference'), 'Draft')}</b>",
                        styles["QuoteMeta"],
                    ),
                ],
            ]
        ],
        colWidths=[95 * mm, 85 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ],
    )

    story = [header, Spacer(1, 4 * mm), section("Quote details"), details_table(order_rows)]
    story.extend(
        [
            Spacer(1, 4 * mm),
            section("Technical specification"),
            technical_table,
            Spacer(1, 4 * mm),
            Table(
                [[price_card, "", delivery_card]],
                colWidths=[87 * mm, 6 * mm, 87 * mm],
                style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
            ),
        ]
    )
    notes = str(record.get("notes", "") or "").strip() or "No additional notes."
    notes_table = Table(
        [[Paragraph(html.escape(notes), styles["CellValue"])]],
        colWidths=[180 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), PALE),
            ("BOX", (0, 0), (-1, -1), 0.3, GREY),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ],
    )
    story.extend(
        [
            Spacer(1, 4 * mm),
            KeepTogether([section("Notes"), notes_table]),
        ]
    )
    story.extend(
        [
            Spacer(1, 4 * mm),
            section("Commercial terms"),
            Spacer(1, 2 * mm),
            *[
                Paragraph(f"- {html.escape(term)}", styles["Terms"])
                for term in commercial_terms
            ],
        ]
    )

    def draw_footer(canvas, _: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(GREY)
        canvas.setLineWidth(0.4)
        canvas.line(15 * mm, 12 * mm, A4[0] - 15 * mm, 12 * mm)
        canvas.setFillColor(colors.HexColor("#666C6C"))
        canvas.setFont("Helvetica", 7)
        canvas.drawString(
            15 * mm,
            8 * mm,
            "Solidus | Customer quotation | Subject to final commercial approval",
        )
        canvas.drawRightString(
            A4[0] - 15 * mm, 8 * mm, f"Page {canvas.getPageNumber()}"
        )
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return _append_terms(buffer.getvalue())


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
        "spread_per_machine_hour",
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
