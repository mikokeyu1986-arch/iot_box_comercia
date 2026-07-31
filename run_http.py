from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_HTTP_PORT = os.getenv("IOT_HTTP_PORT", os.getenv("IOT_PORT", "8399"))
os.environ.setdefault("IOT_CONFIG_PATH", str(BASE_DIR / "runtime_config_http.json"))
os.environ.setdefault("IOT_IP", f"127.0.0.1:{_DEFAULT_HTTP_PORT}")

from run_https import (
    LOG_DIR,
    _clear_spool_history,
    _configure_windows_asyncio,
    _write_fatal_log,
)

import uvicorn

from app import main as app_main

app = app_main.app
cloud_bridge = app_main.cloud_bridge
config_store = app_main.config_store


def _runtime_local_url() -> str:
    local_config = config_store.get_local_config()
    return str(local_config.get("local_url") or "").strip()


def _resolve_host_port() -> tuple[str, int]:
    default_host = os.getenv("IOT_HTTP_HOST", os.getenv("IOT_HOST", "0.0.0.0"))
    default_port = int(os.getenv("IOT_HTTP_PORT", os.getenv("IOT_PORT", "8399")))
    local_url = _runtime_local_url()
    if not local_url or not local_url.startswith("http://"):
        return default_host, default_port
    parsed = urlparse(local_url)
    host = parsed.hostname or default_host
    port = parsed.port or default_port
    if host in {"127.0.0.1", "localhost"}:
        host = "0.0.0.0"
    return host, port


def _coerce_http_runtime_config(port: int) -> None:
    local_config = config_store.get_local_config()
    ssl_engine = str(local_config.get("ssl_engine") or "").strip()
    local_url = str(local_config.get("local_url") or "").strip()
    expected_url = f"http://127.0.0.1:{port}"
    updates: dict[str, object] = {}
    if ssl_engine != "plain_http":
        updates["ssl_engine"] = "plain_http"
    if not local_url or local_url.startswith("https://") or local_url != expected_url:
        updates["local_url"] = expected_url
    if updates:
        config_store.update_local_config(**updates)

    app_main.IOT_IP = f"127.0.0.1:{port}"
    cloud_bridge.iot_ip = f"127.0.0.1:{port}"


def main() -> None:
    _configure_windows_asyncio()
    _clear_spool_history()
    host, port = _resolve_host_port()
    _coerce_http_runtime_config(port)
    logging.getLogger(__name__).info("Starting IoT HTTP runtime host=%s port=%s log_dir=%s", host, port, LOG_DIR)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _write_fatal_log(exc)
        raise
