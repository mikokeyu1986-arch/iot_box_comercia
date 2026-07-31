from __future__ import annotations

import asyncio
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

os.environ.setdefault("IOT_SSL_VERIFY", "0")


BASE_DIR = Path(__file__).resolve().parent
SOURCE_DIR = BASE_DIR.parent
if (SOURCE_DIR / "app").exists() and str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

DEPENDENCY_FILE_CANDIDATES = (
    BASE_DIR / "pyproject.toml",
    BASE_DIR.parent / "script_bundle" / "pyproject.toml",
)
REQUIRED_MODULES = {
    "uvicorn": "uvicorn[standard]>=0.35.0",
    "fastapi": "fastapi>=0.116.0",
    "pydantic": "pydantic>=2.0.0",
    "websockets": "websockets>=16.0",
    "PIL": "Pillow>=10.0.0",
    "cairosvg": "CairoSVG>=2.7.0",
    "qrcode": "qrcode>=8.0",
}
if os.name == "nt":
    REQUIRED_MODULES["win32api"] = "pywin32>=308"


def _runtime_dir() -> Path:
    return Path(
        os.getenv(
            "IOT_RUNTIME_DIR",
            str(Path(os.getenv("IOT_PORTABLE_DIR", Path(os.sys.executable).resolve().parent)).resolve()),
        )
    )


def _log_dir() -> Path:
    return Path(os.getenv("IOT_LOG_DIR", str(_runtime_dir() / "logs"))).resolve()


def _configure_logging() -> Path:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runtime.log"
    error_log_path = log_dir / "runtime_error.log"
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

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.INFO)

    logging.getLogger(__name__).info("Logging initialized log_dir=%s", log_dir)
    return log_dir


LOG_DIR = _configure_logging()


def _dependency_specs() -> list[str]:
    for candidate in DEPENDENCY_FILE_CANDIDATES:
        if not candidate.exists() or tomllib is None:
            continue
        try:
            with candidate.open("rb") as handle:
                data = tomllib.load(handle)
        except Exception:
            continue
        dependencies = data.get("project", {}).get("dependencies", [])
        if dependencies:
            return [str(item) for item in dependencies]
    return list(dict.fromkeys(REQUIRED_MODULES.values()))


def _missing_dependency_specs() -> list[str]:
    missing_specs: list[str] = []
    for module_name, spec in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module_name) is None:
            missing_specs.append(spec)
    return missing_specs


def _install_missing_dependencies() -> None:
    missing_specs = _missing_dependency_specs()
    if not missing_specs:
        return

    install_specs = _dependency_specs()
    print(f"Missing Python dependencies detected. Installing: {', '.join(missing_specs)}", flush=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *install_specs],
            check=True,
            cwd=os.fspath(BASE_DIR),
            timeout=120,
        )
    except Exception as exc:
        print(f"WARNING: Failed to auto-install dependencies: {exc}", flush=True)
        print(f"Please run manually: {sys.executable} -m pip install {' '.join(install_specs)}", flush=True)


_install_missing_dependencies()

import uvicorn

from app.certificate_manager import ensure_runtime_tls_assets
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


def _runtime_local_url() -> str:
    local_config = config_store.get_local_config()
    return str(local_config.get("local_url") or "").strip()


def _runtime_ssl_engine() -> str:
    local_config = config_store.get_local_config()
    return str(local_config.get("ssl_engine") or "secure_https").strip() or "secure_https"


def _resolve_host_port() -> tuple[str, int]:
    default_host = os.getenv("IOT_HOST", "0.0.0.0")
    default_port = int(os.getenv("IOT_PORT", "8398"))
    local_url = _runtime_local_url()
    if not local_url:
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
    host, port = _resolve_host_port()
    _coerce_https_runtime_config(port)
    ssl_engine = _runtime_ssl_engine()
    kwargs: dict[str, object] = {
        "host": host,
        "port": port,
        "log_level": "info",
        "log_config": None,
        "access_log": True,
    }
    if ssl_engine == "secure_https":
        _seed_runtime_certs()
        try:
            manager = ensure_runtime_tls_assets(
                certificate_manager.certs_dir,
                iot_ip=f"127.0.0.1:{port}",
                p12_password=os.getenv("IOT_P12_PASSWORD", "odoo"),
            )
            kwargs["ssl_keyfile"] = os.fspath(manager.key_path)
            kwargs["ssl_certfile"] = os.fspath(manager.crt_path)
        except Exception as cert_exc:
            logger = logging.getLogger(__name__)
            logger.error("Certificate generation failed: %s", cert_exc)
            logger.warning("Falling back to plain HTTP (no SSL). Certificates can be re-generated later via /api/regenerate-certs")
            print(f"WARNING: Certificate generation failed ({cert_exc}), falling back to HTTP", flush=True)
    logging.getLogger(__name__).info("Starting IoT runtime host=%s port=%s log_dir=%s", host, port, LOG_DIR)
    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _write_fatal_log(exc)
        raise
