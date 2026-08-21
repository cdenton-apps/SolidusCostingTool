from __future__ import annotations

import html
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.calculations import MIN_PALLET_HOLDING_CHARGE


INK = colors.HexColor("#000000")
YELLOW = colors.HexColor("#FDD615")
PALE = colors.HexColor("#F3F5F2")
GREY = colors.HexColor("#D4DEDD")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAND_HEADER_PATH = PROJECT_ROOT / "assets" / "solidus-brand-header.jpg"
TERMS_PATH = PROJECT_ROOT / "assets" / "solidus-terms-and-conditions.pdf"

SAGE_STOCK_COLUMNS = [
    "Stock item code",
    "Stock item name",
    "Product group",
    "Tax code",
    "Stock item description",
    "Manufacturer's name",
    "Manufacturer's part number",
    "Commodity code",
    "Net mass",
    "Stock take days",
    "Allow Sales order",
    "Asset of stock - account number",
    "Asset of stock - cost centre",
    "Asset of stock - department",
    "Revenue - account number",
    "Revenue - cost centre",
    "Revenue - department",
    "Supplier",
    "Supplier lead time",
    "Supplier lead time unit",
    "Supplier minimum quantity",
    "Supplier usual order quantity",
    "Supplier part number",
    "Alternative item",
    "Alternative item name",
    "Barcode",
    *[
        heading
        for index in range(1, 21)
        for heading in (f"AnalysisName\\{index}", f"AnalysisValue\\{index}")
    ],
    "Accrued receipts - account number",
    "Accrued receipts - cost centre",
    "Accrued receipts - department",
    "Issues - account number",
    "Issues - cost centre",
    "Issues - department",
]

SAGE_ANALYSIS_FIELDS = [
    ("Legacy Code", "legacy_code", ""),
    ("Doublestack", "double_stack", "N"),
    ("Pallet Size", "pallet_size", ""),
    ("MRP Type", "mrp_type", "MTO"),
    ("Length", "length_mm", ""),
    ("Width", "width_mm", ""),
    ("Height", "height_mm", ""),
    ("Grade / Gram", "board_gsm", ""),
    ("Boardwidth/Reel Width", "board_width_mm", ""),
    ("Boardlength/Chop", "board_length_mm", ""),
    ("BundleQty / Reel Core ID", "bundle_quantity", ""),
    ("Bundles Per Layer / Bundle Type", "bundles_per_layer", ""),
    ("Layers Per Pallet", "layers_per_pallet", ""),
    ("Pallet Height", "pallet_height_mm", ""),
    ("Product State", "product_state", "FG Box"),
    ("Number Of Colours", "number_of_colours", ""),
    ("FSC", "fsc", ""),
    ("Pallet Qty", "pallet_quantity", ""),
    ("Board Code", "board_code", ""),
    ("Market Segment", "market_segment", ""),
]


def _currency_symbol(currency: Any = "GBP") -> str:
    return "€" if str(currency or "GBP").upper() == "EUR" else "£"


def _money(value: Any, currency: Any = "GBP") -> str:
    try:
        return f"{_currency_symbol(currency)}{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _unit_money(value: Any, currency: Any = "GBP") -> str:
    """Format every per-item price to the agreed five decimal places."""
    try:
        return f"{_currency_symbol(currency)}{float(value):,.5f}"
    except (TypeError, ValueError):
        return "—"


def _board_material(board_code: Any, fallback: Any = "") -> str:
    """Return the material suffix that follows the GSM part of a board code."""
    parts = [part.strip() for part in str(board_code or "").strip("/").split("/")]
    for index, part in enumerate(parts):
        if re.fullmatch(r"\d+(?:\.\d+)?G(?:SM)?", part, flags=re.IGNORECASE):
            material_parts = [value for value in parts[index + 1 :] if value]
            if material_parts:
                return "/".join(material_parts)
    return str(fallback or "").strip()


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


def _uk_datetime(value: Any, *, include_time: bool = False, default: str = "") -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return default
    parsed = parsed.tz_convert("Europe/London")
    return parsed.strftime("%d/%m/%Y %H:%M" if include_time else "%d/%m/%Y")


def _uk_date_after(value: Any, days: int) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return ""
    return (parsed.tz_convert("Europe/London") + pd.Timedelta(days=days)).strftime(
        "%d/%m/%Y"
    )


def _uk_date_after_months(value: Any, months: int) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return ""
    return (
        parsed.tz_convert("Europe/London") + pd.DateOffset(months=months)
    ).strftime("%d/%m/%Y")


def _print_colours(value: Any) -> str:
    """Translate the internal print code into customer-facing wording."""
    text = str(value or "").strip()
    try:
        text = str(int(float(text)))
    except (TypeError, ValueError, OverflowError):
        pass
    digits = "".join(character for character in text if character.isdigit())
    if digits == "901":
        return "CMYK"
    if not digits or digits[0] == "0":
        return "Not specified"
    count = int(digits[0])
    return f"{count} colour" if count == 1 else f"{count} colours"


