"""Build receipt lines from raw Odoo POS order JSON.

Odoo POS frontend sends **only** raw order data (no pre-rendered lines).
All receipt layout — company info, customer, products, totals, payments,
QR codes, footers — is built here in Python, then fed to
``DeviceManager._build_escpos_bytes()`` for ESC/POS byte generation.

Supports:
- Standard POS receipts (with company, customer, products, totals, payments)
- Kitchen tickets / preparation display tickets
- ``receipt_language`` field for multi-language label support
- Graceful fallback for missing fields
"""

from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlencode

_logger = logging.getLogger(__name__)

SEPARATOR = "-" * 48


# ── i18n labels ───────────────────────────────────────────────────────
# These can be overridden based on receipt_language from order config.

_LABELS: dict[str, dict[str, str]] = {
    "es_ES": {
        "TABLE": "MESA",
        "ORDER": "PEDIDO",
        "SUBTOTAL": "Subtotal",
        "DISCOUNT": "Descuento",
        "TAX": "IGIC 7%",
        "TOTAL": "TOTAL",
        "PAID": "Pagado",
        "DUE": "Pendiente",
        "CHANGE": "Cambio",
        "CODE": "Código",
        "NOTE": "Nota",
        "QTY": "Ud.",
        "PRODUCT": "Producto",
        "AMOUNT": "Importe",
    },
    "en_US": {
        "TABLE": "TABLE",
        "ORDER": "ORDER",
        "SUBTOTAL": "Subtotal",
        "DISCOUNT": "Discount",
        "TAX": "Tax",
        "TOTAL": "TOTAL",
        "PAID": "Paid",
        "DUE": "Due",
        "CHANGE": "Change",
        "CODE": "Code",
        "NOTE": "Note",
        "QTY": "Qty",
        "PRODUCT": "Product",
        "AMOUNT": "Amount",
    },
    "zh_CN": {
        "TABLE": "桌号",
        "ORDER": "订单",
        "DISCOUNT": "折扣",
        "TAX": "税额",
        "TOTAL": "合计",
        "PAID": "已付",
        "DUE": "欠款",
        "CHANGE": "找零",
        "CODE": "编码",
        "NOTE": "备注",
        "QTY": "数量",
        "PRODUCT": "商品",
        "AMOUNT": "金额",
    },
}

_FALLBACK_LANG = "en_US"


def _labels(lang: str) -> dict[str, str]:
    return _LABELS.get(lang, _LABELS[_FALLBACK_LANG])


def _resolve_lang(order: dict[str, Any]) -> str:
    """Resolve receipt language from order config or country."""
    config = order.get("config")
    if isinstance(config, dict):
        lang = _text(config.get("receipt_language"))
        if lang and lang in _LABELS:
            return lang
    company = order.get("company", {})
    if isinstance(company, dict):
        country = _text(company.get("country_id") or "")
        if country and "España" in country or "Spain" in country or "Estados Unidos" in country:
            return "es_ES"
    return _FALLBACK_LANG


# ── helpers ───────────────────────────────────────────────────────────

def _text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return str(value).strip()


def _decimal(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else Decimal("0")
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.,-]", "", str(value))
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _money(order: dict[str, Any], amount: Any) -> str:
    amt = _decimal(amount)
    currency = order.get("currency", {}) or {}
    symbol = _text(currency.get("symbol") or "$")
    position = str(currency.get("position", "after")).strip().lower()
    formatted = f"{amt:.2f}"
    if position == "before":
        return f"{symbol}{formatted}"
    return f"{formatted} {symbol}"


def _order_date_text(order: dict[str, Any]) -> str:
    date_order = order.get("date_order", "")
    if not date_order:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", date_order)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"
    return _text(date_order)[:16]


def _tracking_text(order: dict[str, Any]) -> str:
    tracking = _text(order.get("tracking_number"))
    if tracking:
        return f"#{tracking}"
    fallback = _text(order.get("name") or order.get("pos_reference"))
    if not fallback:
        return ""
    match = re.search(r"(\d+)\s*$", fallback)
    return f"#{match.group(1)}" if match else fallback


def _table_text(order: dict[str, Any]) -> str:
    table = order.get("table_id")
    if isinstance(table, dict):
        return _text(table.get("table_number") or table.get("name"))
    return ""


# ── line / discount helpers ───────────────────────────────────────────

def _pick_number(line: dict[str, Any], keys: list[str]) -> Decimal:
    for key in keys:
        d = _parse_decimal(line.get(key))
        if d is not None and d > 0:
            return d
    return Decimal("0")


