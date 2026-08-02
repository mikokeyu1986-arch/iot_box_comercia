from __future__ import annotations

import asyncio
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import traceback
from urllib.parse import urlparse

os.environ.setdefault("IOT_SSL_VERIFY", "1")


BASE_DIR = Path(__file__).resolve().parent
SPOOL_DIR = BASE_DIR / "spool"
REQUIRED_MODULES = {
    "uvicorn": "uvicorn[standard]>=0.35.0",
    "fastapi": "fastapi>=0.116.0",
    "pydantic": "pydantic>=2.0.0",
    "websockets": "websockets>=15.0,<16.0",
    "PIL": "Pillow>=10.0.0",
    "cairosvg": "CairoSVG>=2.7.0",
    "qrcode": "qrcode>=8.0",
    "serial": "pyserial>=3.5",
}
if os.name == "nt":
    REQUIRED_MODULES["win32api"] = "pywin32>=308"


def _runtime_dir() -> Path:
    return Path(
        os.getenv(
            "IOT_RUNTIME_DIR",
            str(Path(os.getenv("IOT_PORTABLE_DIR", BASE_DIR)).resolve()),
        )
    )


def _log_dir() -> Path:
    return Path(os.getenv("IOT_LOG_DIR", str(_runtime_dir() / "logs"))).resolve()


def _configure_logging() -> Path:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runtime.log"
    error_log_path = log_dir / "runtime_error.log"
    dev_log_path = log_dir / "dev_debug.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=int(os.getenv("IOT_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
        backupCount=int(os.getenv("IOT_LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)

    error_handler = RotatingFileHandler(
        error_log_path,
        maxBytes=int(os.getenv("IOT_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
        backupCount=int(os.getenv("IOT_LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.WARNING)
    root.addHandler(error_handler)

    dev_handler = RotatingFileHandler(
        dev_log_path,
        maxBytes=int(os.getenv("IOT_DEV_LOG_MAX_BYTES", str(20 * 1024 * 1024))),
        backupCount=int(os.getenv("IOT_DEV_LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    dev_handler.setFormatter(formatter)
    dev_handler.setLevel(logging.INFO)
    dev_logger = logging.getLogger("dev")
    dev_logger.handlers.clear()
    dev_logger.propagate = False
    dev_logger.setLevel(logging.INFO)
    dev_logger.addHandler(dev_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.INFO)

    logging.getLogger(__name__).info("Logging initialized log_dir=%s dev_log=%s", log_dir, dev_log_path)
    return log_dir


LOG_DIR = _configure_logging()


def _missing_dependency_specs() -> list[str]:
    missing_specs: list[str] = []
    for module_name, spec in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module_name) is None:
            missing_specs.append(spec)
    return missing_specs


def _require_runtime_dependencies() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    missing_specs = _missing_dependency_specs()
    if not missing_specs:
        return
    missing = ", ".join(missing_specs)
    raise RuntimeError(
        f"Missing Python dependencies: {missing}. "
        f"Install them with: {sys.executable} -m pip install -e {BASE_DIR}"
    )


_require_runtime_dependencies()

import uvicorn

from app.main import app, certificate_manager, config_store


def _write_fatal_log(exc: BaseException) -> None:
    try:
        log_path = _log_dir() / "fatal_startup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
            handle.write("\n")
    except Exception:
        pass


def _seed_runtime_certs() -> None:
    bundle_certs_dir = BASE_DIR / "certs"
    runtime_certs_dir = certificate_manager.certs_dir
    if not bundle_certs_dir.exists():
        return
    runtime_certs_dir.mkdir(parents=True, exist_ok=True)
    for name in ("iotbox.key", "iotbox.crt", "iotbox.p12", "openssl.cnf"):
        source = bundle_certs_dir / name
        target = runtime_certs_dir / name
        if not source.exists() or target.exists():
            continue
        try:
            shutil.copy2(source, target)
        except OSError:
            continue


def _clear_spool_history() -> None:
    if not SPOOL_DIR.exists():
        return
    for child in SPOOL_DIR.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            continue


def _runtime_local_url() -> str:
    local_config = config_store.get_local_config()
    return str(local_config.get("local_url") or "").strip()


def _runtime_ssl_engine() -> str:
    local_config = config_store.get_local_config()
    return str(local_config.get("ssl_engine") or "secure_https").strip() or "secure_https"


def _resolve_host_port() -> tuple[str, int]:
    default_host = os.getenv("IOT_HOST", "0.0.0.0")
    default_port = int(os.getenv("IOT_PORT", "8398"))
    if os.getenv("IOT_PORT"):
        return default_host, default_port
    local_url = _runtime_local_url()
    if not local_url or not local_url.startswith("https://"):
        return default_host, default_port
    parsed = urlparse(local_url)
    host = parsed.hostname or default_host
    port = parsed.port or default_port
    if host in {"127.0.0.1", "localhost"}:
        host = "0.0.0.0"
    return host, port


def _coerce_https_runtime_config(port: int) -> None:
    local_config = config_store.get_local_config()
    ssl_engine = str(local_config.get("ssl_engine") or "").strip()
    local_url = str(local_config.get("local_url") or "").strip()
    expected_url = f"https://127.0.0.1:{port}"
    updates: dict[str, object] = {}
    if ssl_engine != "secure_https":
        updates["ssl_engine"] = "secure_https"
    if not local_url or local_url.startswith("http://") or local_url != expected_url:
        updates["local_url"] = expected_url
    if updates:
        config_store.update_local_config(**updates)


def _configure_windows_asyncio() -> None:
    if os.name != "nt":
        return
    # Python >=3.14 deprecates WindowsSelectorEventLoopPolicy and
    # asyncio.set_event_loop_policy. The ProactorEventLoop is the default
    # on Windows since Python 3.8 and works fine with uvicorn, so we
    # simply skip the selector override on newer Python versions.
    if sys.version_info >= (3, 14):
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass


def main() -> None:
    _configure_windows_asyncio()
    _clear_spool_history()
    host, port = _resolve_host_port()
    _coerce_https_runtime_config(port)
    ssl_engine = _runtime_ssl_engine()
    kwargs: dict[str, object] = {
        "host": host,
        "port": port,
        "log_level": "info",
        "log_config": None,
        "access_log": os.getenv("IOT_ACCESS_LOG", "0").strip().lower() in {"1", "true", "yes", "on"},
        "timeout_keep_alive": max(5, int(os.getenv("IOT_HTTP_KEEP_ALIVE_SECONDS", "30"))),
        "backlog": max(128, int(os.getenv("IOT_HTTP_BACKLOG", "2048"))),
    }
    if ssl_engine == "secure_https":
        _seed_runtime_certs()
        try:
            # Use the same manager and LAN identity as the FastAPI runtime.
            # Generating a separate 127.0.0.1 certificate here caused the app
            # startup hook to replace it after Uvicorn had already loaded it.
            certificate_manager.ensure()
            kwargs["ssl_keyfile"] = os.fspath(certificate_manager.key_path)
            kwargs["ssl_certfile"] = os.fspath(certificate_manager.crt_path)
        except Exception as cert_exc:
            logger = logging.getLogger(__name__)
            logger.error("Certificate generation failed: %s", cert_exc)
            # Never serve plain HTTP on the advertised HTTPS port. That makes
            # clients retry TLS until their request times out and looks like a
            # very slow print job.
            raise RuntimeError("HTTPS certificate preparation failed") from cert_exc
    logging.getLogger(__name__).info("Starting IoT runtime host=%s port=%s log_dir=%s", host, port, LOG_DIR)
    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _write_fatal_log(exc)
        raise
