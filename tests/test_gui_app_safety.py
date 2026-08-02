import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GuiAppSafetyTests(unittest.TestCase):
    def test_gui_sources_compile(self):
        for source_path in (ROOT / "gui_app.py", ROOT / "customer_display_app.py"):
            ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    def test_windows_gui_dependencies_are_declared(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"pystray>=', project)
        self.assertIn('"pywebview>=', project)

    def test_launcher_has_no_unreachable_legacy_body(self):
        launcher_lines = [
            line.strip()
            for line in (ROOT / "start_gui.bat").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().upper().startswith("REM ")
        ]
        self.assertEqual(
            launcher_lines,
            ["@echo off", 'wscript.exe "%~dp0start_gui_hidden.vbs"', "exit /b 0"],
        )

    def test_removed_gui_symbols_do_not_return(self):
        source = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_ODOO_URL", source)
        self.assertNotIn("DEFAULT_TOKEN_URL", source)
        self.assertNotIn("_run_customer_display_webview", source)


if __name__ == "__main__":
    unittest.main()