def _line_discounted_total(line: dict[str, Any]) -> Decimal:
    return _pick_number(line, [
        "price_subtotal_incl", "priceSubtotalIncl",
        "price_incl", "priceIncl",
        "price_with_tax", "priceWithTax",
        "total_with_tax", "totalWithTax",
        "price", "total_rounded", "totalRounded",
    ])


def _line_original_unit_price(line: dict[str, Any]) -> Decimal:
    return _pick_number(line, [
        "oldUnitPrice", "old_unit_price",
        "price_unit_before_discount", "priceUnitBeforeDiscount",
        "lst_price", "list_price", "listPrice",
    ])


def _line_discounted_unit_price(line: dict[str, Any]) -> Decimal:
    return _pick_number(line, [
        "unit_price", "unitPrice", "price_unit", "priceUnit",
        "display_price", "displayPrice",
    ])


def _line_qty(line: dict[str, Any]) -> Decimal:
    qty = line.get("qty") or line.get("quantity") or 0
    d = _parse_decimal(qty)
    return d if d is not None else Decimal("0")


def _line_original_total_from_fields(line: dict[str, Any]) -> Decimal:
    qty = _line_qty(line)
    direct = _pick_number(line, [
        "price_with_tax_before_discount", "priceWithTaxBeforeDiscount",
        "total_with_tax_before_discount", "totalWithTaxBeforeDiscount",
        "oldPrice", "old_price",
        "price_without_discount", "priceWithoutDiscount",
        "total_before_discount", "totalBeforeDiscount",
    ])
    if direct > 0:
        return direct
    unit = _line_original_unit_price(line)
    return unit * qty if qty and unit else Decimal("0")


def _line_discount_pct(line: dict[str, Any]) -> Decimal:
    discount_raw = line.get("discount")
    if discount_raw is not None:
        d = _parse_decimal(discount_raw)
        if d is not None and d > 0:
            return d

    direct = _pick_number(line, [
        "discount", "discount_pct", "discountPct",
        "discount_percent", "discountPercent",
    ])
    if direct > 0:
        return direct

    for key in ("discountStr", "discount_str", "discountText", "discount_text"):
        dt = _text(line.get(key))
        if dt:
            m = re.search(r"([\d.]+)", dt)
            if m:
                d = _parse_decimal(m.group(1))
                if d is not None and d > 0:
                    return d
            break

    discounted = _line_discounted_total(line)
    original = _line_original_total_from_fields(line)
    if original > discounted > 0:
        return ((original - discounted) / original) * Decimal("100")

    qty = _line_qty(line)
    discounted_unit = _line_discounted_unit_price(line)
    original_unit = _line_original_unit_price(line)
    if qty > 0 and original_unit > discounted_unit > 0:
        return ((original_unit - discounted_unit) / original_unit) * Decimal("100")
    return Decimal("0")


def _line_original_total(line: dict[str, Any], discounted: Decimal) -> Decimal:
    direct = _line_original_total_from_fields(line)
    if direct > 0:
        return direct
    discount = _line_discount_pct(line)
    if not discount or discount >= 100 or not discounted:
        return Decimal("0")
    return discounted / (Decimal("1") - discount / Decimal("100"))


def _order_discount_total(order: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for line in order.get("lines", []):
        if not isinstance(line, dict) or line.get("combo_parent_id"):
            continue
        discounted = _line_discounted_total(line)
        original = _line_original_total(line, discounted)
        if original > discounted > 0:
            total += original - discounted
    return total


def _calculate_subtotal(order: dict[str, Any]) -> Decimal:
    """Calculate subtotal (sum of all line discounted totals)."""
    total = Decimal("0")
    for line in order.get("lines", []):
        if not isinstance(line, dict) or line.get("combo_parent_id"):
            continue
        total += _line_discounted_total(line)
    return total


def _qty_text(line: dict[str, Any]) -> str:
    qty = line.get("qty") or line.get("quantity") or 0
    d = _parse_decimal(qty)
    if d is None:
        return "0"
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d:.2f}".rstrip("0").rstrip(".")


