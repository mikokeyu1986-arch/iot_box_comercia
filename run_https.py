from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import uvicorn

# HTTPS has its own runtime configuration and must not inherit the HTTP 8399
# configuration when both services are available.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("IOT_CONFIG_PATH", os.path.join(BASE_DIR, "runtime_config.json"))
os.environ.setdefault("IOT_SSL_VERIFY", "0")
os.environ.setdefault("IOT_PORT", "8398")

from app.certificate_manager import ensure_runtime_tls_assets  # noqa: E402
from app.main import app, certificate_manager, config_store  # noqa: E402


def _resolve_host_port() -> tuple[str, int]:
    host = os.getenv("IOT_HOST", "0.0.0.0")
    port = int(os.getenv("IOT_PORT", "8398"))
    local_url = str(config_store.get_local_config().get("local_url") or "")
    if local_url.startswith("https://") and not os.getenv("IOT_PORT_OVERRIDE"):
        parsed = urlparse(local_url)
        host = parsed.hostname or host
        port = parsed.port or port
        if host in {"127.0.0.1", "localhost"}:
            host = "0.0.0.0"
    return host, port


def main() -> None:
    host, port = _resolve_host_port()
    config_store.update_local_config(
        ssl_engine="secure_https",
        local_url=f"https://127.0.0.1:{port}",
        service_protocol="https",
    )
    certs = ensure_runtime_tls_assets(
        certificate_manager.certs_dir,
        iot_ip=f"127.0.0.1:{port}",
        p12_password=os.getenv("IOT_P12_PASSWORD", "odoo"),
    )
    logging.getLogger(__name__).info("Starting IoT HTTPS runtime host=%s port=%s", host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_keyfile=os.fspath(certs.key_path),
        ssl_certfile=os.fspath(certs.crt_path),
        log_level="info",
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
