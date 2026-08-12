from __future__ import annotations

import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from time import time

from ..models import Device
from ..printing.common import (
    DEVICE_CACHE_FILE as _DEVICE_CACHE_FILE,
    PRINTER_CACHE_FILE as _PRINTER_CACHE_FILE,
)

try:
    import win32print  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    win32print = None

_logger = logging.getLogger(__name__)

class DeviceDiscoveryMixin:
    def _refresh_devices_if_needed(self) -> None:
        if not self.devices:
            self._refresh_devices(force=True)
            return
        if time() - self._devices_cached_at >= self._device_refresh_interval_seconds:
            self._refresh_devices(force=True)

    def _refresh_devices(self, force: bool = False) -> None:
        if (
            not force
            and self.devices
            and time() - self._devices_cached_at < self._device_refresh_interval_seconds
        ):
            return
        refresh_start = time()
        # Try loading cached devices from disk first (avoids slow enumeration)
        if not self.devices and not force:
            cached = self._load_device_cache()
            if cached:
                self.devices = cached
                self._devices_cached_at = time()
                _logger.info("Device refresh loaded from cache devices=%s", len(cached))
                return
        dynamic_printers = self._discover_printer_devices()
        extra_devices = self._discover_extra_devices()
        dynamic_printers.update(extra_devices)
        self.devices = dynamic_printers
        self._devices_cached_at = time()
        self._persist_printer_binding(dynamic_printers)
        self._save_device_cache(dynamic_printers)
        _logger.info(
            "Device refresh completed devices=%s duration_ms=%.1f",
            len(dynamic_printers),
            (time() - refresh_start) * 1000,
        )

    def _discover_printer_devices(self) -> dict[str, Device]:
        if self._is_windows_native_printing_available():
            printers = self._discover_windows_printer_devices()
        else:
            printers = {
                "printer_main": Device(
                    identifier="printer_main",
                    name="Receipt Printer",
                    type="printer",
                    connection="direct",
                    subtype="receipt_printer",
                )
            }

        network_printers = self._discover_epson_network_printers()
        printers.update(network_printers)
        # On hosts without a native printer queue, make a discovered Epson the
        # default device so generic Odoo print requests use TCP/9100 directly.
        if network_printers and printers["printer_main"].metadata.get("backend") != "windows":
            printers["printer_main"] = next(iter(network_printers.values()))
        return printers

    def _discover_epson_network_printers(self) -> dict[str, Device]:
        """Discover Epson-compatible raw TCP printers exposed on port 9100.

        Port 9100 is the Epson ESC/POS raw-print endpoint.  A TCP connection
        cannot expose a reliable model string, so each reachable endpoint is
        reported as an Epson-compatible network printer rather than claiming a
        specific model.
        """
        config = self.local_config_getter() or {}
        if not bool(config.get("epson_discovery_enabled", True)):
            return {}

        try:
            port = int(config.get("epson_discovery_port", 9100) or 9100)
        except (TypeError, ValueError):
            port = 9100
        if not 1 <= port <= 65535:
            _logger.warning("Invalid Epson discovery port=%s; using 9100", port)
            port = 9100
        timeout = max(0.02, min(1.0, float(config.get("epson_discovery_timeout", 0.12) or 0.12)))
        hosts = self._configured_epson_hosts(config)
        hosts.update(self._local_ipv4_hosts(config))
        if not hosts:
            return {}

        workers = max(1, min(128, int(config.get("epson_discovery_workers", 64) or 64)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="epson-discovery") as executor:
            reachable = [host for host, open_ in zip(hosts, executor.map(
                lambda candidate: self._tcp_port_is_open(candidate, port, timeout), hosts
            )) if open_]

        printers: dict[str, Device] = {}
        for host in sorted(reachable, key=lambda value: tuple(map(int, value.split(".")))):
            identifier = self._sanitize_identifier(f"epson_tcp_{host}")
            printers[identifier] = Device(
                identifier=identifier,
                name=f"Epson Network Printer ({host}:{port})",
                type="printer",
                connection="network",
                subtype="receipt_printer",
                manufacturer="EPSON",
                metadata={
                    "raw_tcp_host": host,
                    "raw_tcp_port": port,
                    "discovery": "tcp/9100",
                    "printer_protocol": "escpos",
                },
            )
        if printers:
            _logger.info("Discovered Epson-compatible TCP printers hosts=%s", ", ".join(reachable))
        return printers

    def _configured_epson_hosts(self, config: dict) -> set[str]:
        raw_hosts = config.get("epson_printer_hosts") or []
        if isinstance(raw_hosts, str):
            raw_hosts = re.split(r"[\s,;]+", raw_hosts)
        if not isinstance(raw_hosts, list):
            return set()
        return {str(host).strip() for host in raw_hosts if self._is_ipv4_address(str(host).strip())}

    def _local_ipv4_hosts(self, config: dict) -> set[str]:
        """Return the configured LANs, or the active IPv4 /24 as a safe default."""
        raw_subnets = config.get("epson_discovery_subnets") or []
        if isinstance(raw_subnets, str):
            raw_subnets = re.split(r"[\s,;]+", raw_subnets)
        networks = []
        if isinstance(raw_subnets, list):
            for subnet in raw_subnets:
                try:
                    networks.append(ipaddress.ip_network(str(subnet).strip(), strict=False))
                except ValueError:
                    _logger.warning("Ignoring invalid Epson discovery subnet=%r", subnet)
        if not networks:
            local_ip = self._primary_local_ipv4()
            if local_ip:
                networks.append(ipaddress.ip_network(f"{local_ip}/24", strict=False))

        # A malformed wide network must never turn a printer refresh into a
        # large port scan.  Explicitly split it into at most 254 candidates.
        hosts: set[str] = set()
        for network in networks:
            if network.version != 4:
                continue
            for host in islice(network.hosts(), 254):
                hosts.add(str(host))
        return hosts

    def _primary_local_ipv4(self) -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                address = sock.getsockname()[0]
        except OSError:
            return None
        return address if self._is_ipv4_address(address) and not address.startswith("127.") else None

    @staticmethod
    def _is_ipv4_address(value: str) -> bool:
        try:
            return ipaddress.ip_address(value).version == 4
        except ValueError:
            return False

    @staticmethod
    def _tcp_port_is_open(host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _discover_windows_printer_devices(self) -> dict[str, Device]:
        printers: dict[str, Device] = {}
        queues = self._windows_printer_queues()
        default_queue = self._windows_default_queue(queues)
        primary_queue = self._preferred_printer_queue(queues, default_queue)
        if not queues:
            printers["printer_main"] = Device(
                identifier="printer_main",
                name="Receipt Printer",
                type="printer",
                connection="direct",
                subtype="receipt_printer",
                metadata={"backend": "windows"},
            )
            return printers

        used_identifiers: set[str] = set()
        for index, queue in enumerate(queues):
            identifier = self._sanitize_identifier(f"printer_{queue}")
            if identifier in used_identifiers:
                identifier = self._sanitize_identifier(f"{identifier}_{index + 1}")
            used_identifiers.add(identifier)
            printers[identifier] = Device(
                identifier=identifier,
                name=queue,
                type="printer",
                connection="direct",
                subtype="receipt_printer",
                metadata={
                    "windows_printer": queue,
                    "is_default": queue == default_queue,
                    "is_primary_binding": queue == primary_queue,
                    "backend": "windows",
                },
            )
            if queue == primary_queue or (primary_queue is None and index == 0):
                printers["printer_main"] = Device(
                    identifier="printer_main",
                    name=queue,
                    type="printer",
                    connection="direct",
                    subtype="receipt_printer",
                    metadata={
                        "windows_printer": queue,
                        "is_default": queue == default_queue,
                        "is_primary_binding": True,
                        "backend": "windows",
                    },
                )
        return printers

    def _discover_extra_devices(self) -> dict[str, Device]:
        devices: dict[str, Device] = {}
        config = self.local_config_getter() or {}

        scale_port = str(config.get("scale_port") or "").strip()
        if scale_port:
            scale_brand = str(config.get("scale_brand") or "zfoc")
            devices["scale_main"] = Device(
                identifier="scale_main",
                name=f"电子秤 ({scale_port})",
                type="scale",
                connection="serial",
                # Odoo iot.device.subtype only accepts printer subtypes; keep the
                # brand in manufacturer so /iot/setup can register the scale.
                subtype="",
                manufacturer=scale_brand.upper(),
                status="connected",
                metadata={
                    "port": scale_port,
                    "baudrate": int(config.get("scale_baudrate") or 9600),
                    "brand": scale_brand,
                },
            )

        return devices

    def _configured_printer_identifier(self) -> str:
        config = self.local_config_getter() or {}
        return str(config.get("printer_identifier") or "").strip()

    def _configured_primary_printer_queue(self) -> str:
        config = self.local_config_getter() or {}
        return str(config.get("primary_printer_queue") or "").strip()

    def _configured_enabled_printer_queues(self) -> list[str]:
        config = self.local_config_getter() or {}
        raw_value = config.get("enabled_printer_queues") or []
        if isinstance(raw_value, str):
            candidates = [item.strip() for item in raw_value.splitlines()]
        elif isinstance(raw_value, list):
            candidates = [str(item).strip() for item in raw_value]
        else:
            candidates = []
        enabled: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in enabled:
                enabled.append(candidate)
        return enabled

    def _preferred_printer_queue(self, queues: list[str], default_queue: str | None) -> str | None:
        configured_queue = self._configured_primary_printer_queue()
        if configured_queue and configured_queue in queues:
            return configured_queue
        if default_queue and default_queue in queues:
            return default_queue
        return queues[0] if queues else None

    def _filter_allowed_printer_queues(self, queues: list[str]) -> list[str]:
        enabled_queues = self._configured_enabled_printer_queues()
        if enabled_queues:
            return [queue for queue in queues if queue in enabled_queues]
        if self._is_windows_native_printing_available():
            return [queue for queue in queues if not self._is_virtual_windows_printer(queue)]
        return queues

    def _persist_printer_binding(self, printers: dict[str, Device]) -> None:
        if not printers or self.local_config_updater is None:
            return
        configured_identifier = self._configured_printer_identifier()
        configured_queue = self._configured_primary_printer_queue()

        next_identifier = configured_identifier
        next_queue = configured_queue

        if configured_identifier and configured_identifier in printers:
            return
        else:
            primary_device = printers.get("printer_main")
            if primary_device:
                next_identifier = primary_device.identifier
                next_queue = self._printer_queue_name(primary_device) or configured_queue

        if not next_identifier or not next_queue:
            return
        if next_identifier == configured_identifier and next_queue == configured_queue:
            return
        try:
            self.local_config_updater(
                printer_identifier=next_identifier,
                primary_printer_queue=next_queue,
            )
        except Exception:
            _logger.exception("Failed to persist printer binding identifier=%s queue=%s", next_identifier, next_queue)

    def _printer_queue_name(self, device: Device) -> str:
        return str(
            device.metadata.get("windows_printer")
            or ""
        ).strip()

    def _windows_printer_queues(self) -> list[str]:
        # win32print.EnumPrinters is fast (~1ms). PowerShell Get-Printer is
        # very slow (~500ms-3s) and should NEVER be called in the hot path.
        # We rely solely on win32print for enumeration and only fall back
        # to the disk cache (which was saved from a previous PowerShell run)
        # if win32print returns nothing.
        queues: list[str] = []
        if self._is_windows_native_printing_available():
            try:
                flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                printers = win32print.EnumPrinters(flags)
            except Exception:
                printers = []

            for entry in printers:
                printer_name = ""
                if isinstance(entry, tuple) and len(entry) >= 3:
                    printer_name = str(entry[2] or "").strip()
                if printer_name and printer_name not in queues:
                    queues.append(printer_name)

        # If win32print returned nothing (unlikely), fall back to disk cache
        if not queues:
            cached = self._powershell_queues_cache if self._powershell_queues_cache else self._load_printer_cache()
            if cached:
                for printer_name in cached:
                    if printer_name and printer_name not in queues:
                        queues.append(printer_name)

        return self._filter_allowed_printer_queues(queues)

    def _powershell_windows_printer_queues(self) -> list[str]:
        # No longer called in the hot path. Kept for background refresh only.
        if os.name != "nt":
            return []
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-Printer | Select-Object -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []

        queues: list[str] = []
        for line in proc.stdout.splitlines():
            printer_name = str(line or "").strip()
            if printer_name and printer_name not in queues:
                queues.append(printer_name)
        # Save to disk cache so it survives restarts
        self._save_printer_cache(queues)
        return queues

    def _printer_cache_path(self) -> Path:
        return self.spool_dir / _PRINTER_CACHE_FILE

    def _load_printer_cache(self) -> list[str] | None:
        cache_path = self._printer_cache_path()
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, list) and all(isinstance(x, str) for x in data):
                    _logger.debug("Loaded printer cache from %s queues=%s", cache_path, len(data))
                    return data
        except Exception:
            pass
        return None

    def _save_printer_cache(self, queues: list[str]) -> None:
        if not queues:
            return
        cache_path = self._printer_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(queues, ensure_ascii=False), encoding="utf-8")
            _logger.debug("Saved printer cache to %s queues=%s", cache_path, len(queues))
        except Exception:
            pass

    def _device_cache_path(self) -> Path:
        return self.spool_dir / _DEVICE_CACHE_FILE

    def _load_device_cache(self) -> dict[str, Device] | None:
        cache_path = self._device_cache_path()
        try:
            if cache_path.exists():
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    devices = {}
                    for key, value in raw.items():
                        if isinstance(value, dict):
                            devices[key] = Device.from_dict(value)
                    if devices:
                        _logger.info("Loaded device cache from %s devices=%s", cache_path, len(devices))
                        return devices
        except Exception:
            pass
        return None

    def _save_device_cache(self, devices: dict[str, Device]) -> None:
        if not devices:
            return
        cache_path = self._device_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            raw = {key: vars(device) for key, device in devices.items()}
            cache_path.write_text(json.dumps(raw, ensure_ascii=False, default=str), encoding="utf-8")
            _logger.debug("Saved device cache to %s devices=%s", cache_path, len(devices))
        except Exception:
            pass

    def _windows_default_queue(self, queues: list[str]) -> str | None:
        if not queues or not self._is_windows_native_printing_available():
            return None
        try:
            default_queue = str(win32print.GetDefaultPrinter() or "").strip()
        except Exception:
            default_queue = ""
        if default_queue in queues and not self._is_virtual_windows_printer(default_queue):
            return default_queue
        for queue in queues:
            if not self._is_virtual_windows_printer(queue):
                return queue
        return default_queue if default_queue in queues else None

    def _sanitize_identifier(self, raw: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
        return cleaned or "printer_extra"

    def _detect_workspace_root(self) -> Path:
        current = Path(__file__).resolve()
        for candidate in [current, *current.parents]:
            if (candidate / "custom_addons" / "iot_box_comercia").is_dir():
                return candidate
            if (candidate / ".venv").exists() and (candidate / "instances").exists():
                return candidate
        return current.parents[3]

    def _ensure_fallback_site_packages(self) -> None:
        if self._fallback_site_packages_added:
            return
        fallback_paths = [
            self._workspace_root / ".venv" / "Lib" / "site-packages",
            self._workspace_root / ".venv" / "lib" / "site-packages",
        ]
        for path in fallback_paths:
            if path.exists():
                path_text = str(path)
                if path_text not in sys.path:
                    sys.path.append(path_text)
        self._fallback_site_packages_added = True

    def _import_optional_module(self, module_name: str):
        try:
            return __import__(module_name, fromlist=["*"])
        except ImportError:
            self._ensure_fallback_site_packages()
            try:
                return __import__(module_name, fromlist=["*"])
            except ImportError:
                return None

    def _is_virtual_windows_printer(self, printer_name: str) -> bool:
        normalized = printer_name.strip().lower()
        virtual_markers = (
            "microsoft print to pdf",
            "pdf",
            "xps",
            "onenote",
            "fax",
            "anydesk",
        )
        return any(marker in normalized for marker in virtual_markers)