def _split_name_and_options(line: dict[str, Any]) -> tuple[str, list[str]]:
    display = line.get("orderDisplayProductName", {})
    if isinstance(display, dict):
        base_name = _text(display.get("name")) or _text(line.get("product_name")) or _text(line.get("full_product_name"))
        attr_str = _text(display.get("attributeString") or display.get("attribute_string"))
        if attr_str:
            return base_name, [s.strip() for s in attr_str.split(",") if s.strip()]

    full_name = _text(line.get("full_product_name"))
    if not full_name:
        full_name = _text(line.get("product_name"))
    match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", full_name) if full_name else None
    if match:
        return match.group(1).strip(), [s.strip() for s in match.group(2).split(",") if s.strip()]
    return base_name or full_name or "", []


# ── section builders ───────────────────────────────────────────────────

def _company_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    company = order.get("company")
    if not isinstance(company, dict):
        return []
    lines: list[dict[str, Any]] = []
    for field, bold in [("name", True), ("street", False), ("street2", False)]:
        val = _text(company.get(field))
        if val:
            lines.append({"text": val, "align": "center", "bold": bold})
    locality = " ".join(filter(None, [
        _text(company.get("zip")),
        _text(company.get("city")),
        _text(company.get("state_id")),
    ]))
    if locality:
        lines.append({"text": locality, "align": "center"})
    country = _text(company.get("country_id"))
    if country:
        lines.append({"text": country, "align": "center"})
    phone = _text(company.get("phone"))
    if phone:
        lines.append({"text": phone, "align": "center"})
    return lines


def _customer_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    partner = order.get("partner_id")
    if not isinstance(partner, dict):
        return []
    lines: list[dict[str, Any]] = []
    full_name = ", ".join(filter(None, [_text(partner.get("parent_name")), _text(partner.get("name"))]))
    if full_name:
        lines.append({"text": full_name, "align": "center"})
    address = _text(partner.get("pos_contact_address") or partner.get("street"))
    if address:
        lines.append({"text": address, "align": "center"})
    vat = _text(partner.get("vat"))
    if vat:
        lines.append({"text": vat, "align": "center"})
    return lines


def _portal_url(order: dict[str, Any]) -> str:
    base_url = _text(order.get("config", {}).get("_base_url") if isinstance(order.get("config"), dict) else "")
    return f"{base_url}/pos/ticket" if base_url else ""


def _ticket_qr_src(order: dict[str, Any]) -> str:
    company = order.get("company", {}) if isinstance(order.get("company"), dict) else {}
    if not company.get("point_of_sale_use_ticket_qr_code") or not order.get("finalized") or not order.get("access_token"):
        return ""
    base_url = _text(order.get("config", {}).get("_base_url") if isinstance(order.get("config"), dict) else "")
    if not base_url:
        return ""
    token = order["access_token"]
    validation_url = f"{base_url}/pos/ticket/validate?access_token={token}"
    return f"{base_url}/report/barcode?{urlencode({'barcode_type': 'QR', 'value': validation_url, 'width': 180, 'height': 180})}"


# ── kitchen ticket builder ────────────────────────────────────────────

