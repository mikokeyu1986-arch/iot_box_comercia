"""Certificate generation using only Python standard library.

We use ``ssl`` + ``subprocess`` (openssl if available) to generate self-signed
TLS certificates and PKCS12 bundles.  If the openssl binary is not found we
fall back to the ``cryptography`` package (installed via pip).  If neither is
available uvicorn will raise a clear error at startup.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class CertificateManager:
    def __init__(
        self,
        certs_dir: Path,
        *,
        iot_ip: str,
        p12_password: str = "odoo",
    ) -> None:
        self.certs_dir = certs_dir
        self.iot_ip = iot_ip
        self.p12_password = p12_password

        self.key_path = self.certs_dir / "iotbox.key"
        self.crt_path = self.certs_dir / "iotbox.crt"
        self.p12_path = self.certs_dir / "iotbox.p12"

    def ensure(self) -> None:
        self.certs_dir.mkdir(parents=True, exist_ok=True)
        if self._is_existing_bundle_usable():
            return
        self._clear_existing_bundle()
        self._generate_with_best_available()

    def status(self) -> dict[str, Any]:
        return {
            "crt_ready": self.crt_path.exists(),
            "p12_ready": self.p12_path.exists(),
            "password_hint": self.p12_password,
        }

    # ------------------------------------------------------------------
    # Generation strategy
    # ------------------------------------------------------------------

    def _generate_with_best_available(self) -> None:
        """Try openssl CLI first, then fall back to cryptography package."""
        openssl_bin = self._find_openssl_bin()
        if openssl_bin:
            self._generate_via_openssl(openssl_bin)
            return
        # Fallback: use the cryptography library
        self._generate_via_cryptography()

    def _find_openssl_bin(self) -> str | None:
        candidates = [
            os.getenv("IOT_OPENSSL_BIN", ""),
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files\Git\mingw64\bin\openssl.exe",
            r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
            r"C:\Program Files (x86)\OpenSSL-Win32\bin\openssl.exe",
            r"C:\msys64\usr\bin\openssl.exe",
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        # Search PATH
        which = shutil.which("openssl")
        if which:
            return which
        return None

    # ------------------------------------------------------------------
    # OpenSSL CLI path
    # ------------------------------------------------------------------

    def _generate_via_openssl(self, openssl_bin: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "openssl.cnf"
            self._write_openssl_config(config_path)

            key_tmp = tmp_path / "iotbox.key"
            crt_tmp = tmp_path / "iotbox.crt"
            p12_tmp = tmp_path / "iotbox.p12"

            # Generate key + self-signed cert
            subprocess.run(
                [
                    openssl_bin, "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                    "-keyout", str(key_tmp), "-out", str(crt_tmp),
                    "-days", "3650",
                    "-subj", "/CN=Custom IoT Box",
                    "-extensions", "v3_req",
                    "-config", str(config_path),
                ],
                check=True, capture_output=True, text=True,
            )

            # Convert to PKCS12
            subprocess.run(
                [
                    openssl_bin, "pkcs12", "-export",
                    "-out", str(p12_tmp),
                    "-inkey", str(key_tmp), "-in", str(crt_tmp),
                    "-passout", f"pass:{self.p12_password}",
                    "-name", "Custom IoT Box",
                ],
                check=True, capture_output=True, text=True,
            )

            # Move to final location
            shutil.move(str(key_tmp), str(self.key_path))
            shutil.move(str(crt_tmp), str(self.crt_path))
            shutil.move(str(p12_tmp), str(self.p12_path))

    def _write_openssl_config(self, config_path: Path) -> None:
        san_entries = self._subject_alt_names()
        lines = [
            "[req]",
            "default_bits = 2048",
            "prompt = no",
            "default_md = sha256",
            "distinguished_name = dn",
            "x509_extensions = v3_req",
            "",
            "[dn]",
            "CN = Custom IoT Box",
            "",
            "[v3_req]",
            "subjectAltName = @alt_names",
            "extendedKeyUsage = serverAuth",
            "",
            "[alt_names]",
        ]
        lines.extend(san_entries)
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _subject_alt_names(self) -> list[str]:
        host = self.iot_ip.split(":", 1)[0].strip()
        names: list[str] = [
            "DNS.1 = localhost",
            "IP.1 = 127.0.0.1",
        ]
        if not host:
            return names
        next_dns = 2
        next_ip = 2
        try:
            ipaddress.ip_address(host)
            names.append(f"IP.{next_ip} = {host}")
        except ValueError:
            names.append(f"DNS.{next_dns} = {host}")
        return names

    # ------------------------------------------------------------------
    # cryptography library path (fallback)
    # ------------------------------------------------------------------

    def _generate_via_cryptography(self) -> None:
        """Lazy-import cryptography only when needed."""
        try:
            from cryptography import x509 as _x509
            from cryptography.hazmat.primitives import hashes as _hashes, serialization as _serialization
            from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
            from cryptography.hazmat.primitives.serialization import pkcs12 as _pkcs12
            from cryptography.x509.oid import NameOID as _NameOID
        except ImportError:
            raise RuntimeError(
                "No certificate generation method available. "
                "Install cryptography: pip install cryptography"
            )

        from datetime import datetime, timezone

        key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = _x509.Name([
            _x509.NameAttribute(_NameOID.COMMON_NAME, "Custom IoT Box"),
        ])

        san_list: list[_x509.GeneralName] = [
            _x509.DNSName("localhost"),
            _x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
        host = self.iot_ip.split(":", 1)[0].strip()
        if host:
            try:
                san_list.append(_x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                san_list.append(_x509.DNSName(host))

        now = datetime.now(timezone.utc)
        cert = (
            _x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(_x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now.replace(year=now.year + 10))
            .add_extension(_x509.SubjectAlternativeName(san_list), critical=False)
            .add_extension(
                _x509.ExtendedKeyUsage([_x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(key, _hashes.SHA256())
        )

        self.key_path.write_bytes(
            key.private_bytes(
                encoding=_serialization.Encoding.PEM,
                format=_serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=_serialization.NoEncryption(),
            )
        )
        self.crt_path.write_bytes(cert.public_bytes(_serialization.Encoding.PEM))

        p12_data = _pkcs12.serialize_key_and_certificates(
            name=b"Custom IoT Box", key=key, cert=cert, cas=None,
            encryption_algorithm=_serialization.BestAvailableEncryption(
                self.p12_password.encode("utf-8")
            ),
        )
        self.p12_path.write_bytes(p12_data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_existing_bundle(self) -> None:
        for path in (self.key_path, self.crt_path, self.p12_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def _is_existing_bundle_usable(self) -> bool:
        if not (self.crt_path.exists() and self.key_path.exists() and self.p12_path.exists()):
            return False
        try:
            cert = ssl._ssl._test_decode_cert(os.fspath(self.crt_path))
        except Exception:
            return False
        subject_alt_names = cert.get("subjectAltName", ())
        wanted_host = self.iot_ip.split(":", 1)[0].strip()
        if not wanted_host:
            return True
        for san_type, san_value in subject_alt_names:
            if san_type == "IP Address" and san_value == wanted_host:
                return True
            if san_type == "DNS" and san_value.lower() == wanted_host.lower():
                return True
        return False


def ensure_runtime_tls_assets(
    certs_dir: Path,
    *,
    iot_ip: str,
    p12_password: str = "odoo",
) -> CertificateManager:
    manager = CertificateManager(
        certs_dir,
        iot_ip=iot_ip,
        p12_password=p12_password,
    )
    manager.ensure()
    return manager
