# ESC/POS 小票模板

这里把项目现有的小票构建器作为一个独立入口暴露出来，适合修改样例数据、预览版式，或导出给 ESC/POS 打印器使用的行结构。运行模板不需要 `device_manager.pyc`，也不会连接真实打印机。

## 使用

在项目根目录运行：

```bash
python3 templates/escpos_receipt/receipt_template.py \
  templates/escpos_receipt/example_order.json
```

导出项目打印器所用的 JSON 行结构：

```bash
python3 templates/escpos_receipt/receipt_template.py \
  templates/escpos_receipt/example_order.json \
  --format json
```

厨房小票：

```bash
python3 templates/escpos_receipt/receipt_template.py \
  templates/escpos_receipt/example_order.json \
  --kitchen
```

版式的实际构建逻辑仍位于 `app/receipt_builder.py`，因此模板预览与项目当前小票保持一致。
