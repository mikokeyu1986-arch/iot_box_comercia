from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ROOT / "app" / "device_manager.py",
    ROOT / "app" / "devices" / "discovery.py",
    ROOT / "app" / "receipts" / "processing.py",
    ROOT / "app" / "receipts" / "structured.py",
    ROOT / "app" / "printing" / "network_printer.py",
    ROOT / "app" / "printing" / "windows_printer.py",
    ROOT / "app" / "printing" / "barcode.py",
    ROOT / "app" / "printing" / "escpos.py",
    ROOT / "app" / "printing" / "image_renderer.py",
    ROOT / "app" / "printing" / "normalization.py",
    ROOT / "app" / "printing" / "product_parser.py",
    ROOT / "app" / "printing" / "receipt_metadata.py",
    ROOT / "app" / "printing" / "section_consumers.py",
    ROOT / "app" / "printing" / "text_layout.py",
]


class DeviceManagerArchitectureTests(unittest.TestCase):
    def test_device_manager_remains_a_small_orchestrator(self):
        manager = ROOT / "app" / "device_manager.py"
        self.assertLessEqual(len(manager.read_text(encoding="utf-8").splitlines()), 700)

    def test_mixin_methods_are_unique_and_core_contract_is_present(self):
        owners: dict[str, list[str]] = defaultdict(list)
        for path in MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
                for method in class_node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owners[method.name].append(f"{path.name}:{method.lineno}")

        duplicates = {name: locations for name, locations in owners.items() if len(locations) > 1}
        self.assertEqual(duplicates, {})
        required = {
            "execute",
            "_process_receipt_escpos",
            "_build_structured_receipt_lines",
            "_build_escpos_bytes",
            "_build_kitchen_escpos_bytes",
            "_send_raw_to_printer",
            "_send_raw_to_windows_printer",
            "_build_escpos_image",
            "_render_escpos_lines",
            "_encode_code128_values",
        }
        self.assertEqual(required - owners.keys(), set())

    def test_templated_receipts_skip_legacy_whitespace_normalization(self):
        path = ROOT / "app" / "receipts" / "processing.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_build_escpos_bytes"
        ]
        self.assertEqual(len(calls), 1)
        keyword = next(
            (item for item in calls[0].keywords if item.arg == "normalize_lines"),
            None,
        )
        self.assertIsNotNone(keyword)
        self.assertIsInstance(keyword.value, ast.UnaryOp)
        self.assertIsInstance(keyword.value.op, ast.Not)
        self.assertIsInstance(keyword.value.operand, ast.Name)
        self.assertEqual(keyword.value.operand.id, "skip_normalize")


if __name__ == "__main__":
    unittest.main()
