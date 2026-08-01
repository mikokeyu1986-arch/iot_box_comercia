import json
import os
import tempfile
import unittest
from pathlib import Path

from app.receipt_builder import build_receipt_lines
from app.receipt_template_store import _cell_width, default_template, load_template, save_template, validate_template


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ORDER = json.loads(
    (ROOT / "templates" / "escpos_receipt" / "example_order.json").read_text(encoding="utf-8")
)


class ReceiptTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("IOT_RECEIPT_TEMPLATE_PATH")
        os.environ["IOT_RECEIPT_TEMPLATE_PATH"] = str(Path(self.temp_dir.name) / "receipt_template.json")

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("IOT_RECEIPT_TEMPLATE_PATH", None)
        else:
            os.environ["IOT_RECEIPT_TEMPLATE_PATH"] = self.previous_path
        self.temp_dir.cleanup()

    def test_default_template_preserves_all_blocks(self):
        lines = build_receipt_lines(SAMPLE_ORDER)
        self.assertTrue(lines)
        self.assertFalse(any("_template_block" in line for line in lines))
        self.assertTrue(any(line.get("text") == "示例餐厅" for line in lines))
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))
        self.assertIn("Uds.", header["text"])
        self.assertIn("Producto", header["text"])
        self.assertIn("Importe", header["text"])
        self.assertEqual(len(header["text"]), 48)

    def test_saved_template_hides_and_reorders_blocks(self):
        template = default_template()
        next(block for block in template["blocks"] if block["id"] == "company")["enabled"] = False
        products = next(block for block in template["blocks"] if block["id"] == "products")
        template["blocks"].remove(products)
        template["blocks"].insert(0, products)

        saved = save_template(template)
        lines = build_receipt_lines(SAMPLE_ORDER)

        self.assertEqual(saved, load_template())
        self.assertIn("receipt-product-row", lines[0].get("classes", []))
        self.assertEqual(_cell_width(lines[0]["text"]), 48)
        self.assertTrue(lines[0]["text"].endswith("17.00 €"))
        self.assertFalse(any(line.get("text") == "示例餐厅" for line in lines))

    def test_unknown_blocks_are_rejected(self):
        template = default_template()
        template["blocks"][0]["id"] = "arbitrary_python"
        with self.assertRaises(ValueError):
            validate_template(template)

    def test_old_template_is_migrated_to_fixed_width_with_product_header(self):
        template = default_template()
        template["paper_width"] = 32
        template["blocks"] = [block for block in template["blocks"] if block["id"] != "product_header"]

        migrated = validate_template(template)
        ids = [block["id"] for block in migrated["blocks"]]

        self.assertEqual(migrated["paper_width"], 48)
        self.assertLess(ids.index("product_header"), ids.index("products"))

    def test_product_header_column_widths_are_editable(self):
        template = default_template()
        header_block = next(block for block in template["blocks"] if block["id"] == "product_header")
        header_block.update({"qty_columns": 8, "amount_columns": 12})

        lines = build_receipt_lines(SAMPLE_ORDER, template=template)
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))

        self.assertEqual(len(header["text"]), 48)
        self.assertEqual(header["text"].index("Producto"), 8)
        self.assertGreaterEqual(header["text"].index("Importe"), 40)

    def test_unused_columns_create_a_safe_gutter_before_amount(self):
        template = default_template()
        header_block = next(block for block in template["blocks"] if block["id"] == "product_header")
        header_block.update({"qty_columns": 6, "product_columns": 20, "amount_columns": 10})

        validated = validate_template(template)
        saved_header = next(block for block in validated["blocks"] if block["id"] == "product_header")
        lines = build_receipt_lines(SAMPLE_ORDER, template=validated)
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))

        self.assertEqual(saved_header["gutter_columns"], 12)
        self.assertEqual(_cell_width(header["text"]), 48)
        self.assertTrue(header["text"].endswith("Importe"))

    def test_native_odoo_discount_line_format(self):
        order = {
            **SAMPLE_ORDER,
            "lines": [{
                "qty": 1,
                "full_product_name": "Producto",
                "price_subtotal_incl": 6.95,
                "price_unit": 13.90,
                "discount": 50,
            }],
            "totalDue": 6.95,
            "amountPaid": 6.95,
        }

        lines = build_receipt_lines(order, template=default_template())
        discount_rows = [
            line["text"].strip()
            for line in lines
            if "receipt-product-discount-row" in line.get("classes", [])
        ]

        self.assertEqual(discount_rows, ["50% de descuento en 13,90 €"])

    def test_editor_preview_uses_field_names_and_total_owns_both_separators(self):
        lines = build_receipt_lines(SAMPLE_ORDER, template=default_template(), preview_fields=True)
        texts = [line.get("text", "") for line in lines]
        total_index = texts.index("TOTAL {{ totalDue }}")

        self.assertIn("{{ company.name }}", texts)
        self.assertIn("{{ config.receipt_footer }}", texts)
        self.assertEqual(texts[total_index - 1], "-" * 48)
        self.assertEqual(texts[total_index + 1], "-" * 48)
        product_row = next(line for line in lines if "receipt-product-row" in line.get("classes", []))
        self.assertIn("qty", product_row["text"])
        self.assertIn("full_product_name", product_row["text"])
        product_rows = [line["text"] for line in lines if "receipt-product-row" in line.get("classes", [])]
        self.assertIn("price_subt", product_rows[0])
        self.assertTrue(any("otal_incl" in row for row in product_rows))
        self.assertTrue(any("discount% de descuento en" in row for row in texts))

    def test_custom_text_and_footer_override_are_rendered(self):
        template = default_template()
        footer = next(block for block in template["blocks"] if block["id"] == "footer")
        footer["content"] = "自定义页脚\n再次感谢"
        template["blocks"].insert(0, {
            "id": "custom_test_text",
            "kind": "text",
            "label": "欢迎文字",
            "text": "欢迎光临",
            "enabled": True,
            "align": "center",
            "bold": True,
            "horizontal_offset": 3,
            "double_size": True,
            "spacing_after": 1,
        })

        lines = build_receipt_lines(SAMPLE_ORDER, template=template)

        self.assertEqual(lines[0].get("text").lstrip(), "欢迎光临")
        self.assertTrue(lines[0].get("text").startswith(" "))
        self.assertEqual(lines[0].get("align"), "left")
        self.assertTrue(lines[0].get("double_width"))
        self.assertTrue(any(line.get("text") == "自定义页脚" for line in lines))
        self.assertFalse(any(line.get("text") == "谢谢惠顾" for line in lines))


if __name__ == "__main__":
    unittest.main()