def build_kitchen_ticket_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a kitchen / preparation display ticket.

    Matches the auto-print style from _buildKitchenEscposLines() in the
    Odoo preparation display service:
      1. "NUEVO" title (double width/height, centered, bold)
      2. Tracking number + table ref (header_meta_line)
      3. Separator
      4. Product lines (qty + name + attributes + notes)
      5. Separator
      6. Config/shop name (centered)
      7. Time (centered)
    """
    lines: list[dict[str, Any]] = []
    lang = _resolve_lang(order)
    L = _labels(lang)

    # ── 1. Order type line ──
    chino_order_type = _text(order.get("chino_order_type"))
    order_type = chino_order_type or "DINE IN"
    lines.append({
        "text": order_type,
        "align": "center", "bold": True,
        "double_width": True, "double_height": True,
    })

    # ── 2. "NUEVO" title (auto-print style) ──
    lines.append({
        "text": "NUEVO",
        "align": "center", "bold": True,
        "double_width": True, "double_height": True,
    })

    # ── 3. Tracking number + table ref (header_meta_line like auto-print) ──
    tracking = _text(order.get("tracking_number"))
    table = _table_text(order)
    left = f"# {tracking}" if tracking else ""
    right = f"{L['TABLE']} {table}" if table else ""
    if left or right:
        lines.append({
            "type": "header_meta_line",
            "left_text": left,
            "right_text": right,
            "bold": True,
            "double_width": True,
            "double_height": True,
        })

    lines.append({"text": SEPARATOR, "align": "left"})

    # ── 3. Product lines (qty x name) ──
    for raw_line in order.get("lines", []):
        if not isinstance(raw_line, dict):
            continue
        qty = _qty_text(raw_line)
        name, options = _split_name_and_options(raw_line)
        if not name:
            continue
        lines.append({
            "type": "product_line",
            "qty": qty,
            "name": name,
            "total": "",
            "double_width": True,
            "classes": ["kitchen-product-line"],
        })
        if options:
            for opt in options:
                lines.append({
                    "text": f"  + {opt}",
                    "align": "left",
                    "classes": ["kitchen-note"],
                })
        note = _text(raw_line.get("customer_note"))
        if note:
            lines.append({
                "text": f"  {L['NOTE']}: {note}",
                "align": "left", "bold": True,
                "classes": ["kitchen-note"],
            })
    lines.append({"text": SEPARATOR, "align": "left"})

    # ── 5. Config name (shop/restaurant name, centered) ──
    config = order.get("config", {}) if isinstance(order.get("config"), dict) else {}
    config_name = _text(config.get("name") or order.get("pos_reference") or "")
    if not config_name:
        company = order.get("company", {}) if isinstance(order.get("company"), dict) else {}
        config_name = _text(company.get("name"))
    if config_name:
        lines.append({"text": config_name, "align": "center"})

    # ── 6. Time (centered) ──
    date_text = _order_date_text(order)
    time_part = date_text[11:16] if len(date_text) >= 16 else date_text
    if time_part:
        lines.append({"text": time_part, "align": "center"})

    _logger.info(
        "Built kitchen ticket lines=%s table=%s tracking=%s",
        len(lines), table or "<none>", tracking or "<none>",
    )
    return lines


# ── main POS receipt builder ──────────────────────────────────────────

def build_receipt_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Build receipt lines from raw Odoo POS order JSON.

    All layout decisions are made here — the Odoo JS simply serialises
    the order and sends it across.  Returns a list of dicts compatible
    with ``DeviceManager._build_escpos_bytes()``.
    """
    lines: list[dict[str, Any]] = []
    lang = _resolve_lang(order)
    L = _labels(lang)
    is_final = bool(order.get("finalized"))
    discount_total = _order_discount_total(order)
    config = order.get("config", {}) if isinstance(order.get("config"), dict) else {}

    # ══════════════════════════════════════════════════════════════════
    # 1. Logo
    # ══════════════════════════════════════════════════════════════════
    logo_url = _text(config.get("receiptLogoUrl"))
    if logo_url:
        lines.append({
            "type": "image", "src": logo_url, "align": "left",
            "classes": ["pos-receipt-logo"],
            "width": 480, "height": 150, "image_kind": "logo",
        })

    # ══════════════════════════════════════════════════════════════════
    # 2. Company
    # ══════════════════════════════════════════════════════════════════
    lines.extend(_company_lines(order))

    # ══════════════════════════════════════════════════════════════════
    # 3. Customer
    # ══════════════════════════════════════════════════════════════════
    customer_info = _customer_lines(order)
    if customer_info:
        lines.extend(customer_info)
        lines.append({"text": "", "align": "left"})

    lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    # ══════════════════════════════════════════════════════════════════
    # 4. Shared date+cashier info (used below table)
    # ══════════════════════════════════════════════════════════════════
    reference = _text(order.get("pos_reference") or order.get("name"))
    date_text = _order_date_text(order)
    operator = _text(order.get("user_id", {}).get("name"))
    
    info_parts = []
    if date_text:
        info_parts.append(date_text)
    if operator:
        info_parts.append(operator)
    info_text = " | ".join(info_parts) if info_parts else ""

    # ══════════════════════════════════════════════════════════════════
    # 5. Table / order marker
    # ══════════════════════════════════════════════════════════════════
    table = _table_text(order)
    order_marker = _tracking_text(order)
    if table or order_marker:
        text = f"{L['TABLE']} {table}" if table else order_marker
        lines.append({
            "text": text, "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        })
        lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    # ══════════════════════════════════════════════════════════════════
    # 6. Simplified invoice info (Factura Simplificada)
    # ══════════════════════════════════════════════════════════════════
    if is_final:
        order_name = _text(order.get("name"))
        seq_match = re.search(r"(\d+)$", order_name) if order_name else None
        if seq_match:
            seq_number = seq_match.group(1)
            current_year = date_text[:4] if date_text else "2026"
            lines.append({"text": "*" * 26, "align": "center", "classes": ["invoice-asterisk-border"]})
            lines.append({"text": "Factura Simplificada", "align": "center", "bold": True})
            lines.append({"text": f"Fs/{current_year}/{seq_number}", "align": "center"})
            lines.append({"text": "*" * 26, "align": "center", "classes": ["invoice-asterisk-border"]})
            lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    # ══════════════════════════════════════════════════════════════════
    # 7. Reference + date + cashier (below table)
    # ══════════════════════════════════════════════════════════════════
    order_label = f"{L['ORDER']} {reference}" if reference else ""
    if order_label:
        lines.append({
            "text": order_label,
            "align": "center", "bold": True,
        })
    if info_text:
        lines.append({
            "text": info_text,
            "align": "center",
        })
    if order_label or info_text:
        lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    # ══════════════════════════════════════════════════════════════════
    # 7. Product lines
    # ══════════════════════════════════════════════════════════════════
    for raw_line in order.get("lines", []):
        if not isinstance(raw_line, dict) or raw_line.get("combo_parent_id"):
            continue
        name, options = _split_name_and_options(raw_line)
        discounted = _line_discounted_total(raw_line)
        if discounted <= 0:
            continue
        discount_pct = _line_discount_pct(raw_line)
        original = _line_original_total(raw_line, discounted)
        entry: dict[str, Any] = {
            "type": "product_line",
            "qty": _qty_text(raw_line),
            "name": name,
            "total": _money(order, discounted),
            "combo_items": options,
        }
        if discount_pct > 0:
            entry["discount_text"] = f"{float(discount_pct):.0f}%"
        if original > 0:
            entry["original_total"] = _money(order, original)
        lines.append(entry)
        lines.append({
            "type": "spacer", "align": "left",
            "classes": ["receipt-spacer", "product-line-spacer"],
        })
        note = _text(raw_line.get("customer_note"))
        if note:
            lines.append({
                "text": f"  {L['NOTE']}: {note}",
                "align": "left", "classes": ["customer-note"],
            })

    # ══════════════════════════════════════════════════════════════════
    # 9. Spacer (replaces separator before Discount/Tax)
    # ══════════════════════════════════════════════════════════════════
    lines.append({"text": "", "align": "left", "classes": ["receipt-spacer"]})

    # ══════════════════════════════════════════════════════════════════
    # 10. Subtotal
    # ══════════════════════════════════════════════════════════════════
    subtotal_amount = _calculate_subtotal(order)
    if subtotal_amount > 0:
        lines.append({
            "type": "header_meta_line",
            "left_text": L["SUBTOTAL"],
            "right_text": _money(order, subtotal_amount),
        })

    # ══════════════════════════════════════════════════════════════════
    # 11. Discount summary
    # ══════════════════════════════════════════════════════════════════
    if discount_total > 0:
        amt = _decimal(discount_total)
        lines.append({
            "type": "header_meta_line",
            "left_text": L["DISCOUNT"],
            "right_text": f"-{amt:.2f}",
        })

    # ══════════════════════════════════════════════════════════════════
    # 12. Tax (use actual tax names from system)
    # ══════════════════════════════════════════════════════════════════
    tax_amount = order.get("amountTaxes")
    if tax_amount is not None and _decimal(tax_amount) > 0:
        tax_names = order.get("tax_names", [])
        tax_label = ", ".join(tax_names) if tax_names else L["TAX"]
        lines.append({
            "type": "header_meta_line",
            "left_text": tax_label,
            "right_text": _money(order, tax_amount),
        })

    # ══════════════════════════════════════════════════════════════════
    # 13. Separator + TOTAL
    # ══════════════════════════════════════════════════════════════════
    lines.append({"text": SEPARATOR, "align": "left"})
    total_due = order.get("totalDue")
    if total_due is not None:
        amt = _decimal(total_due)
        lines.append({
            "text": f"{L['TOTAL']} {amt:.2f} EUR",
            "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        })

    # ══════════════════════════════════════════════════════════════════
    # 14. Payment section (final receipts only)
    # ══════════════════════════════════════════════════════════════════
    if is_final:
        lines.append({"text": SEPARATOR, "align": "left"})

        # Individual payment lines (show payment method name instead of PAID label)
        payment_lines = order.get("payment_lines") or order.get("statement_ids") or []
        has_payment_line = False
        for pl in payment_lines:
            if isinstance(pl, dict):
                pname = _text(pl.get("name") or pl.get("payment_method_id"))
                pamount = pl.get("amount")
                if pamount:
                    lines.append({
                        "type": "header_meta_line",
                        "left_text": pname or "Pago",
                        "right_text": _money(order, pamount),
                    })
                    has_payment_line = True

        # If no individual payment lines, show the total paid amount with a generic label
        amount_paid = order.get("amountPaid")
        if amount_paid is not None and not has_payment_line:
            lines.append({
                "type": "header_meta_line",
                "left_text": L["PAID"],
                "right_text": _money(order, amount_paid),
            })

        # Change calculation (only show if there is change)
        if amount_paid is not None:
            total_val = _decimal(total_due or 0)
            paid_val = _decimal(amount_paid)
            change = max(Decimal("0"), paid_val - total_val)
            if change > 0:
                lines.append({
                    "type": "header_meta_line",
                    "left_text": L["CHANGE"],
                    "right_text": _money(order, change),
                })

    # ══════════════════════════════════════════════════════════════════
    # 15. QR + Portal URL
    # ══════════════════════════════════════════════════════════════════
    if is_final:
        qr_src = _ticket_qr_src(order)
        ticket_code = _text(order.get("ticket_code"))
        if qr_src or ticket_code:
            lines.append({"text": "", "align": "left"})
        if qr_src:
            lines.append({
                "type": "image", "src": qr_src, "align": "center",
                "classes": ["portal-qr"],
                "width": 180, "height": 180, "image_kind": "qr",
            })
        portal = _portal_url(order)
        url_mode = _text(order.get("company", {}).get("point_of_sale_ticket_portal_url_display_mode"))
        if portal and url_mode in ("url", "qr_code_and_url"):
            lines.append({"text": portal, "align": "center", "classes": ["portal-url"]})
        if ticket_code:
            lines.append({
                "text": f"{L['CODE']}: {ticket_code}",
                "align": "center", "classes": ["unique-code"],
            })

    # ══════════════════════════════════════════════════════════════════
    # 17. Footer
    # ══════════════════════════════════════════════════════════════════
    footer = _text(config.get("receipt_footer"))
    if footer:
        for fl in footer.split("\n"):
            fl = fl.strip()
            if fl:
                lines.append({"text": fl, "align": "center", "classes": ["pos-config-name"]})

    # ══════════════════════════════════════════════════════════════════
    # 18. Takeout/Delivery info — LARGE, at the very bottom for tear-off
    # ══════════════════════════════════════════════════════════════════
    chino_order_type = _text(order.get("chino_order_type"))
    if chino_order_type == "DELIVERY":
        chino_floating = _text(order.get("chino_floating_order_name"))

        # Blank lines so this section can be torn off
        for _ in range(4):
            lines.append({"type": "spacer", "align": "left", "classes": ["receipt-spacer"]})

        lines.append({"text": SEPARATOR, "align": "left"})
        # 1. Order type + number (only show number for takeout, not delivery phone)
        lines.append({
            "text": chino_order_type,
            "align": "center", "bold": True,
            "double_width": True, "double_height": True,
        })
        if chino_floating and not re.match(r"^\d{9}$", chino_floating):
            lines.append({
                "text": chino_floating,
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })
        # 2. Blank line
        lines.append({"type": "spacer", "align": "left", "classes": ["receipt-spacer"]})
        # 3. Phone + 4. Address — smaller font
        partner = order.get("partner_id", {})
        if isinstance(partner, dict):
            phone = _text(partner.get("phone") or partner.get("mobile"))
            if phone:
                lines.append({"text": phone, "align": "center", "bold": True})
            address = _text(partner.get("pos_contact_address") or partner.get("street"))
            if address:
                lines.append({"text": address, "align": "center", "bold": True})
        # 5. Blank line
        lines.append({"type": "spacer", "align": "left", "classes": ["receipt-spacer"]})
        # 6. Total
        total_due = order.get("totalDue")
        if total_due is not None:
            amt = _decimal(total_due)
            lines.append({
                "text": f"TOTAL: {amt:.2f} EUR",
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })
        lines.append({"text": SEPARATOR, "align": "left"})

    _logger.info(
        "Built receipt lines lang=%s lines=%s is_final=%s discount_total=%s",
        lang, len(lines), is_final, float(discount_total),
    )
    return lines


# ── public dispatch ───────────────────────────────────────────────────

def build_lines(order: dict[str, Any], kitchen: bool = False) -> list[dict[str, Any]]:
    """Dispatch to the appropriate builder based on ticket type."""
    if kitchen:
        return build_kitchen_ticket_lines(order)
    return build_receipt_lines(order)