def _multi_quote_pdf(record: dict[str, Any], *, esign_tags: bool = False) -> bytes:
    raw_items = record.get("quote_items", [])
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_items = []
    items = [dict(item) for item in raw_items if isinstance(item, dict)]
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=10 * mm,
        bottomMargin=17 * mm,
        title=f"Quotation {record.get('quote_reference', '')}",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="MultiTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=18,
            alignment=TA_RIGHT,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MultiMeta",
            parent=styles["BodyText"],
            fontSize=7.5,
            leading=9.2,
            alignment=TA_RIGHT,
            textColor=colors.HexColor("#4A5050"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="MultiSection",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MultiCell",
            parent=styles["BodyText"],
            fontSize=7.2,
            leading=8.6,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MultiCellBold",
            parent=styles["MultiCell"],
            fontName="Helvetica-Bold",
        )
    )
    styles.add(
        ParagraphStyle(
            name="MultiTerms",
            parent=styles["BodyText"],
            fontSize=6.8,
            leading=7.8,
            textColor=colors.HexColor("#303434"),
            spaceAfter=1.5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MultiSignatureHeading",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            alignment=TA_CENTER,
        )
    )

    def cell(value: Any, *, bold: bool = False) -> Paragraph:
        return Paragraph(_display(value), styles["MultiCellBold" if bold else "MultiCell"])

    def section(title: str) -> Table:
        return Table(
            [[Paragraph(title.upper(), styles["MultiSection"])]],
            colWidths=[180 * mm],
            rowHeights=[6 * mm],
            style=[
                ("BACKGROUND", (0, 0), (-1, -1), YELLOW),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ],
        )

    quote_currency = str(record.get("quote_currency", "GBP") or "GBP").upper()
    fulfilment = str(record.get("fulfilment_type", "MTO") or "MTO").upper()
    collected = str(record.get("delivery_method", "Haulier")) == "Customer collection"
    quotation_date = _uk_datetime(record.get("created_at_utc"))
    valid_until = _uk_date_after_months(record.get("created_at_utc"), 3)
    logo = (
        Image(str(BRAND_HEADER_PATH), width=64 * mm, height=20 * mm)
        if BRAND_HEADER_PATH.exists()
        else Paragraph("Solidus", styles["MultiTitle"])
    )
    header = Table(
        [[
            logo,
            [
                Paragraph("CUSTOMER QUOTATION", styles["MultiTitle"]),
                Paragraph(
                    "<b>PRIVATE AND CONFIDENTIAL</b><br/>"
                    f"Reference: <b>{_display(record.get('quote_reference'), 'Draft')}</b>"
                    + (f"<br/>Quotation date: <b>{quotation_date}</b>" if quotation_date else ""),
                    styles["MultiMeta"],
                ),
                Paragraph(
                    "Solidus Packaging Solutions Limited<br/>"
                    "Engine Shed Lane, Skipton, North Yorkshire, BD23 1TX<br/>"
                    "+44 (0)1756 799411"
                    + (f"<br/>Quotation valid until: <b>{valid_until}</b>" if valid_until else ""),
                    styles["MultiMeta"],
                ),
            ],
        ]],
        colWidths=[95 * mm, 85 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ],
    )
    common_rows = [
        [cell("Customer", bold=True), cell(record.get("customer_name")), cell("For the attention of", bold=True), cell(record.get("customer_contact"))],
        [cell("Fulfilment", bold=True), cell("MTC - Make to Contract" if fulfilment == "MTC" else "MTO - Make to Order"), cell("Currency / delivery basis", bold=True), cell(f"{quote_currency} / {'Collected' if collected else 'DAP'}")],
        [cell("Delivery postcode", bold=True), cell("—" if collected else record.get("delivery_postcode")), cell("Item delivery", bold=True), cell(record.get("multi_delivery_mode", "Delivered together"))],
    ]
    if fulfilment == "MTC":
        common_rows.append(
            [
                cell("Agreement term", bold=True),
                cell(f"{_number(record.get('agreement_term_months'), 12):,.0f} months"),
                cell("Minimum call-off", bold=True),
                cell(f"{_number(record.get('delivery_pallets_per_calloff')):,.0f} pallets"),
            ]
        )
    common_table = Table(
        common_rows,
        colWidths=[28 * mm, 62 * mm, 33 * mm, 57 * mm],
        style=[
            ("BACKGROUND", (0, 0), (0, -1), PALE),
            ("BACKGROUND", (2, 0), (2, -1), PALE),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ],
    )

    item_rows: list[list[Any]] = [[
        cell("Item", bold=True),
        cell("Description", bold=True),
        cell("Quantity", bold=True),
        cell("Pallets", bold=True),
        cell("Per 1,000", bold=True),
        cell("Per item", bold=True),
    ]]
    for item in items:
        item_rows.append(
            [
                cell(item.get("item_code")),
                cell(item.get("description")),
                cell(f"{_number(item.get('order_quantity')):,.0f}"),
                cell(f"{_number(item.get('pallet_count')):,.0f}"),
                cell(_money(item.get("selling_price_per_1000"), quote_currency)),
                cell(_unit_money(item.get("selling_price_per_item"), quote_currency)),
            ]
        )
    item_table = Table(
        item_rows,
        repeatRows=1,
        colWidths=[36 * mm, 62 * mm, 19 * mm, 13 * mm, 25 * mm, 25 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ],
    )
    price_rows = [
        [cell("Quoted order value", bold=True), cell(_money(record.get("quoted_value"), quote_currency), bold=True)],
    ]
    if record.get("additional_charge_foc") or _number(record.get("additional_charge_amount")) > 0:
        charge = "FOC" if record.get("additional_charge_foc") else _money(record.get("additional_charge_amount"), quote_currency)
        price_rows.append([cell(record.get("additional_charge_description") or "One-off charge", bold=True), cell(charge, bold=True)])
    price_table = Table(
        price_rows,
        colWidths=[130 * mm, 50 * mm],
        style=[
            ("BACKGROUND", (0, 0), (0, -1), PALE),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ],
    )

    terms = [
        "This quotation supplements the attached Solidus General Terms and Conditions of Sale and Delivery, which form part of this quotation and prevail in the event of any conflict.",
        (
            f"Unless otherwise agreed in writing, prices are in {quote_currency}, exclusive of VAT and are based on customer collection; delivery is not included."
            if collected
            else f"Unless otherwise agreed in writing, prices are in {quote_currency}, exclusive of VAT and based on DAP Incoterms."
        ),
        "All payments must be made within thirty (30) days after the invoice date, unless another payment term has been agreed in writing by Solidus.",
        "Lead time will be confirmed upon acceptance of a valid purchase order and remains subject to change.",
        f"This quotation is valid until {valid_until}. Delivery dates will be confirmed upon receipt and acceptance of a purchase order."
        if valid_until
        else "This quotation is valid for three months from the quotation date.",
    ]
    if fulfilment == "MTC":
        months = _number(record.get("agreement_term_months"), 12)
        holding = max(
            MIN_PALLET_HOLDING_CHARGE,
            _number(record.get("pallet_holding_charge_per_pallet_per_week")),
        )
        terms.extend(
            [
                f"The {months:,.0f}-month MTC term starts on the commencement date confirmed by Solidus in line with current lead times and production planning, not the quotation date. Changes to the call-off profile may change transport pricing.",
                f"Where stock is held beyond the agreed call-off profile or the Customer breaches the agreement, Solidus reserves the right either to charge £{holding:,.2f} per pallet per week or to despatch and invoice any stock held or produced under the agreement.",
            ]
        )
    else:
        terms.append(
            "MTO pricing is based on the delivery arrangement shown above. A changed delivery profile may change transport pricing."
        )
    if not collected and str(record.get("transport_service", "") or "").strip():
        terms.append(
            f"The delivery allowance is based on the {record.get('transport_service')} service. This will ordinarily be the service used."
        )

    internal_route = (
        "sales_director"
        if str(record.get("traffic_light_status", "")).lower() == "red"
        else "sales_representative"
    )
    internal_heading = (
        "Sales Director or delegated individual"
        if internal_route == "sales_director"
        else "Sales Representative"
    )
    internal_role = (
        "Role: Sales Director or delegated individual"
        if internal_route == "sales_director"
        else "Role: Sales Representative"
    )
    internal_signature = "Signed: ______________________________"
    internal_name = "Name: _______________________________"
    internal_date = "Date (DD/MM/YYYY): ___________________"
    customer_signature = "Signed: ______________________________"
    customer_name = "Name: _______________________________"
    customer_date = "Date (DD/MM/YYYY): ___________________"
    if esign_tags:
        internal_signature = '<font color="#FFFFFF">[sig|req|signer1]</font>'
        internal_name = 'Name: <font color="#FFFFFF">[text|req|signer1|Full name]</font>'
        internal_date = 'Date: <font color="#FFFFFF">[date|req|signer1|Signing date]</font>'
        customer_signature = '<font color="#FFFFFF">[sig|req|signer2]</font>'
        customer_name = 'Name: <font color="#FFFFFF">[text|req|signer2|Full name]</font>'
        customer_date = 'Date: <font color="#FFFFFF">[date|req|signer2|Signing date]</font>'

    def signature_card(heading: str, signature: str, name: str, role: str, date: str) -> Table:
        return Table(
            [
                [Paragraph(heading, styles["MultiSignatureHeading"])],
                [Paragraph(signature, styles["MultiCell"])],
                [Paragraph(name, styles["MultiCell"])],
                [Paragraph(role, styles["MultiCell"])],
                [Paragraph(date, styles["MultiCell"])],
            ],
            colWidths=[88 * mm],
            rowHeights=[4 * mm, 8 * mm, 5 * mm, 4.5 * mm, 4.5 * mm] if esign_tags else None,
            style=[
                ("BACKGROUND", (0, 0), (0, 0), YELLOW),
                ("BOX", (0, 0), (-1, -1), 0.45, GREY),
                ("INNERGRID", (0, 1), (-1, -1), 0.25, GREY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
            ],
        )

    signature_table = Table(
        [[
            signature_card(internal_heading, internal_signature, internal_name, internal_role, internal_date),
            "",
            signature_card(
                "Customer",
                customer_signature,
                customer_name,
                f"Role: {_display(record.get('customer_role'), '_______________________________')}",
                customer_date,
            ),
        ]],
        colWidths=[88 * mm, 4 * mm, 88 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ],
    )

    notes = str(record.get("notes", "") or "").strip() or "No additional notes."
    story: list[Any] = [
        header,
        Spacer(1, 1 * mm),
        section("Quote details"),
        common_table,
        Spacer(1, 1 * mm),
        section("Quoted items"),
        item_table,
        price_table,
        Spacer(1, 1 * mm),
        section("Notes"),
        Table([[cell(notes)]], colWidths=[180 * mm], style=[("BOX", (0, 0), (-1, -1), 0.3, GREY), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]),
        Spacer(1, 1 * mm),
        section("Commercial terms"),
        *[Paragraph(f"- {html.escape(term)}", styles["MultiTerms"]) for term in terms],
        Spacer(1, 1 * mm),
        KeepTogether(
            [
                section("Quotation acceptance"),
                Table(
                    [[
                        cell("Quotation", bold=True),
                        cell(record.get("quote_reference") or "Draft"),
                        cell("Customer", bold=True),
                        cell(record.get("customer_name")),
                        cell("Items", bold=True),
                        cell(str(len(items))),
                    ]],
                    colWidths=[23 * mm, 30 * mm, 23 * mm, 65 * mm, 15 * mm, 24 * mm],
                    style=[
                        ("BACKGROUND", (0, 0), (0, 0), PALE),
                        ("BACKGROUND", (2, 0), (2, 0), PALE),
                        ("BACKGROUND", (4, 0), (4, 0), PALE),
                        ("GRID", (0, 0), (-1, -1), 0.3, GREY),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ],
                ),
                Paragraph(
                    f"Acceptance: quotation {_display(record.get('quote_reference'), 'Draft')} and the attached terms and conditions.",
                    styles["MultiTerms"],
                ),
                signature_table,
            ]
        ),
        PageBreak(),
        section("Item technical specifications"),
    ]
    specification_rows: list[list[Any]] = [[
        cell("Item", bold=True),
        cell("Finished size", bold=True),
        cell("Material / GSM", bold=True),
        cell("Board code", bold=True),
        cell("Pallet qty", bold=True),
        cell("Print", bold=True),
    ]]
    for item in items:
        dimensions = [_number(item.get(key)) for key in ("length_mm", "width_mm", "height_mm")]
        size = (
            f"{dimensions[0]:,.0f} × {dimensions[1]:,.0f} × {dimensions[2]:,.0f} mm"
            if all(value > 0 for value in dimensions)
            else "Not specified"
        )
        material = _board_material(item.get("board_code"), item.get("material"))
        gsm = _number(item.get("board_gsm"))
        specification_rows.append(
            [
                cell(item.get("item_code")),
                cell(size),
                cell(" / ".join(value for value in [material, f"{gsm:,.0f} GSM" if gsm else ""] if value)),
                cell(str(item.get("board_code", "") or "").rstrip("/")),
                cell(f"{_number(item.get('pallet_quantity')):,.0f}"),
                cell(_print_colours(item.get("number_of_colours"))),
            ]
        )
    story.append(
        Table(
            specification_rows,
            repeatRows=1,
            colWidths=[38 * mm, 36 * mm, 32 * mm, 34 * mm, 18 * mm, 22 * mm],
            style=[
                ("BACKGROUND", (0, 0), (-1, 0), PALE),
                ("GRID", (0, 0), (-1, -1), 0.3, GREY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ],
        )
    )

    def footer(canvas, _: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(GREY)
        canvas.line(15 * mm, 12 * mm, A4[0] - 15 * mm, 12 * mm)
        canvas.setFillColor(colors.HexColor("#666C6C"))
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return _append_terms(buffer.getvalue())


def quote_pdf(record: dict[str, Any], *, esign_tags: bool = False) -> bytes:
    raw_items = record.get("quote_items", [])
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_items = []
    if isinstance(raw_items, list) and len(raw_items) > 1:
        return _multi_quote_pdf(record, esign_tags=esign_tags)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=10 * mm,
        bottomMargin=17 * mm,
        title=f"Quotation {record.get('quote_reference', '')}",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="QuoteTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=18,
            alignment=TA_RIGHT,
            textColor=INK,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="QuoteMeta",
            parent=styles["BodyText"],
            fontSize=7.5,
            leading=9.2,
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
            fontSize=6.7,
            leading=7.6,
            textColor=colors.HexColor("#303434"),
            spaceAfter=1.5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ApprovalNotice",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9,
            alignment=TA_CENTER,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SignatureLabel",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            alignment=TA_CENTER,
            textColor=INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SignatureMeta",
            parent=styles["BodyText"],
            fontSize=7.5,
            leading=10,
            textColor=colors.HexColor("#303434"),
        )
    )
    def paragraph(value: Any, style_name: str = "CellValue") -> Paragraph:
        return Paragraph(_display(value), styles[style_name])

    def section(title: str) -> Table:
        return Table(
            [[Paragraph(title.upper(), styles["SectionLabel"])]],
            colWidths=[180 * mm],
            rowHeights=[6 * mm],
            style=[
                ("BACKGROUND", (0, 0), (-1, -1), YELLOW),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ],
        )

    def details_table(rows: list[tuple[str, Any]]) -> Table:
        data: list[list[Any]] = []
        spans: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
        full_width_value_rows: list[int] = []
        pending: tuple[str, Any] | None = None
        full_width_labels = {"Description", "Planned call-off"}
        for label, value in rows:
            if label in full_width_labels:
                if pending is not None:
                    data.append(
                        [
                            paragraph(pending[0], "CellLabel"),
                            paragraph(pending[1]),
                            "",
                            "",
                        ]
                    )
                    row_index = len(data) - 1
                    spans.append(("SPAN", (1, row_index), (3, row_index)))
                    full_width_value_rows.append(row_index)
                    pending = None
                data.append(
                    [paragraph(label, "CellLabel"), paragraph(value), "", ""]
                )
                row_index = len(data) - 1
                spans.append(("SPAN", (1, row_index), (3, row_index)))
                full_width_value_rows.append(row_index)
            elif pending is None:
                pending = (label, value)
            else:
                data.append(
                    [
                        paragraph(pending[0], "CellLabel"),
                        paragraph(pending[1]),
                        paragraph(label, "CellLabel"),
                        paragraph(value),
                    ]
                )
                pending = None
        if pending is not None:
            data.append(
                [paragraph(pending[0], "CellLabel"), paragraph(pending[1]), "", ""]
            )
            row_index = len(data) - 1
            spans.append(("SPAN", (1, row_index), (3, row_index)))
            full_width_value_rows.append(row_index)
        table = Table(
            data,
            colWidths=[30 * mm, 60 * mm, 30 * mm, 60 * mm],
            style=[
                ("BACKGROUND", (0, 0), (0, -1), PALE),
                ("BACKGROUND", (2, 0), (2, -1), PALE),
                *[
                    ("BACKGROUND", (1, row), (3, row), colors.white)
                    for row in full_width_value_rows
                ],
                ("GRID", (0, 0), (-1, -1), 0.3, GREY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                *spans,
            ],
        )
        return table

    fulfilment_type = str(record.get("fulfilment_type", "MTO") or "MTO").upper()
    quote_currency = str(record.get("quote_currency", "GBP") or "GBP").upper()
    if quote_currency not in {"GBP", "EUR"}:
        quote_currency = "GBP"
    incoterm = str(record.get("incoterm", "DAP") or "DAP").upper()
    collected = (
        str(record.get("delivery_method", "Haulier") or "Haulier")
        == "Customer collection"
        or incoterm == "EXW"
    )
    delivery_basis = "Collected" if collected else "DAP"
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
        ("Currency / delivery basis", f"{quote_currency} / {delivery_basis}"),
    ]
    quotation_date = _uk_datetime(record.get("created_at_utc"))
    valid_until = _uk_date_after_months(record.get("created_at_utc"), 3)
    commercial_terms: list[str] = [
        "This quotation supplements the attached Solidus General Terms and Conditions of Sale and Delivery, which form part of this quotation and prevail in the event of any conflict.",
        (
            f"Unless otherwise agreed in writing, prices are in {quote_currency}, exclusive of VAT and are based on customer collection; delivery is not included."
            if collected
            else f"Unless otherwise agreed in writing, prices are in {quote_currency}, exclusive of VAT and based on DAP Incoterms."
        ),
        "All payments must be made within thirty (30) days after the invoice date, unless another payment term has been agreed in writing by Solidus in an order confirmation, sales agreement or service level agreement.",
        "Lead time will be confirmed upon acceptance of a valid purchase order and remains subject to change.",
        (
            f"This quotation is valid until {valid_until}. Delivery dates will be confirmed upon receipt and acceptance of a purchase order."
            if valid_until
            else "This quotation is valid for three months from the quotation date. Delivery dates will be confirmed upon receipt and acceptance of a purchase order."
        ),
    ]
    if fulfilment_type == "MTC":
        agreement_months = _number(record.get("agreement_term_months"), 12)
        calloff_pallets = _number(record.get("delivery_pallets_per_calloff"))
        delivery_count = _number(record.get("estimated_delivery_count"), 1)
        holding_charge = max(
            MIN_PALLET_HOLDING_CHARGE,
            _number(record.get("pallet_holding_charge_per_pallet_per_week")),
        )
        calloff_unit = "pallet" if calloff_pallets == 1 else "pallets"
        delivery_unit = "delivery" if delivery_count == 1 else "deliveries"
        order_rows.extend(
            [
                ("Agreement term", f"{agreement_months:,.0f} months"),
                (
                    "Planned call-off",
                    f"Minimum of {calloff_pallets:,.0f} {calloff_unit} per delivery; approximately {delivery_count:,.0f} {delivery_unit}",
                ),
            ]
        )
        commercial_terms.append(
            f"The {agreement_months:,.0f}-month MTC term starts on the commencement "
            "date confirmed by Solidus in line with current lead times and production "
            "planning, not the quotation date. Changes to the call-off profile may "
            "change transport pricing."
        )
        commercial_terms.append(
            "Where stock is held beyond the agreed call-off profile or the Customer "
            "breaches the agreement, Solidus reserves the right either to charge "
            f"£{holding_charge:,.2f} per pallet per week or to despatch and invoice "
            "any stock held or produced under the agreement."
        )
    else:
        commercial_terms.append(
            "MTO pricing assumes the quoted order quantity is released as one delivery event. A changed delivery profile may change transport pricing."
        )

    delivery_method = str(record.get("delivery_method", ""))
    transport_service = str(record.get("transport_service", "") or "").strip()
    if delivery_method == "Haulier" and transport_service:
        commercial_terms.append(
            f"The delivery allowance is based on the {transport_service} service. "
            "This will ordinarily be the service used."
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
    material = _board_material(record.get("board_code"), record.get("material"))
    gsm = _number(record.get("board_gsm"))
    material_grade = " / ".join(
        value for value in [material, f"{gsm:,.0f} GSM" if gsm > 0 else ""] if value
    ) or "Not specified"
    net_mass = _number(record.get("net_mass_kg"))
    net_mass_display = f"{net_mass:,.4f} kg" if net_mass > 0 else "Not specified"
    board_code_display = str(record.get("board_code", "") or "").strip().rstrip("/")
    technical_items = [
        ("Finished size", finished_size),
        ("Material / GSM", material_grade),
        ("Board size", board_size),
        ("Board code", board_code_display),
        ("Pallet quantity", _whole_number(record.get("pallet_quantity"))),
        ("Pallet size", record.get("pallet_size")),
        ("Print colours", _print_colours(record.get("number_of_colours"))),
        ("FSC", record.get("fsc")),
        ("Net mass / item", net_mass_display),
    ]
    if len(technical_items) % 2:
        technical_items.append(("", ""))
    technical_rows: list[list[Paragraph]] = []
    for index in range(0, len(technical_items), 2):
        left_label, left_value = technical_items[index]
        right_label, right_value = technical_items[index + 1]
        technical_rows.append(
            [
                paragraph(left_label, "CellLabel"),
                paragraph(left_value),
                paragraph(right_label, "CellLabel") if right_label else "",
                paragraph(right_value) if right_label else "",
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
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ],
    )

    price_rows = [
            [paragraph("PRICE", "SectionLabel"), ""],
            [paragraph("Per 1,000", "CellLabel"), paragraph(_money(record.get("selling_price_per_1000"), quote_currency), "CardValue")],
            [paragraph("Per item", "CellLabel"), paragraph(_unit_money(record.get("selling_price_per_item"), quote_currency), "CardValue")],
    ]
    additional_description = str(
        record.get("additional_charge_description", "") or ""
    ).strip()
    additional_description = re.sub(
        r"\bstereo\b", "Stereo", additional_description, flags=re.IGNORECASE
    )
    additional_amount = _number(record.get("additional_charge_amount"))
    additional_foc = bool(record.get("additional_charge_foc", False))
    price_card = Table(
        price_rows,
        colWidths=[46 * mm, 44 * mm],
        rowHeights=[6 * mm, 6.5 * mm, 6.5 * mm],
        style=[
            ("SPAN", (0, 0), (1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), YELLOW),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ],
    )
    one_off_card = None
    if additional_description and (additional_foc or additional_amount > 0):
        one_off_card = Table(
            [
                [paragraph("ONE-OFF COSTS", "SectionLabel"), ""],
                [
                    paragraph(additional_description, "CellLabel"),
                    paragraph(
                        "FOC"
                        if additional_foc
                        else _money(additional_amount, quote_currency),
                        "CardValue",
                    ),
                ],
            ],
            colWidths=[46 * mm, 44 * mm],
            rowHeights=[6 * mm, 6.5 * mm],
            style=[
                ("SPAN", (0, 0), (1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), PALE),
                ("BACKGROUND", (1, 1), (1, 1), YELLOW if additional_foc else colors.white),
                ("GRID", (0, 0), (-1, -1), 0.3, GREY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ],
        )
    booking = str(record.get("transport_booking", "Standard") or "Standard")
    profile_pallets = _number(record.get("delivery_pallets_per_calloff"))
    profile_unit = "pallet" if profile_pallets == 1 else "pallets"
    delivery_profile = (
        f"Minimum of {profile_pallets:,.0f} {profile_unit} per delivery"
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
        colWidths=[35 * mm, 55 * mm],
        rowHeights=[6 * mm, 6.5 * mm, 6.5 * mm, 6.5 * mm, 6.5 * mm],
        style=[
            ("SPAN", (0, 0), (1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), YELLOW),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ],
    )

    logo = (
        Image(str(BRAND_HEADER_PATH), width=64 * mm, height=20 * mm)
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
                        "<b>PRIVATE AND CONFIDENTIAL</b><br/>"
                        f"Reference: <b>{_display(record.get('quote_reference'), 'Draft')}</b>"
                        + (
                            f"<br/>Quotation date: <b>{html.escape(quotation_date)}</b>"
                            if quotation_date
                            else ""
                        ),
                        styles["QuoteMeta"],
                    ),
                    Paragraph(
                        "Solidus Packaging Solutions Limited<br/>"
                        "Engine Shed Lane, Skipton, North Yorkshire, BD23 1TX<br/>"
                        "+44 (0)1756 799411"
                        + (
                            f"<br/>Quotation valid until: <b>{html.escape(valid_until)}</b>"
                            if valid_until
                            else ""
                        ),
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

    left_price_cards: list[list[Any]] = [[price_card]]
    if one_off_card is not None:
        left_price_cards.extend([[Spacer(1, 2 * mm)], [one_off_card]])
    price_stack = Table(
        left_price_cards,
        colWidths=[90 * mm],
        style=[
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ],
    )

    story = [header, Spacer(1, 1 * mm), section("Quote details"), details_table(order_rows)]
    story.extend(
        [
            Spacer(1, 1 * mm),
            section("Technical specification"),
            technical_table,
            Spacer(1, 1 * mm),
            Table(
                [[price_stack, delivery_card]],
                colWidths=[90 * mm, 90 * mm],
                style=[
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ],
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
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ],
    )
    approval_notice = Table(
        [
            [
                Paragraph(
                    "THIS QUOTATION IS GENERATED FROM THE COSTING TOOL AND REMAINS "
                    "SUBJECT TO FINAL COMMERCIAL APPROVAL.",
                    styles["ApprovalNotice"],
                )
            ]
        ],
        colWidths=[180 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), YELLOW),
            ("BOX", (0, 0), (-1, -1), 0.7, INK),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ],
    )
    quote_reference = _display(record.get("quote_reference"), "Draft")
    internal_route = str(record.get("esign_internal_signer_role", "") or "").strip()
    if internal_route not in {"sales_representative", "sales_director"}:
        internal_route = (
            "sales_director"
            if str(record.get("traffic_light_status", "") or "").lower() == "red"
            else "sales_representative"
        )
    internal_heading = (
        "Sales Director or delegated individual"
        if internal_route == "sales_director"
        else "Sales Representative"
    )
    internal_role_line = (
        "Role: Sales Director or delegated individual"
        if internal_route == "sales_director"
        else "Role: Sales Representative"
    )
    customer_role = str(record.get("customer_role", "") or "").strip()
    customer_signature = "Signed: ______________________________"
    customer_name_line = "Name: _______________________________"
    customer_date = "Date (DD/MM/YYYY): ___________________"
    internal_signature = "Signed: ______________________________"
    internal_name_line = "Name: _______________________________"
    internal_date = "Date (DD/MM/YYYY): ___________________"
    if esign_tags:
        customer_signature = '<font color="#FFFFFF">[sig|req|signer2]</font>'
        customer_name_line = (
            'Name: <font color="#FFFFFF">[text|req|signer2|Full name]</font>'
        )
        customer_date = (
            'Date: <font color="#FFFFFF">[date|req|signer2|Signing date]</font>'
        )
        internal_signature = '<font color="#FFFFFF">[sig|req|signer1]</font>'
        internal_name_line = (
            'Name: <font color="#FFFFFF">[text|req|signer1|Full name]</font>'
        )
        internal_date = (
            'Date: <font color="#FFFFFF">[date|req|signer1|Signing date]</font>'
        )
    if fulfilment_type == "MTC":
        signature_intro = (
            f"Acceptance: quotation {quote_reference}, its MTC term, call-off profile "
            "and the attached terms and conditions."
            if esign_tags
            else f"Acceptance: quotation {quote_reference}, its MTC term and call-off "
            "profile, subject to final commercial approval and the attached terms and conditions."
        )
    else:
        signature_intro = f"Acceptance: quotation {quote_reference} and the attached terms."
    approval_summary = Table(
        [[Paragraph(html.escape(signature_intro), styles["Terms"])]],
        colWidths=[180 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), PALE),
            ("BOX", (0, 0), (-1, -1), 0.35, GREY),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ],
    )

    card_row_heights = (
        [3.5 * mm, 8 * mm, 5 * mm, 4.25 * mm, 4.25 * mm]
        if esign_tags
        else [3 * mm, 5.5 * mm, 3.5 * mm, 3 * mm, 3 * mm]
    )

    def signature_card(
        heading: str,
        signature: str,
        name_line: str,
        role_line: str,
        date_line: str,
    ) -> Table:
        return Table(
            [
                [Paragraph(heading, styles["SignatureLabel"])],
                [Paragraph(signature, styles["SignatureMeta"])],
                [Paragraph(name_line, styles["SignatureMeta"])],
                [Paragraph(role_line, styles["SignatureMeta"])],
                [Paragraph(date_line, styles["SignatureMeta"])],
            ],
            colWidths=[88 * mm],
            rowHeights=card_row_heights,
            style=[
                ("BACKGROUND", (0, 0), (0, 0), YELLOW),
                ("BOX", (0, 0), (-1, -1), 0.45, GREY),
                ("INNERGRID", (0, 1), (-1, -1), 0.25, GREY),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
            ],
        )

    internal_card = signature_card(
        internal_heading,
        internal_signature,
        internal_name_line,
        internal_role_line,
        internal_date,
    )
    customer_card = signature_card(
        "Customer",
        customer_signature,
        customer_name_line,
        f"Role: {html.escape(customer_role)}"
        if customer_role
        else "Role: ________________________________",
        customer_date,
    )
    signature_table = Table(
        [[internal_card, "", customer_card]],
        colWidths=[88 * mm, 4 * mm, 88 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ],
    )
    story.extend(
        [
            Spacer(1, 2 * mm),
            KeepTogether([section("Notes"), notes_table]),
        ]
    )
    commercial_heading = [section("Commercial terms")]
    if not esign_tags:
        commercial_heading.extend([Spacer(1, 2 * mm), approval_notice])
    story.extend(
        [
            Spacer(1, 0.8 * mm),
            KeepTogether(commercial_heading),
            Spacer(1, 0.5 * mm),
            *[
                Paragraph(f"- {html.escape(term)}", styles["Terms"])
                for term in commercial_terms
            ],
            Spacer(1, 0.8 * mm),
            KeepTogether(
                [
                    approval_summary,
                    Spacer(1, 0.5 * mm),
                    signature_table,
                ]
            ),
        ]
    )

    def draw_footer(canvas, _: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(GREY)
        canvas.setLineWidth(0.4)
        canvas.line(15 * mm, 12 * mm, A4[0] - 15 * mm, 12 * mm)
        canvas.setFillColor(colors.HexColor("#666C6C"))
        canvas.setFont("Helvetica", 7)
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
        title="Costing history",
    )
    styles = getSampleStyleSheet()
    columns = [
        "created_at_utc",
        "created_by_username",
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
    heading_labels = {
        "created_by_username": "Username",
        "created_by_name": "Name",
    }
    headings = [
        heading_labels.get(column, column.replace("_", " ").title())
        for column in available
    ]
    rows = [headings]
    for _, row in frame[available].iterrows():
        rows.append(
            [
                _uk_datetime(row[column], include_time=True)
                if column == "created_at_utc"
                else str(row[column])[:38]
                for column in available
            ]
        )
    story = [
        Paragraph("Solidus costing history", styles["Title"]),
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
    """Create one row in the exact supplied Sage stock export/import layout."""
    site = str(record.get("manufacturing_site", "") or "").strip()
    site_accounts = {
        "101": {
            "Asset of stock - account number": "10260361",
            "Asset of stock - cost centre": "201",
            "Asset of stock - department": "101",
            "Revenue - account number": "10210065",
            "Revenue - cost centre": "301",
            "Revenue - department": "101",
        },
        "102": {
            "Asset of stock - account number": "10260361",
            "Asset of stock - cost centre": "201",
            "Asset of stock - department": "101",
            "Revenue - account number": "10210065",
            "Revenue - cost centre": "301",
            "Revenue - department": "101",
        },
        "103": {
            "Asset of stock - account number": "10260441",
            "Asset of stock - cost centre": "201",
            "Asset of stock - department": "103",
            "Revenue - account number": "10210055",
            "Revenue - cost centre": "301",
            "Revenue - department": "103",
        },
    }
    row: dict[str, Any] = {column: "" for column in SAGE_STOCK_COLUMNS}
    row.update({
        "Stock item code": record.get("item_code", ""),
        "Stock item name": record.get("item_name") or record.get("description", ""),
        "Product group": record.get("product_group", ""),
        "Tax code": 1,
        "Stock item description": record.get("description", ""),
        "Manufacturer's name": site,
        "Net mass": record.get("net_mass_kg", ""),
        "Stock take days": 0,
        "Allow Sales order": 1,
        "Supplier lead time": 0,
        "Supplier lead time unit": 0,
        "Supplier minimum quantity": "0.00000",
        "Supplier usual order quantity": "0.00000",
        "Accrued receipts - account number": "10020003",
        "Accrued receipts - department": "101",
        "Issues - account number": "10260031",
        "Issues - cost centre": "201",
        "Issues - department": "101",
        **site_accounts.get(site, {}),
    })
    for index, (name, field, default) in enumerate(SAGE_ANALYSIS_FIELDS, start=1):
        value = record.get(field)
        if field == "mrp_type" and not str(value or "").strip():
            value = record.get("fulfilment_type", default)
        elif value is None:
            value = default
        row[f"AnalysisName\\{index}"] = name
        row[f"AnalysisValue\\{index}"] = value
    frame = pd.DataFrame([row], columns=SAGE_STOCK_COLUMNS)
    return frame.to_csv(index=False).encode("utf-8-sig")
