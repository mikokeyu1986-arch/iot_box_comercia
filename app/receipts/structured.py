from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlencode

from ..kitchen_template_store import apply_kitchen_template
from ..receipt_template_store import apply_template
from ..printing.common import perf_log as _perf_log

_logger = logging.getLogger(__name__)

class StructuredReceiptMixin:
    def _build_structured_receipt_lines(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        loyalty_cards = [card for card in (receipt.get("loyalty_cards") or []) if isinstance(card, dict)]
        loyalty_card_names = {
            str(card.get("name") or "").strip().lower()
            for card in loyalty_cards
            if str(card.get("name") or "").strip()
        }
        _perf_log(
            "[IOT ESCPOS PAYLOAD] "
            + json.dumps(
                {
                    "summary_lines": receipt.get("summary_lines") or [],
                    "total_line": receipt.get("total_line") or "",
                    "payment_lines": receipt.get("payment_lines") or [],
                    "change_line": receipt.get("change_line") or "",
                    "discount_line": receipt.get("discount_line") or "",
                },
                ensure_ascii=False,
            )
        )

        logo = receipt.get("logo") if isinstance(receipt.get("logo"), dict) else None
        logo_src = str(logo.get("src") or "").strip() if logo else ""
        if logo_src:
            logo_src = self._with_logo_cache_buster(logo_src)
            lines.append(
                {
                    "type": "image",
                    "src": logo_src,
                    "align": "left",
                    "classes": ["pos-receipt-logo"],
                    "width": 480,
                    "height": 150,
                    "image_kind": "logo",
                }
            )

        company_section_added = False
        company_lines, inferred_reference = self._split_company_and_reference_lines(receipt.get("company_lines") or [])
        for text, is_bold in company_lines:
            company_section_added = True
            lines.append(
                {
                    "text": text,
                    "align": "center",
                    "bold": is_bold,
                    "double_width": False,
                    "classes": ["company-info"],
                }
            )

        order_info_lines: list[dict[str, Any]] = []
        for value in (
            inferred_reference,
            receipt.get("reference_text"),
            receipt.get("date_text"),
            receipt.get("cashier_text"),
        ):
            text = self._normalize_order_info_text(value)
            if text:
                upper_text = text.upper()
                is_table_line = upper_text.startswith("MESA ") or upper_text.startswith("TABLE ")
                order_info_lines.append(
                    {
                        "text": text,
                        "align": "center",
                        "bold": is_table_line,
                        "double_width": is_table_line,
                        "double_height": is_table_line,
                        "classes": ["table-info"] if is_table_line else ["order-info"],
                    }
                )

        if company_section_added and order_info_lines:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "company-order-spacer"],
                }
            )
        lines.extend(order_info_lines)

        # Detect whether this receipt carries a table (MESA/TABLE) marker.
        # Receipts without a table should stay compact: no extra blank lines
        # and no doubled separators around the barcode/product section.
        has_table = False
        for order_line in order_info_lines:
            upper = str(order_line.get("text") or "").upper()
            if "MESA" in upper or "TABLE" in upper:
                has_table = True
                break
        if not has_table:
            for header_text in receipt.get("header_lines") or []:
                upper = str(header_text or "").upper()
                if "MESA" in upper or "TABLE" in upper:
                    has_table = True
                    break

        # ── Simplified invoice info (Factura Simplificada) ─────────────
        factura_number = str(receipt.get("factura_simplificada_number") or "").strip()
        if factura_number:
            lines.append(
                {
                    "text": "*" * 26,
                    "align": "center",
                    "classes": ["invoice-asterisk-border"],
                }
            )
            lines.append(
                {
                    "text": "Factura Simplificada",
                    "align": "center",
                    "bold": True,
                    "double_width": False,
                    "double_height": False,
                    "classes": ["simplified-invoice-title"],
                }
            )
            lines.append(
                {
                    "text": factura_number,
                    "align": "center",
                    "bold": False,
                    "double_width": False,
                    "double_height": False,
                    "classes": ["simplified-invoice-number"],
                }
            )
            lines.append(
                {
                    "text": "*" * 26,
                    "align": "center",
                    "classes": ["invoice-asterisk-border"],
                }
            )
            if has_table:
                lines.append(
                    {
                        "type": "spacer",
                        "align": "left",
                        "classes": ["receipt-spacer", "company-order-spacer"],
                    }
                )

        for item in receipt.get("company_lines") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            # Remaining header lines that weren't promoted into the fixed
            # company/order sections stay out to avoid duplicated content.
            continue

        for text in receipt.get("header_lines") or []:
            text = str(text or "").strip()
            if text:
                lines.append({"text": text, "align": "center", "bold": False, "double_width": False, "classes": []})

        customer = receipt.get("customer") if isinstance(receipt.get("customer"), dict) else None
        if not customer:
            customer = self._load_customer_from_reference_text(receipt.get("reference_text"))
        customer_rows = [
            str(customer.get("name") or "").strip() if customer else "",
            str(customer.get("vat") or "").strip() if customer else "",
            str(customer.get("address") or "").strip() if customer else "",
            str(customer.get("region") or "").strip() if customer else "",
        ]
        customer_rows = [text for text in customer_rows if text]
        if customer_rows:
            lines.append(
                {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "customer-info-separator"],
                }
            )
            for text in customer_rows:
                lines.append(
                    {
                        "text": text,
                        "align": "center",
                        "bold": False,
                        "double_width": False,
                        "classes": ["customer-info"],
                    }
                )
            lines.append(
                {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "customer-info-separator"],
                }
            )

        barcode = receipt.get("barcode") if isinstance(receipt.get("barcode"), dict) else None
        barcode_src = str(barcode.get("src") or "").strip() if barcode else ""
        if barcode_src:
            barcode_is_qr = self._is_qr_receipt_image_src(barcode_src, barcode)
            barcode_width = int(barcode.get("width") or (200 if barcode_is_qr else 260))
            barcode_height = int(barcode.get("height") or (barcode_width if barcode_is_qr else 58))
            barcode_classes = ["order-qr-img"] if barcode_is_qr else ["order-barcode-img"]
            if not has_table:
                # Compact receipts without a table: don't print the extra
                # separator line right after the barcode.
                barcode_classes.append("no-barcode-separator")
            lines.append(
                {
                    "type": "image",
                    "src": barcode_src,
                    "align": "center",
                    "classes": barcode_classes,
                    "width": barcode_width,
                    "height": barcode_height,
                    "image_kind": "qr" if barcode_is_qr else "barcode",
                }
            )

        receipt_items = [item for item in (receipt.get("items") or []) if isinstance(item, dict)]
        for item_index, item in enumerate(receipt_items):
            qty = str(item.get("qty") or "").strip()
            raw_name = str(item.get("name") or "").strip()
            total = str(item.get("total") or "").strip()
            if not qty or not raw_name or not total:
                continue
            name = raw_name
            if loyalty_card_names and name.strip().lower() in loyalty_card_names:
                continue
            combo_items = [
                str(combo).strip() for combo in (item.get("combo_items") or []) if str(combo).strip()
            ]
            # NOTE: the product column header ("Uds. Producto Importe") and its
            # leading separator are intentionally NOT generated here. They are
            # printed once by _build_escpos_bytes() when it encounters the first
            # product_line, using _build_product_header_text() (fully aligned).
            # Generating them here would produce a flattened duplicate header.
            lines.append(
                {
                    "type": "product_line",
                    "qty": qty,
                    "name": name,
                    "unit_price": str(item.get("unit_price") or "").strip(),
                    "total": total,
                    "combo_items": combo_items,
                    "discount_text": str(item.get("discount_text") or "").strip(),
                    "original_total": str(item.get("original_total") or "").strip(),
                }
            )
            customer_note = str(item.get("customer_note") or "").strip()
            if customer_note:
                lines.append(
                    {
                        "text": customer_note,
                        "align": "left",
                        "bold": False,
                        "double_width": False,
                        "classes": ["customer-note"],
                    }
                )
        summary_lines = [str(text or "").strip() for text in receipt.get("summary_lines") or [] if str(text or "").strip()]
        if summary_lines:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "summary-line-spacer"],
                }
            )
        for index, text in enumerate(summary_lines):
            if text:
                next_text = summary_lines[index + 1].lower() if index + 1 < len(summary_lines) else ""
                is_subtotal_line = text.lower().startswith("subtotal")
                next_is_tax_line = self._looks_like_tax_summary_line(next_text)
                lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": []})
                is_last_summary_line = index == len(summary_lines) - 1
                if (
                    not self._looks_like_tax_summary_line(text)
                    and not (is_subtotal_line and next_is_tax_line)
                    and not is_last_summary_line
                ):
                    lines.append(
                        {
                            "type": "spacer",
                            "align": "left",
                            "classes": ["receipt-spacer", "summary-line-spacer"],
                        }
                    )

        total_line = str(receipt.get("total_line") or "").strip()
        if total_line:
            lines.append(
                {
                    "text": total_line,
                    "align": "center",
                    "bold": True,
                    "double_width": True,
                    "classes": ["receipt-total"],
                }
            )

        for item in receipt.get("payment_lines") or []:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": ["paymentlines"]})
                lines.append(
                    {
                        "type": "spacer",
                        "align": "left",
                        "classes": ["receipt-spacer", "summary-line-spacer"],
                    }
                )

        for value in (receipt.get("change_line"), receipt.get("discount_line")):
            text = str(value or "").strip()
            if text:
                lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": []})

        for receipt_item in receipt.get("payment_terminal_receipts") or []:
            if not isinstance(receipt_item, dict):
                continue
            # 禁止在刷卡小票中打印图片，只保留刷卡文字记录。
            terminal_logo_src = ""
            if terminal_logo_src:
                lines.append(
                    {
                        "type": "image",
                        "src": terminal_logo_src,
                        "align": "center",
                        "classes": ["payment-terminal-logo"],
                        # 只限制宽度，不固定高度，保持原始 PNG 比例。
                        "width": 179,
                        "image_kind": "logo",
                    }
                )
            for text in self._iter_payment_terminal_receipt_lines(receipt_item):
                text = self._normalize_payment_terminal_line(text)
                if text:
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": False,
                            "double_width": False,
                            "classes": ["payment-terminal-line", "pos-payment-terminal-receipt"],
                        }
                    )

        portal = receipt.get("portal") if isinstance(receipt.get("portal"), dict) else None
        if portal and portal.get("show"):
            qr_src = str(
                portal.get("qrSrc")
                or portal.get("qr_src")
                or portal.get("qr")
                or portal.get("src")
                or ""
            ).strip()
            if qr_src:
                qr_size = max(180, int(os.getenv("IOT_PORTAL_QR_SIZE", "200") or "200"))
                lines.append(
                    {
                        "type": "image",
                        "src": qr_src,
                        "align": "center",
                        "classes": ["m-0", "portal-qr"],
                        "width": qr_size,
                        "height": qr_size,
                        "image_kind": "qr",
                    }
                )
            for value, bold in (
                (portal.get("title"), True),
                (portal.get("url"), False),
                (portal.get("code"), False),
            ):
                text = str(value or "").strip()
                if text:
                    classes: list[str] = []
                    if value == portal.get("url"):
                        classes.append("portal-url")
                    elif value == portal.get("code"):
                        classes.append("unique-code")
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": bold,
                            "double_width": False,
                            "classes": classes,
                        }
                    )

        for loyalty_card in loyalty_cards:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "gift-card-spacer"],
                }
            )
            loyalty_card_code = str(loyalty_card.get("code") or "").strip()
            for value, bold, classes in (
                (loyalty_card.get("name"), True, ["gift-card-title"]),
                (loyalty_card_code, False, ["gift-card-code"]),
            ):
                text = str(value or "").strip()
                if text:
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": bold,
                            "double_width": False,
                            "classes": classes,
                        }
                    )
            gift_card_barcode_src = ""
            if loyalty_card_code:
                encoded_query = urlencode(
                    {
                        "barcode_type": "Code128",
                        "value": loyalty_card_code,
                        "width": 360,
                        "height": 80,
                    }
                )
                gift_card_barcode_src = f"/report/barcode?{encoded_query}"
            qr_src = str(loyalty_card.get("qrSrc") or "").strip()
            if gift_card_barcode_src:
                lines.append(
                    {
                        "type": "image",
                        "src": gift_card_barcode_src,
                        "align": "center",
                        "classes": ["gift-card-barcode"],
                        "width": 360,
                        "height": 80,
                        "image_kind": "barcode",
                    }
                )
            elif qr_src:
                lines.append(
                    {
                        "type": "image",
                        "src": qr_src,
                        "align": "center",
                        "classes": ["gift-card-qr"],
                        "width": 125,
                        "height": 125,
                        "image_kind": "qr",
                    }
                )
            amount_field, amount_text = self._gift_card_display_amount(loyalty_card)
            _logger.info(
                "Gift card payload code=%s amount_field=%s display_amount=%s payload=%s",
                loyalty_card_code or "<missing>",
                amount_field or "<none>",
                amount_text or "<empty>",
                json.dumps(loyalty_card, ensure_ascii=False, default=str),
            )
            if amount_text:
                lines.append(
                    {
                        "text": amount_text,
                        "align": "center",
                        "bold": True,
                        "double_width": True,
                        "classes": ["gift-card-amount"],
                    }
                )
        # Footer lines: skip any line that duplicates the company name shown at
        # the top (e.g. a trailing "My Company") to avoid repetition.
        company_names = {
            str(item.get("text") or "").strip()
            for item in (receipt.get("company_lines") or [])
            if isinstance(item, dict)
            and item.get("bold")
            and str(item.get("text") or "").strip()
        }
        for text in receipt.get("footer_lines") or []:
            text = str(text or "").strip()
            if not text or text in company_names:
                continue
            lines.append(
                {
                    "text": text,
                    "align": "center",
                    "bold": False,
                    "double_width": False,
                    "classes": ["pos-config-name"],
                }
            )

        return self._apply_visual_template_to_structured_lines(lines)

    def _apply_visual_template_to_structured_lines(
        self, lines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Tag native Odoo structured lines and apply the saved receipt template."""
        first_product = next(
            (index for index, line in enumerate(lines) if line.get("type") == "product_line"),
            len(lines),
        )
        first_payment = next(
            (
                index for index, line in enumerate(lines)
                if {"paymentlines", "payment-terminal-line", "pos-payment-terminal-receipt"}
                .intersection({str(value) for value in line.get("classes") or []})
            ),
            len(lines),
        )
        tagged: list[dict[str, Any]] = []
        product_header_added = False
        for index, source in enumerate(lines):
            line = dict(source)
            classes = {str(value) for value in line.get("classes") or []}
            if line.get("type") == "product_line" and not product_header_added:
                tagged.append({
                    "type": "product_header",
                    "bold": True,
                    "_template_block": "product_header",
                })
                product_header_added = True

            if "pos-receipt-logo" in classes:
                block = "logo"
            elif "company-info" in classes:
                block = "company"
            elif classes.intersection({"customer-info", "customer-info-separator"}):
                block = "customer"
            elif any(value.startswith("simplified-invoice") or value == "invoice-asterisk-border" for value in classes):
                block = "invoice"
            elif "table-info" in classes:
                block = "table"
            elif classes.intersection({"order-info", "company-order-spacer", "order-barcode-img", "order-qr-img"}):
                block = "order_info"
            elif line.get("type") == "product_line" or classes.intersection({"customer-note", "product-line-spacer"}):
                block = "products"
            elif "receipt-total" in classes or (first_product < index < first_payment):
                block = "totals"
            elif classes.intersection({"paymentlines", "payment-terminal-line", "pos-payment-terminal-receipt"}):
                block = "payments"
            elif any(value.startswith("portal-") or value.startswith("gift-card-") for value in classes):
                block = "qr"
            elif "pos-config-name" in classes:
                block = "footer"
            elif index < first_product:
                block = "order_info"
            elif index >= first_payment:
                block = "payments"
            else:
                block = "totals"
            line["_template_block"] = block
            tagged.append(line)
        def is_separator(candidate: dict[str, Any] | None) -> bool:
            text = str((candidate or {}).get("text") or "").strip()
            return bool(text) and set(text) <= {"-", "="}

        with_total_separators: list[dict[str, Any]] = []
        for index, line in enumerate(tagged):
            classes = {str(value) for value in line.get("classes") or []}
            if "receipt-total" in classes:
                separator = {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "template-total-separator"],
                    "_template_block": "totals",
                }
                if not is_separator(with_total_separators[-1] if with_total_separators else None):
                    with_total_separators.append(dict(separator))
                with_total_separators.append(line)
                next_line = tagged[index + 1] if index + 1 < len(tagged) else None
                if not is_separator(next_line):
                    with_total_separators.append(dict(separator))
            else:
                with_total_separators.append(line)
        return apply_template(with_total_separators)

    def _apply_visual_template_to_kitchen_lines(
        self, lines: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Adapt legacy native kitchen lines to the saved kitchen template."""
        first_product = next(
            (index for index, line in enumerate(lines) if line.get("type") == "product_line"),
            len(lines),
        )
        last_product = max(
            (
                index for index, line in enumerate(lines)
                if line.get("type") == "product_line"
                or "kitchen-note" in {str(value) for value in line.get("classes") or []}
            ),
            default=-1,
        )
        tagged: list[dict[str, Any]] = []
        order_type_assigned = False
        for index, source in enumerate(lines):
            line = dict(source)
            classes = {str(value) for value in line.get("classes") or []}
            text = str(line.get("text") or "").strip()
            if line.get("type") == "header_meta_line":
                left = str(line.get("left_text") or "").strip()
                right = str(line.get("right_text") or "").strip()
                if left:
                    tagged.append({**line, "right_text": "", "_template_block": "order_meta"})
                if right:
                    tagged.append({
                        "text": right,
                        "align": "center",
                        "bold": bool(line.get("bold", True)),
                        "double_width": bool(line.get("double_width", False)),
                        "double_height": bool(line.get("double_height", True)),
                        "_template_block": "status",
                    })
                continue
            if line.get("type") == "product_line" or "kitchen-note" in classes:
                block = "products"
            elif text and set(text) <= {"-", "="}:
                block = "separator_before" if index < first_product else "separator_after"
            elif "kitchen-footer" in classes:
                block = "time" if re.fullmatch(r"\d{1,2}:\d{2}", text) else "location"
            elif "MESA" in text.upper() or "TABLE" in text.upper() or text.startswith("#"):
                block = "order_meta"
            elif index < first_product and not order_type_assigned and text:
                block = "order_type"
                order_type_assigned = True
            elif index < first_product:
                block = "status"
            elif index > last_product:
                block = "location"
            else:
                block = "products"
            line["_template_block"] = block
            tagged.append(line)
        return apply_kitchen_template(tagged)

    def _split_receipt_product_name_and_options(self, raw_name: str) -> tuple[str, list[str]]:
        text = str(raw_name or "").strip()
        if not text:
            return "", []
        match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", text)
        if not match:
            return text, []
        base_name = match.group(1).strip() or text
        options_text = match.group(2).strip()
        if not options_text:
            return base_name, []
        options = [part.strip() for part in options_text.split(",") if part.strip()]
        return base_name, options
