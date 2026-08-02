#!/usr/bin/env python3
"""Standalone entry point for the project's ESC/POS receipt template."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.receipt_builder import build_lines  # noqa: E402


def render_text(lines: list[dict[str, Any]], width: int = 48) -> str:
    """Create a readable preview without requiring an ESC/POS printer."""
    output: list[str] = []
    for line in lines:
        line_type = str(line.get("type") or "text")
        if line_type == "spacer":
            output.append("")
            continue
        if line_type == "image":
            if line.get("image_kind") == "barcode":
                value = line.get("barcode_value", "")
                output.append(_align(f"[BARCODE Code128: {value}]", line.get("align"), width))
            else:
                output.append(_align(f"[IMAGE: {line.get('src', '')}]", line.get("align"), width))
            continue
        if line_type == "header_meta_line":
            output.append(_columns(line.get("left_text"), line.get("right_text"), width))
            continue
        if line_type == "product_line":
            left = f"{line.get('qty', '')} x {line.get('name', '')}".strip()
            output.append(_columns(left, line.get("total"), width))
            for option in line.get("combo_items") or []:
                output.append(f"  + {option}")
            continue
        output.append(_align(str(line.get("text") or ""), line.get("align"), width))
    return "\n".join(output)


def _align(text: str, align: Any, width: int) -> str:
    if align == "center":
        return text.center(width)
    if align == "right":
        return text.rjust(width)
    return text


def _columns(left: Any, right: Any, width: int) -> str:
    left_text = str(left or "")
    right_text = str(right or "")
    gap = max(1, width - len(left_text) - len(right_text))
    return f"{left_text}{' ' * gap}{right_text}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the IoT Box ESC/POS receipt template")
    parser.add_argument("order", type=Path, help="Order JSON file")
    parser.add_argument("--kitchen", action="store_true", help="Render the kitchen ticket variant")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--width", type=int, default=48, help="Text preview width")
    args = parser.parse_args()

    order = json.loads(args.order.read_text(encoding="utf-8"))
    lines = build_lines(order, kitchen=args.kitchen)
    if args.format == "json":
        print(json.dumps({"lines": lines}, ensure_ascii=False, indent=2))
    else:
        print(render_text(lines, width=args.width))


if __name__ == "__main__":
    main()
