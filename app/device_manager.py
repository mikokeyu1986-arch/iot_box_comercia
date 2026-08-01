"""Runtime loader for the recovered device_manager bytecode.

The original source was damaged, but the complete CPython bytecode remains
in device_manager.pyc. This loader keeps the module importable while the
source is reconstructed separately.
"""
from pathlib import Path
import marshal

_bytecode = Path(__file__).with_suffix(".pyc").read_bytes()
_code = marshal.loads(_bytecode[16:])
exec(_code, globals(), globals())

# Restore complete Redsys terminal details for structured receipts.
def _restore_payment_lines(self, receipt_item):
    values = receipt_item.get("lines") if isinstance(receipt_item, dict) else []
    return [str(value).strip() for value in (values or []) if str(value).strip()]

def _restore_payment_line_text(self, text):
    import re
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if compact.lower().startswith("method:"):
        return ""
    return compact


def _restore_receipt_amount_layout(lines):
    import re

    result = []
    amount_pattern = re.compile(r"^(.*?)\s+([-+]?\d[\d.,]*\s*(?:€|EUR|鈧\?)?)$")
    for line in lines:
        if not isinstance(line, dict):
            result.append(line)
            continue
        text = str(line.get("text") or "").strip()
        classes = line.get("classes") if isinstance(line.get("classes"), list) else []
        is_total = "total" in text.lower() or "receipt-total" in classes
        is_subtotal = text.lower().startswith("subtotal")
        if is_subtotal:
            while (
                result
                and isinstance(result[-1], dict)
                and (
                    "separator" in [str(cls) for cls in result[-1].get("classes") or []]
                    or (
                        str(result[-1].get("text") or "").strip()
                        and set(str(result[-1].get("text") or "").strip()) <= {"-"}
                    )
                )
            ):
                result.pop()
        if is_total and not (
            result
            and isinstance(result[-1], dict)
            and "separator" in [str(cls) for cls in result[-1].get("classes") or []]
        ):
            result.append({
                "text": "-" * 42,
                "align": "left",
                "bold": False,
                "double_width": False,
                "classes": ["receipt-separator", "before-total"],
            })
        is_amount_row = (
            "paymentlines" in classes
            or text.lower().startswith(("subtotal", "iva", "tax", "discount", "paid", "change"))
        )
        match = amount_pattern.match(text) if is_amount_row else None
        if match:
            left_text, right_text = match.groups()
            line = {
                **line,
                "type": "header_meta_line",
                "left_text": left_text.strip(),
                "right_text": right_text.strip(),
            }
            line.pop("text", None)
        result.append(line)
    return result


def _add_portal_description_spacer(lines):
    result = []
    for line in lines:
        if (
            result
            and isinstance(result[-1], dict)
            and "portal-qr" in [str(cls) for cls in result[-1].get("classes") or []]
            and isinstance(line, dict)
            and str(line.get("text") or "").strip()
            and not str(line.get("type") or "") == "spacer"
        ):
            result.append({"type": "spacer", "align": "left", "classes": ["portal-description-spacer"]})
        result.append(line)
    return result

if "DeviceManager" in globals():
    DeviceManager._iter_payment_terminal_receipt_lines = _restore_payment_lines
    DeviceManager._normalize_payment_terminal_line = _restore_payment_line_text

    _original_structured_lines = DeviceManager._build_structured_receipt_lines

    def _independent_redsys_block(self, receipt):
        lines = _original_structured_lines(self, receipt)
        terminal = []
        remaining = []
        for line in lines:
            if isinstance(line, dict) and "iu-2.png" in str(line.get("src") or "").lower():
                # Remove the original NFC image so only the merged block image remains.
                continue
            classes = line.get("classes") if isinstance(line, dict) else []
            if isinstance(classes, list) and (
                "payment-terminal-line" in classes
                or "pos-payment-terminal-receipt" in classes
            ):
                terminal.append(line)
            else:
                remaining.append(line)
        if not terminal:
            return lines
        remaining = _restore_receipt_amount_layout(remaining)
        remaining = _add_portal_description_spacer(remaining)
        # Keep the Redsys section independent at the very bottom.
        block = [{"type": "spacer", "align": "left", "classes": ["payment-terminal-spacer"]}]
        block.append({
            "type": "image",
            "src": "/iot_box_comercia/web/iu-2.png",
            "align": "center",
            "width": 78,
            "image_kind": "image",
            "classes": ["payment-terminal-nfc-icon"],
        })
        block.extend(terminal)
        block.append({"type": "spacer", "align": "left", "classes": ["payment-terminal-spacer"]})
        return remaining + block

    DeviceManager._build_structured_receipt_lines = _independent_redsys_block

    _original_image_size = DeviceManager._escpos_requested_image_size

    def _small_iu2_image(self, line, max_width):
        src = str(line.get("src") or "").lower() if isinstance(line, dict) else ""
        if "iu-2.png" in src:
            return (78, None)
        return _original_image_size(self, line, max_width)

    DeviceManager._escpos_requested_image_size = _small_iu2_image

    _original_render_escpos_lines = DeviceManager._render_escpos_lines
    def _render_receipt_spacer_line(self, line, width):
        classes = line.get("classes") if isinstance(line, dict) else []
        if isinstance(classes, list) and "after-unique-code" in classes:
            # A whitespace line is deliberately emitted so the printer advances
            # paper; an empty text field is discarded by the normal renderer.
            return [" "]
        return _original_render_escpos_lines(self, line, width)
    DeviceManager._render_escpos_lines = _render_receipt_spacer_line

    _original_build_bytes = DeviceManager._build_escpos_bytes
    _original_build_image = DeviceManager._build_escpos_image

    def _conditional_receipt_bytes(self, lines, cut=True, payload=None, normalize_lines=True):
        has_terminal = any(
            isinstance(line, dict)
            and ("payment-terminal-line" in (line.get("classes") or []))
            for line in (lines or [])
        )
        if has_terminal:
            seen_iu2 = {"count": 0}

            def one_iu2(line):
                src = str(line.get("src") or "").lower() if isinstance(line, dict) else ""
                if "iu-2.png" in src:
                    seen_iu2["count"] += 1
                    if seen_iu2["count"] > 1:
                        return b""
                return _original_build_image(self, line)

            old_image = self._build_escpos_image
            self._build_escpos_image = one_iu2
            try:
                result = _original_build_bytes(self, lines, cut, payload, False)
                return result
            finally:
                self._build_escpos_image = old_image

        def no_iu2(line):
            src = str(line.get("src") or "").lower() if isinstance(line, dict) else ""
            if "iu-2.png" in src:
                return b""
            return _original_build_image(self, line)

        old_image = self._build_escpos_image
        self._build_escpos_image = no_iu2
        try:
            return _original_build_bytes(self, lines, cut, payload, normalize_lines)
        finally:
            self._build_escpos_image = old_image

    DeviceManager._build_escpos_bytes = _conditional_receipt_bytes
