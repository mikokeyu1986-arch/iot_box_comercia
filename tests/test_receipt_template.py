import json
import os
import tempfile
import unittest
from pathlib import Path

from app.receipt_builder import build_kitchen_ticket_lines, build_receipt_lines
from app.kitchen_template_store import default_kitchen_template, save_kitchen_template
from app.receipt_template_store import _cell_width, default_template, load_template, save_template, validate_template


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ORDER = json.loads(
    (ROOT / "templates" / "escpos_receipt" / "example_order.json").read_text(encoding="utf-8")
)


class ReceiptTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("IOT_RECEIPT_TEMPLATE_PATH")
        self.previous_kitchen_path = os.environ.get("IOT_KITCHEN_TEMPLATE_PATH")
        os.environ["IOT_RECEIPT_TEMPLATE_PATH"] = str(Path(self.temp_dir.name) / "receipt_template.json")
        os.environ["IOT_KITCHEN_TEMPLATE_PATH"] = str(Path(self.temp_dir.name) / "kitchen_template.json")

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("IOT_RECEIPT_TEMPLATE_PATH", None)
        else:
            os.environ["IOT_RECEIPT_TEMPLATE_PATH"] = self.previous_path
        if self.previous_kitchen_path is None:
            os.environ.pop("IOT_KITCHEN_TEMPLATE_PATH", None)
        else:
            os.environ["IOT_KITCHEN_TEMPLATE_PATH"] = self.previous_kitchen_path
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
        order_label_index = next(
            index for index, line in enumerate(lines)
            if str(line.get("text") or "").startswith("PEDIDO ")
        )
        barcode = lines[order_label_index + 1]
        self.assertEqual(barcode.get("image_kind"), "barcode")
        self.assertEqual(barcode.get("barcode_type"), "Code128")
        self.assertEqual(barcode.get("barcode_value"), SAMPLE_ORDER["pos_reference"])
        self.assertIn("barcode_type=Code128", barcode.get("src", ""))

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
        order_label_index = texts.index("PEDIDO {{ pos_reference }}")
        barcode = lines[order_label_index + 1]
        self.assertEqual(barcode.get("image_kind"), "barcode")
        self.assertEqual(barcode.get("barcode_value"), "{{ pos_reference }}")
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

    def test_kitchen_template_preview_uses_odoo_field_names(self):
        lines = build_kitchen_ticket_lines(
            SAMPLE_ORDER, template=default_kitchen_template(), preview_fields=True,
        )
        texts = [str(line.get("text") or "") for line in lines]
        meta = next(line for line in lines if line.get("type") == "header_meta_line")
        product = next(line for line in lines if line.get("type") == "product_line")

        self.assertEqual(texts[0], "{{ chino_order_type || 'DINE IN' }}")
        self.assertEqual(texts[1], "{{ kitchen_title || changes.title || 'NUEVO' }}")
        self.assertEqual(meta["left_text"], "# {{ tracking_number }}")
        self.assertEqual(meta["right_text"], "MESA {{ table_id.table_number }}")
        self.assertEqual(product["qty"], "{{ course_groups[].items[].qty }}")
        self.assertEqual(product["name"], "{{ course_groups[].items[].full_product_name }}")
        self.assertTrue(any("course_groups[].course_name" in text for text in texts))
        self.assertIn("{{ config.name }}", texts)
        self.assertIn("{{ date_order.time }}", texts)

    def test_saved_kitchen_template_hides_and_reorders_blocks(self):
        template = default_kitchen_template()
        next(block for block in template["blocks"] if block["id"] == "status")["enabled"] = False
        time_block = next(block for block in template["blocks"] if block["id"] == "time")
        template["blocks"].remove(time_block)
        template["blocks"].insert(0, time_block)
        save_kitchen_template(template)

        lines = build_kitchen_ticket_lines(SAMPLE_ORDER)
        texts = [str(line.get("text") or "") for line in lines]

        self.assertEqual(texts[0], "20:30")
        self.assertNotIn("NUEVO", texts)

    def test_kitchen_notification_and_course_sequence_have_no_receipt_columns(self):
        order = {
            **SAMPLE_ORDER,
            "kitchen_title": "CANCELA",
            "course_groups": [
                {
                    "course_name": "1º ENTRANTES",
                    "items": [{
                        "qty": 1,
                        "full_product_name": "Ensalada",
                        "customer_note": "Sin cebolla",
                        "_cancelled": True,
                    }],
                },
                {
                    "course_name": "2º PRINCIPALES",
                    "items": [{"qty": 2, "full_product_name": "Paella"}],
                },
            ],
        }

        lines = build_kitchen_ticket_lines(order, template=default_kitchen_template())
        texts = [str(line.get("text") or "") for line in lines]
        products = [line for line in lines if line.get("type") == "product_line"]

        self.assertEqual(texts[1], "CANCELA")
        self.assertIn("** 1º ENTRANTES **", texts)
        self.assertIn("** 2º PRINCIPALES **", texts)
        self.assertEqual([line["name"] for line in products], ["Ensalada", "Paella"])
        self.assertEqual(products[0]["kitchen_notification"], "CANCELA")
        self.assertFalse(any("Uds." in text or "Producto" in text or "Importe" in text for text in texts))

    def test_kitchen_group_merges_items_and_cancelled_without_duplicates(self):
        order = {
            **SAMPLE_ORDER,
            "course_groups": [],
            "changes": {
                "title": "CANCELA",
                "groupedData": [{
                    "course_name": "1º ENTRANTES",
                    "items": [{"uuid": "line-1", "qty": 1, "full_product_name": "Ensalada"}],
                    "new": [{"uuid": "line-2", "qty": 1, "full_product_name": "Pan"}],
                    "cancelled": [{"uuid": "line-1", "qty": 1, "full_product_name": "Ensalada"}],
                }],
            },
        }

        lines = build_kitchen_ticket_lines(order, template=default_kitchen_template())
        products = [line for line in lines if line.get("type") == "product_line"]

        self.assertEqual([line["name"] for line in products], ["Ensalada", "Pan"])
        self.assertEqual(products[0]["kitchen_notification"], "CANCELA")


if __name__ == "__main__":
    unittest.main()
