from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
import zipfile

from app.config_store import ConfigStore
from app.certificate_manager import CertificateManager
from app.updater import UpdateManager, VersionInfo


class RuntimeSafetyTests(unittest.TestCase):
    def test_p12_password_is_random_and_persisted_per_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            first = CertificateManager(Path(directory), iot_ip="127.0.0.1:8398")
            self.assertTrue(first._ensure_p12_password())
            self.assertGreaterEqual(len(first.p12_password), 20)

            second = CertificateManager(Path(directory), iot_ip="127.0.0.1:8398")
            self.assertFalse(second._ensure_p12_password())
            self.assertEqual(second.p12_password, first.p12_password)

    def test_certificate_is_reused_then_renewed_for_changed_lan_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            certs = Path(directory)
            first = CertificateManager(certs, iot_ip="192.168.1.20:8398")
            first.ensure()
            original_certificate = first.crt_path.read_bytes()

            same_ip = CertificateManager(certs, iot_ip="192.168.1.20:8398")
            same_ip.ensure()
            self.assertEqual(same_ip.crt_path.read_bytes(), original_certificate)

            changed_ip = CertificateManager(certs, iot_ip="192.168.1.21:8398")
            changed_ip.ensure()
            self.assertNotEqual(changed_ip.crt_path.read_bytes(), original_certificate)
            decoded = ssl._ssl._test_decode_cert(os.fspath(changed_ip.crt_path))
            self.assertIn(
                ("IP Address", "192.168.1.21"),
                decoded.get("subjectAltName", ()),
            )

    def test_public_connection_never_exposes_pairing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps({
                "server_connection": {
                    "connected": True,
                    "url": "https://odoo.example",
                    "token": "secret-token",
                    "db_uuid": "db-uuid",
                }
            }), encoding="utf-8")
            store = ConfigStore(path)

            self.assertEqual(store.get_connection()["token"], "secret-token")
            self.assertNotIn("token", store.get_public_connection())

    def test_updater_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.zip"
            destination = root / "extract"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "unsafe")

            manager = UpdateManager("1.0", root / "application")
            with self.assertRaisesRegex(ValueError, "越界路径"):
                manager._extract(archive, destination)
            self.assertFalse((root / "outside.txt").exists())

    def test_updater_extracts_normal_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "normal.zip"
            destination = root / "extract"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("release/README.txt", "ok")

            manager = UpdateManager("1.0", root / "application")
            extracted = manager._extract(archive, destination)
            self.assertEqual(extracted, destination / "release")
            self.assertEqual((extracted / "README.txt").read_text(encoding="utf-8"), "ok")

    def test_updater_rejects_unsigned_manifest_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = UpdateManager("1.0", Path(directory))
            version = VersionInfo({
                "version": "2.0",
                "download_url": "https://example.invalid/release.zip",
                "checksum": "",
            })
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                manager.download_update(version)

    def test_backup_collection_prunes_excluded_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            (root / "app" / "main.py").write_text("ok", encoding="utf-8")
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "objects" / "secret").write_text("no", encoding="utf-8")
            (root / "spool").mkdir()
            (root / "spool" / "receipt.png").write_bytes(b"no")

            files = UpdateManager("1.0", root)._collect_source_files()
            self.assertEqual(files, [(root / "app" / "main.py").resolve()])


if __name__ == "__main__":
    unittest.main()
