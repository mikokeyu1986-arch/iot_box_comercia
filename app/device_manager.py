from __future__ import annotations

import asyncio
from base64 import b64decode
import hashlib
import json
import logging
from io import BytesIO
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time as time_module
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from time import time
from typing import Any, Callable
import unicodedata
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urljoin, urlparse
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFont, ImageOps
from .dev_logger import dev_log, summarize_action
from .event_bus import EventBus
from .models import Device, IoTEvent
from .receipt_builder import build_lines

try:
    import win32print  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency for Windows only
    win32print = None

try:
    import win32con  # type: ignore[import-not-found]
    import win32ui  # type: ignore[import-not-found]
    from PIL import ImageWin
except ImportError:  # pragma: no cover - optional dependency for Windows only
    win32con = None
    win32ui = None
    ImageWin = None

# PowerShell Get-Printer can be very slow on some machines (~500ms-5s).
# We cache the result to a file so it survives restarts.
_PRINTER_CACHE_FILE = "printer_cache.json"
_DEVICE_CACHE_FILE = "device_cache.json"


_logger = logging.getLogger(__name__)

_CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212",
    "221213", "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221",
    "223211", "221132", "221231", "213212", "223112", "312131", "311222", "321122", "321221",
    "312212", "322112", "322211", "212123", "212321", "232121", "111323", "131123", "131321",
    "112313", "132113", "132311", "211313", "231113", "231311", "112133", "112331", "132131",
    "113123", "113321", "133121", "313121", "211331", "231131", "213113", "213311", "213131",
    "311123", "311321", "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111", "111242",
    "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311",
    "113141", "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]


def _perf_log(message: str) -> None:
    if os.getenv("IOT_VERBOSE_PERF_LOGS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    print(message, flush=True)


class DeviceManager:
    def __init__(
        self,
        event_bus: EventBus,
        spool_dir: Path,
        local_config_getter: Callable[[], dict[str, Any]] | None = None,
        local_config_updater: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.spool_dir = spool_dir
        self.local_config_getter = local_config_getter or (lambda: {})
        self.local_config_updater = local_config_updater
        self.devices: dict[str, Device] = {}
        self._workspace_root = self._detect_workspace_root()
        self.resource_dir = Path(os.getenv("IOT_RESOURCE_DIR", str(self._workspace_root)))
        self._fallback_site_packages_added = False
        self._devices_cached_at = 0.0
        self._device_refresh_interval_seconds = max(
            0.5, float(os.getenv("IOT_DEVICE_REFRESH_INTERVAL_SECONDS", "30"))
        )
        self._printer_action_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._printer_action_tasks: dict[str, asyncio.Task[None]] = {}
        self._printer_action_queue_max_size = max(
            1, int(os.getenv("IOT_PRINTER_ACTION_QUEUE_MAX_SIZE", "200"))
        )
        self._image_fetch_cache: dict[str, bytes] = {}
        self._escpos_raster_cache: dict[str, bytes] = {}
        self._powershell_queues_cache: list[str] | None = self._load_printer_cache()
        self._runtime_logo_cache_buster = str(int(time()))
        # Ensure spool directory exists for cache files
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_devices()

    async def startup(self) -> None:
        self._clear_runtime_image_caches()
        self._runtime_logo_cache_buster = str(int(time()))

    async def shutdown(self) -> None:
        printer_tasks = list(self._printer_action_tasks.values())
        self._printer_action_tasks.clear()
        self._printer_action_queues.clear()
        for task in printer_tasks:
            task.cancel()
        if printer_tasks:
            await asyncio.gather(*printer_tasks, return_exceptions=True)

    def refresh_local_hardware(self) -> None:
        self._refresh_devices(force=True)

    def device_list(self) -> list[dict[str, Any]]:
        self._refresh_devices()
        return [
            {
                # Standard Odoo IoT Box field names (with device_ prefix)
                "device_identifier": d.identifier,
                "device_name": d.name,
                "device_type": d.type,
                "device_connection": d.connection,
                "device_subtype": d.subtype,
                "device_manufacturer": d.manufacturer,
                # Legacy/compat field names (without prefix)
                "identifier": d.identifier,
                "name": d.name,
                "type": d.type,
                "connection": d.connection,
                "subtype": d.subtype,
                "manufacturer": d.manufacturer,
                "status": d.status,
                "metadata": d.metadata,
            }
            for d in self.devices.values()
        ]

    def as_odoo_devices_payload(self) -> dict[str, dict[str, Any]]:
        self._refresh_devices()
        return {
            d.identifier: {
                "name": d.name,
                "type": d.type,
                "connection": d.connection,
                "manufacturer": d.manufacturer,
                "subtype": d.subtype,
                "device_identifier": d.identifier,
                "device_name": d.name,
                "device_type": d.type,
                "device_connection": d.connection,
                "device_subtype": d.subtype,
            }
            for d in self.devices.values()
        }

    async def _queue_printer_action(
        self,
        owner: str,
        device: Device,
        data: dict[str, Any],
        handler_name: str,
    ) -> bool:
        queue = self._ensure_printer_action_queue(device.identifier)
        queued_at = time()
        queue_size = queue.qsize()
        action = str(data.get("action") or handler_name.lstrip("_"))
        if queue.full():
            _logger.error(
                "Printer action queue full owner=%s device=%s action=%s queue_size=%s max_size=%s",
                owner,
                device.identifier,
                action,
                queue_size,
                self._printer_action_queue_max_size,
            )
            dev_log(
                "printer_queue_full",
                owner=owner,
                device_identifier=device.identifier,
                printer=self._printer_name(device),
                action=action,
                queue_size=queue_size,
                max_size=self._printer_action_queue_max_size,
                action_summary=summarize_action(data),
            )
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message="ERROR_QUEUE_FULL",
                    result={"printer": self._printer_name(device), "mode": action},
                )
            )
            return True

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        await queue.put(
            {
                "owner": owner,
                "device": device,
                "data": dict(data),
                "handler_name": handler_name,
                "future": future,
                "queued_at": queued_at,
            }
        )
        _logger.info(
            "Printer action enqueued owner=%s device=%s action=%s queue_before=%s queue_after=%s",
            owner,
            device.identifier,
            action,
            queue_size,
            queue.qsize(),
        )
        dev_log(
            "printer_action_enqueued",
            owner=owner,
            device_identifier=device.identifier,
            printer=self._printer_name(device),
            action=action,
            queue_before=queue_size,
            queue_after=queue.qsize(),
            action_summary=summarize_action(data),
        )
        return await future

    def _ensure_printer_action_queue(self, device_identifier: str) -> asyncio.Queue[dict[str, Any]]:
        queue = self._printer_action_queues.get(device_identifier)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._printer_action_queue_max_size)
            self._printer_action_queues[device_identifier] = queue
        task = self._printer_action_tasks.get(device_identifier)
        if task is None or task.done():
            task = asyncio.create_task(self._printer_action_worker(device_identifier, queue))
            self._printer_action_tasks[device_identifier] = task
            _logger.info(
                "Printer action worker started device=%s max_queue_size=%s",
                device_identifier,
                self._printer_action_queue_max_size,
            )
        return queue

    async def _printer_action_worker(
        self,
        device_identifier: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        while True:
            job = await queue.get()
            owner = str(job.get("owner") or "")
            device = job.get("device")
            data = job.get("data") if isinstance(job.get("data"), dict) else {}
            handler_name = str(job.get("handler_name") or "")
            future = job.get("future")
            queued_at = float(job.get("queued_at") or time())
            started_at = time()
            action = str(data.get("action") or handler_name.lstrip("_"))
            try:
                _logger.info(
                    "Printer action start owner=%s device=%s action=%s queue_wait_ms=%.1f remaining_queue=%s",
                    owner,
                    device_identifier,
                    action,
                    (started_at - queued_at) * 1000,
                    queue.qsize(),
                )
                dev_log(
                    "printer_action_start",
                    owner=owner,
                    device_identifier=device_identifier,
                    action=action,
                    queue_wait_ms=round((started_at - queued_at) * 1000, 1),
                    remaining_queue=queue.qsize(),
                    action_summary=summarize_action(data),
                )
                if not isinstance(device, Device):
                    raise RuntimeError("queued printer device is invalid")
                handler = getattr(self, handler_name)
                result = await handler(owner, device, data)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_result(bool(result))
                _logger.info(
                    "Printer action done owner=%s device=%s action=%s result=%s duration_ms=%.1f remaining_queue=%s",
                    owner,
                    device_identifier,
                    action,
                    bool(result),
                    (time() - started_at) * 1000,
                    queue.qsize(),
                )
                dev_log(
                    "printer_action_done",
                    owner=owner,
                    device_identifier=device_identifier,
                    action=action,
                    result=bool(result),
                    duration_ms=round((time() - started_at) * 1000, 1),
                    remaining_queue=queue.qsize(),
                )
            except asyncio.CancelledError:
                if isinstance(future, asyncio.Future) and not future.done():
                    future.cancel()
                raise
            except Exception:
                dev_log(
                    "printer_action_exception",
                    owner=owner,
                    device_identifier=device_identifier,
                    action=action,
                    duration_ms=round((time() - started_at) * 1000, 1),
                    action_summary=summarize_action(data),
                )
                _logger.exception(
                    "Printer action failed owner=%s device=%s action=%s duration_ms=%.1f",
                    owner,
                    device_identifier,
                    action,
                    (time() - started_at) * 1000,
                )
                if isinstance(device, Device):
                    await self.event_bus.publish(
                        IoTEvent(
                            device_identifier=device.identifier,
                            owner=owner,
                            status="error",
                            message="ERROR_FAILED",
                            result={"printer": self._printer_name(device), "mode": action},
                        )
                    )
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_result(True)
            finally:
                queue.task_done()

    async def execute(self, owner: str, device_identifier: str, data: dict[str, Any]) -> bool:
        requested_device_identifier = device_identifier
        generic_device_request = device_identifier == "printer"
        if generic_device_request:
            self._refresh_devices_if_needed()
            device_identifier = self._first_device_by_type(device_identifier) or device_identifier

        device = self.devices.get(device_identifier)
        if not device:
            # First try a gentler refresh (keeps PowerShell cache if it's fresh)
            self._refresh_devices_if_needed()
            if generic_device_request:
                device_identifier = self._first_device_by_type(requested_device_identifier) or requested_device_identifier
            device = self.devices.get(device_identifier)
        if not device:
            refresh_started_at = time()
            self._refresh_devices(force=True)
            _logger.info(
                "Device execute refreshed devices owner=%s requested=%s action=%s refresh_ms=%.1f",
                owner,
                requested_device_identifier,
                data.get("action", ""),
                (time() - refresh_started_at) * 1000,
            )
            if generic_device_request:
                device_identifier = self._first_device_by_type(requested_device_identifier) or requested_device_identifier
            device = self.devices.get(device_identifier)
        if not device:
            _logger.warning(
                "Device execute failed because device was not found owner=%s requested=%s resolved=%s action=%s",
                owner,
                requested_device_identifier,
                device_identifier,
                data.get("action", ""),
            )
            dev_log(
                "device_execute_missing_device",
                owner=owner,
                requested_device_identifier=requested_device_identifier,
                resolved_device_identifier=device_identifier,
                action=data.get("action", ""),
                available_devices=list(self.devices.keys()),
                action_summary=summarize_action(data),
            )
            return False

        _logger.info(
            "Device execute owner=%s requested=%s resolved=%s type=%s action=%s",
            owner,
            requested_device_identifier,
            device_identifier,
            device.type,
            data.get("action", ""),
        )
        _logger.debug(
            "Device execute detail owner=%s device=%s printer_name=%s action=%s "
            "has_receipt=%s backend=%s connection=%s metadata=%s",
            owner,
            device.identifier,
            self._printer_name(device) or "<none>",
            data.get("action", ""),
            "yes" if data.get("receipt") else "no",
            device.metadata.get("backend") if isinstance(device.metadata, dict) else "<none>",
            device.connection,
            {k: v for k, v in device.metadata.items() if k in ("windows_printer", "backend", "raw_tcp_host")}
            if isinstance(device.metadata, dict) else {},
        )
        dev_log(
            "device_execute",
            owner=owner,
            requested_device_identifier=requested_device_identifier,
            resolved_device_identifier=device_identifier,
            device_type=device.type,
            device_connection=device.connection,
            printer=self._printer_name(device),
            action=data.get("action", ""),
            device_metadata=device.metadata,
            action_summary=summarize_action(data),
        )

        action = data.get("action", "")
        if action == "print_receipt" and self._is_native_receipt_image_action(data):
            if self._can_submit_printer_action_directly(device):
                return await self._submit_printer_action_directly(owner, device, data, "_print_receipt_native_image")
            return await self._queue_printer_action(owner, device, data, "_print_receipt_native_image")
        if action in {"print_receipt", "print_receipt_escpos"}:
            if self._can_submit_printer_action_directly(device):
                return await self._submit_printer_action_directly(owner, device, data, "_print_receipt_escpos")
            return await self._queue_printer_action(owner, device, data, "_print_receipt_escpos")
        if action == "cashbox":
            opened = self._open_cashbox(device)
            status = "success" if opened else "error"
            message = None if opened else "ERROR_PRINTER"
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status=status,
                    message=message,
                    result={"cashbox": "opened" if opened else "failed"},
                )
            )
            return True

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"action": action, "ok": True},
            )
        )
        _logger.info(
            "Device execute default-success owner=%s device_identifier=%s action=%s",
            owner,
            device.identifier,
            action,
        )
        dev_log("device_execute_default_success", owner=owner, device_identifier=device.identifier, action=action)
        return True

    def _first_device_by_type(self, device_type: str) -> str | None:
        if device_type == "printer":
            return self._first_direct_printer_identifier()
        for d in self.devices.values():
            if d.type == device_type:
                return d.identifier
        return None

    def _first_direct_printer_identifier(self) -> str | None:
        configured_identifier = self._configured_printer_identifier()
        if configured_identifier and configured_identifier in self.devices:
            device = self.devices.get(configured_identifier)
            if device and device.type == "printer" and device.connection == "direct":
                return configured_identifier
        for d in self.devices.values():
            if d.type == "printer" and d.connection == "direct":
                return d.identifier
        for d in self.devices.values():
            if d.type == "printer":
                return d.identifier
        return None

    def _can_submit_printer_action_directly(self, device: Device) -> bool:
        if self._raw_tcp_endpoint(device):
            return False
        return str(device.metadata.get("backend") or "").strip().lower() == "windows"

    async def _submit_printer_action_directly(
        self,
        owner: str,
        device: Device,
        data: dict[str, Any],
        handler_name: str,
    ) -> bool:
        started_at = time()
        action = str(data.get("action") or handler_name.lstrip("_"))
        _logger.info(
            "Printer action direct start owner=%s device=%s action=%s backend=%s",
            owner,
            device.identifier,
            action,
            str(device.metadata.get("backend") or ""),
        )
        dev_log(
            "printer_action_direct_start",
            owner=owner,
            device_identifier=device.identifier,
            action=action,
            action_summary=summarize_action(data),
        )
        try:
            handler = getattr(self, handler_name)
            result = await handler(owner, device, data)
        except Exception:
            dev_log(
                "printer_action_direct_exception",
                owner=owner,
                device_identifier=device.identifier,
                action=action,
                duration_ms=round((time() - started_at) * 1000, 1),
                action_summary=summarize_action(data),
            )
            _logger.exception(
                "Printer action direct failed owner=%s device=%s action=%s duration_ms=%.1f",
                owner,
                device.identifier,
                action,
                (time() - started_at) * 1000,
            )
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message="ERROR_FAILED",
                    result={"printer": self._printer_name(device), "mode": action},
                )
            )
            return True
        _logger.info(
            "Printer action direct done owner=%s device=%s action=%s result=%s duration_ms=%.1f",
            owner,
            device.identifier,
            action,
            bool(result),
            (time() - started_at) * 1000,
        )
        dev_log(
            "printer_action_direct_done",
            owner=owner,
            device_identifier=device.identifier,
            action=action,
            result=bool(result),
            duration_ms=round((time() - started_at) * 1000, 1),
        )
        return bool(result)

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
            return self._discover_windows_printer_devices()
        return {
            "printer_main": Device(
                identifier="printer_main",
                name="Receipt Printer",
                type="printer",
                connection="direct",
                subtype="receipt_printer",
            )
        }

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
            if (candidate / ".venv").exists() and (candidate / "instances").exists():
                return candidate
        return current.parents[5]

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

    def cleanup_spool(self, retention_seconds: int) -> int:
        if retention_seconds < 0:
            return 0
        if not self.spool_dir.exists():
            return 0
        now_ts = time()
        removed = 0
        for pattern in ("receipt_*.bin", "cashbox_*.bin", "native_receipt_*.png"):
            for path in self.spool_dir.glob(pattern):
                try:
                    if not path.is_file():
                        continue
                    age = now_ts - path.stat().st_mtime
                    if age >= retention_seconds:
                        path.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        return removed

    async def _print_receipt_escpos(self, owner: str, device: Device, data: dict[str, Any]) -> bool:
        result = await asyncio.to_thread(self._process_receipt_escpos, owner, device, data)
        target = result.get("target")
        printed_ok = bool(result.get("ok"))
        error = str(result.get("error") or "ERROR_FAILED")
        if not printed_ok:
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message=error,
                    result={"spooled_file": str(target), "printer": self._printer_name(device), "mode": "escpos"},
                )
            )
            return True

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"spooled_file": str(target), "printer": self._printer_name(device), "mode": "escpos"},
            )
        )
        return True

    async def _print_receipt_native_image(self, owner: str, device: Device, data: dict[str, Any]) -> bool:
        result = await asyncio.to_thread(self._process_receipt_native_image, owner, device, data)
        target = result.get("target")
        printed_ok = bool(result.get("ok"))
        error = str(result.get("error") or "ERROR_FAILED")
        if not printed_ok:
            await self.event_bus.publish(
                IoTEvent(
                    device_identifier=device.identifier,
                    owner=owner,
                    status="error",
                    message=error,
                    result={"image_file": str(target), "printer": self._printer_name(device), "mode": "native_image"},
                )
            )
            return True

        await self.event_bus.publish(
            IoTEvent(
                device_identifier=device.identifier,
                owner=owner,
                status="success",
                result={"image_file": str(target), "printer": self._printer_name(device), "mode": "native_image"},
            )
        )
        return True

    def _process_receipt_native_image(self, owner: str, device: Device, data: dict[str, Any]) -> dict[str, Any]:
        started_at = time()
        payload = data.get("receipt") or {}
        image_data = self._native_receipt_image_bytes(payload)
        if not image_data:
            _logger.warning(
                "Native receipt image print skipped because no image was found owner=%s device=%s",
                owner,
                device.identifier,
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": None}

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"native_receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.png"
        try:
            with Image.open(BytesIO(image_data)) as img:
                img = img.convert("RGB")
                img.save(target, format="PNG")
                printed_ok = self._send_native_image_to_windows_printer(device, img)
                _logger.info(
                    "Native receipt image send result owner=%s device=%s printer=%s printed_ok=%s image=%sx%s file=%s total_ms=%.1f",
                    owner,
                    device.identifier,
                    self._printer_name(device) or "<unknown>",
                    printed_ok,
                    img.width,
                    img.height,
                    target,
                    (time() - started_at) * 1000,
                )
                return {"ok": printed_ok, "error": None if printed_ok else "ERROR_PRINTER", "target": target}
        except Exception as exc:
            _logger.exception(
                "Native receipt image print failed owner=%s device=%s error_type=%s error=%s",
                owner,
                device.identifier,
                type(exc).__name__,
                str(exc),
            )
            return {"ok": False, "error": "ERROR_PRINTER", "target": target}

    def _process_receipt_escpos(self, owner: str, device: Device, data: dict[str, Any]) -> dict[str, Any]:
        total_started_at = time()
        payload = data.get("receipt") or {}
        # Ensure payload is a dict; Odoo may send either a JSON object string
        # or a rendered receipt image as a data URL for print_receipt.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = self._receipt_string_payload(payload)
        self._write_debug_request_payload(payload)
        structured = payload.get("structured") if isinstance(payload, dict) else None
        lines = payload.get("lines") if isinstance(payload, dict) else None
        raw_order = payload.get("order") if isinstance(payload, dict) else None
        raw_order_data = payload.get("order_data") if isinstance(payload, dict) else None
        if isinstance(lines, list):
            lines = self._coerce_embedded_receipt_image_lines(lines)
        _logger.debug(
            "ESCPOS process start owner=%s device=%s printer_name=%s "
            "has_structured=%s has_lines=%s has_raw_order=%s has_order_data=%s payload_type=%s action=%s action_unique_id=%s",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            "yes" if structured else "no",
            "yes" if isinstance(lines, list) else "no",
            "yes" if isinstance(raw_order, dict) else "no",
            "yes" if isinstance(raw_order_data, dict) else "no",
            type(payload).__name__,
            str(data.get("action") or ""),
            str(data.get("action_unique_id") or ""),
        )
        if isinstance(structured, dict):
            lines = self._build_structured_receipt_lines(structured)
            _logger.debug(
                "ESCPOS built structured receipt lines=%s owner=%s device=%s",
                len(lines) if isinstance(lines, list) else 0,
                owner,
                device.identifier,
            )
        elif isinstance(raw_order_data, dict):
            # Native Odoo kitchen ticket data format from getOrderData()
            # Used by kitchen_print_patch.js which sends the native
            # OrderChangeReceipt data directly to the IoT Box REST.
            lines = self._build_kitchen_from_order_data(raw_order_data)
            _logger.info(
                "ESCPOS built kitchen ticket from order_data lines=%s owner=%s device=%s",
                len(lines) if isinstance(lines, list) else 0,
                owner,
                device.identifier,
            )
        elif isinstance(raw_order, dict):
            # Build receipt lines from raw Odoo POS order data.
            # Detect kitchen tickets by checking for kitchen-specific flags
            # in the order or fall back to standard receipt builder.
            kitchen = bool(raw_order.get("kitchen") or raw_order.get("is_kitchen") or False)
            lines = build_lines(raw_order, kitchen=kitchen)
            _logger.info(
                "ESCPOS built receipt lines from raw order data lines=%s owner=%s device=%s",
                len(lines) if isinstance(lines, list) else 0,
                owner,
                device.identifier,
            )
        if not isinstance(lines, list) or not lines:
            raw_lines_type = type(payload).__name__ if not isinstance(payload, dict) else (
                type(payload.get("lines")).__name__ if payload.get("lines") is not None else "none"
            )
            _logger.warning(
                "ESCPOS no receipt lines found owner=%s device=%s structured=%s raw_lines=%s",
                owner,
                device.identifier,
                "yes" if structured else "no",
                raw_lines_type,
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": None}
        # Lines from raw_order or structured are already fully built and do
        # not need the fragile normalisation pass that was required for the
        # old JS-rendered receipt line format.
        skip_normalize = isinstance(raw_order, dict) or isinstance(structured, dict)
        is_kitchen_ticket = self._is_kitchen_ticket_lines(lines)
        if is_kitchen_ticket:
            lines = self._drop_kitchen_image_lines(lines)
            lines = [self._normalize_receipt_line_text(line) for line in lines if isinstance(line, dict)]
        elif not skip_normalize:
            lines = self._normalize_receipt_lines(lines)
            lines = self._ensure_receipt_qr_line(lines)
        receipt_summary = self._summarize_escpos_receipt(lines, payload, is_kitchen_ticket)
        _logger.info(
            "ESC/POS receipt prepared owner=%s device=%s printer=%s action_unique_id=%s receipt_type=%s "
            "receipt_ref=%s line_count=%s product_count=%s products=%s fingerprint=%s",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            str(data.get("action_unique_id") or ""),
            receipt_summary["receipt_type"],
            receipt_summary["receipt_ref"],
            len(lines),
            receipt_summary["product_count"],
            receipt_summary["products"],
            receipt_summary["fingerprint"],
        )

        debug_started_at = time()
        self._write_debug_payload(lines)
        debug_duration_ms = (time() - debug_started_at) * 1000
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        try:
            build_started_at = time()
            if is_kitchen_ticket:
                # Kitchen tickets use plain-text ESC/POS (no formatting)
                escpos_bytes = self._build_kitchen_escpos_bytes(
                    lines,
                    cut=bool(payload.get("cut", True)),
                    payload=payload,
                )
            else:
                escpos_bytes = self._build_escpos_bytes(
                    lines,
                    cut=bool(payload.get("cut", True)),
                    payload=payload,
                    normalize_lines=True,
                )
            build_duration_ms = (time() - build_started_at) * 1000
            write_started_at = time()
            target.write_bytes(escpos_bytes)
            write_duration_ms = (time() - write_started_at) * 1000
            _logger.debug(
                "ESCPOS bytes built and written target=%s bytes=%s build_ms=%.1f write_ms=%.1f",
                target,
                len(escpos_bytes),
                build_duration_ms,
                write_duration_ms,
            )
        except Exception as exc:
            _logger.error(
                "ESCPOS build or write failed owner=%s device=%s error_type=%s error=%s",
                owner,
                device.identifier,
                type(exc).__name__,
                str(exc),
            )
            return {"ok": False, "error": "ERROR_FAILED", "target": target}
        print_started_at = time()
        printed_ok = self._send_raw_to_printer(device, target)
        print_duration_ms = (time() - print_started_at) * 1000
        _logger.info(
            "ESCPOS send result owner=%s device=%s printer=%s printed_ok=%s "
            "bytes=%s build_ms=%.1f send_ms=%.1f total_ms=%.1f",
            owner,
            device.identifier,
            self._printer_name(device) or "<unknown>",
            printed_ok,
            len(escpos_bytes),
            build_duration_ms,
            print_duration_ms,
            (time() - total_started_at) * 1000,
        )
        _perf_log(
            "ESCPOS profile "
            f"lines={len(lines)} "
            f"bytes={len(escpos_bytes)} "
            f"debug_ms={debug_duration_ms:.1f} "
            f"build_ms={build_duration_ms:.1f} "
            f"write_ms={write_duration_ms:.1f} "
            f"send_ms={print_duration_ms:.1f} "
            f"total_ms={(time() - total_started_at) * 1000:.1f} "
            f"printer={self._printer_name(device) or '<unknown>'} "
            f"ok={printed_ok}"
        )
        return {"ok": printed_ok, "error": "ERROR_PRINTER" if not printed_ok else None, "target": target}

    def _coerce_embedded_receipt_image_lines(self, lines: list[Any]) -> list[Any]:
        coerced: list[Any] = []
        changed = False
        for line in lines:
            if not isinstance(line, dict):
                coerced.append(line)
                continue
            text = str(line.get("text") or "").strip()
            image_src = self._base64_receipt_image_src(text)
            if not image_src:
                coerced.append(line)
                continue
            next_line = dict(line)
            next_line.pop("text", None)
            next_line["type"] = "image"
            next_line["src"] = image_src
            next_line["align"] = next_line.get("align") or "center"
            next_line["image_kind"] = next_line.get("image_kind") or "receipt"
            classes = next_line.get("classes")
            if not isinstance(classes, list):
                classes = []
            next_line["classes"] = [*classes, "embedded-receipt-image"]
            coerced.append(next_line)
            changed = True
        if changed:
            _logger.info("ESCPOS converted embedded base64 receipt image lines=%s", len(lines))
        return coerced

    def _base64_receipt_image_src(self, text: str) -> str:
        compact = re.sub(r"\s+", "", str(text or ""))
        if len(compact) < 200:
            return ""
        if compact.startswith("data:image/"):
            return compact
        image_type = ""
        if compact.startswith("/9j/"):
            image_type = "jpeg"
        elif compact.startswith("iVBOR"):
            image_type = "png"
        elif compact.startswith("R0lGOD"):
            image_type = "gif"
        elif compact.startswith("UklGR"):
            image_type = "webp"
        if not image_type:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            return ""
        return f"data:image/{image_type};base64,{compact}"

    def _receipt_string_payload(self, raw_receipt: str) -> dict[str, Any]:
        text = str(raw_receipt or "").strip()
        if not text:
            return {}
        image_src = self._base64_receipt_image_src(text)
        if image_src:
            text = image_src
        if self._looks_like_receipt_image_source(text):
            return {
                "lines": [
                    {
                        "type": "image",
                        "src": text,
                        "align": "center",
                        "classes": ["rendered-receipt-image"],
                        "image_kind": "receipt",
                    }
                ]
            }
        return {"lines": self._plain_receipt_text_lines(text)}

    def _is_native_receipt_image_action(self, data: dict[str, Any]) -> bool:
        if str(data.get("action") or "") != "print_receipt":
            return False
        return bool(self._native_receipt_image_bytes(data.get("receipt") or {}, probe_only=True))

    def _native_receipt_image_bytes(self, payload: Any, probe_only: bool = False) -> bytes:
        if isinstance(payload, str):
            stripped = payload.strip()
            try:
                payload = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                if self._looks_like_receipt_image_source(stripped) or self._base64_receipt_image_src(stripped):
                    if probe_only:
                        return b"image"
                    src = self._base64_receipt_image_src(stripped) or stripped
                    return self._fetch_receipt_image(src)
                return b""

        if not isinstance(payload, dict):
            return b""
        lines = payload.get("lines")
        if not isinstance(lines, list):
            return b""
        for line in lines:
            if not isinstance(line, dict):
                continue
            line_type = str(line.get("type") or "")
            image_kind = str(line.get("image_kind") or "")
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            src = str(line.get("src") or "").strip()
            if line_type != "image" or not src:
                continue
            if image_kind == "receipt" or "rendered-receipt-image" in classes or "embedded-receipt-image" in classes:
                if probe_only:
                    return b"image"
                return self._fetch_receipt_image(src)
        return b""

    def _looks_like_receipt_image_source(self, text: str) -> bool:
        lowered = text[:128].lower()
        return (
            lowered.startswith("data:image/")
            or lowered.startswith("http://")
            or lowered.startswith("https://")
        )

    def _plain_receipt_text_lines(self, raw_receipt: str) -> list[dict[str, Any]]:
        text = str(raw_receipt or "").strip()
        if not text:
            return []
        if text.startswith("<"):
            try:
                root = ET.fromstring(text)
                lines: list[dict[str, Any]] = []
                self._append_plain_receipt_xml_lines(root, lines)
                if lines:
                    return lines
            except ET.ParseError:
                pass
        return [
            {"text": line.strip(), "align": "left", "bold": False, "classes": []}
            for line in text.splitlines()
            if line.strip()
        ]

    def _append_plain_receipt_xml_lines(
        self,
        node: ET.Element,
        lines: list[dict[str, Any]],
        inherited: dict[str, Any] | None = None,
    ) -> None:
        inherited = dict(inherited or {})
        tag = node.tag.rsplit("}", 1)[-1].lower()
        style = dict(inherited)
        class_attr = str(node.attrib.get("class") or "")
        classes = [part for part in class_attr.split() if part]

        if tag == "center" or str(node.attrib.get("align") or "").lower() == "center":
            style["align"] = "center"
        elif tag == "right" or "pos-receipt-right-align" in classes:
            style["align"] = "right"
        if tag in {"b", "strong", "bold"}:
            style["bold"] = True
        if tag in {"h1", "h2"}:
            style["bold"] = True
            style["double_width"] = True
            style["double_height"] = True

        src = str(node.attrib.get("src") or "").strip()
        if tag == "img" and src:
            lines.append(
                {
                    "type": "image",
                    "src": src,
                    "align": style.get("align", "center"),
                    "classes": classes,
                    "image_kind": "receipt",
                }
            )

        pieces: list[str] = []
        if node.text and node.text.strip():
            pieces.append(node.text.strip())
        for child in list(node):
            self._append_plain_receipt_xml_lines(child, lines, style)
            if child.tail and child.tail.strip():
                pieces.append(child.tail.strip())
        if pieces and tag not in {"receipt", "root", "table", "tbody", "thead", "tr", "img"}:
            lines.append(
                {
                    "text": " ".join(pieces),
                    "align": style.get("align", "left"),
                    "bold": bool(style.get("bold")),
                    "double_width": bool(style.get("double_width")),
                    "double_height": bool(style.get("double_height")),
                    "classes": classes,
                }
            )

    def _is_kitchen_ticket_lines(self, lines: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(line, dict)
            and isinstance(line.get("classes"), list)
            and "kitchen-product-line" in [str(cls) for cls in line.get("classes") or []]
            for line in lines
        )

    def _is_rendered_receipt_image_lines(self, lines: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(line, dict)
            and str(line.get("type") or "") == "image"
            and (
                str(line.get("image_kind") or "") == "receipt"
                or (
                    isinstance(line.get("classes"), list)
                    and "rendered-receipt-image" in [str(cls) for cls in line.get("classes") or []]
                )
            )
            for line in lines
        )

    def _summarize_escpos_receipt(
        self,
        lines: list[dict[str, Any]],
        payload: dict[str, Any],
        is_kitchen_ticket: bool,
    ) -> dict[str, Any]:
        products: list[str] = []
        references: list[str] = []
        has_total = False
        for line in lines:
            if not isinstance(line, dict):
                continue
            line_type = str(line.get("type") or "")
            if line_type == "product_line":
                qty = str(line.get("qty") or "").strip()
                name = str(line.get("name") or "").strip()
                if name:
                    products.append(f"{qty} {name}".strip())
            if line_type == "header_meta_line":
                left = str(line.get("left_text") or "").strip()
                right = str(line.get("right_text") or "").strip()
                text = " ".join(part for part in (left, right) if part)
            else:
                text = str(line.get("text") or "").strip()
            upper_text = text.upper()
            if text and (text.startswith("#") or "MESA" in upper_text or "TABLE" in upper_text):
                references.append(text)
            if "total" in text.strip().lower():
                has_total = True

        structured = payload.get("structured") if isinstance(payload.get("structured"), dict) else {}
        if structured:
            for key in ("name", "order_name", "pos_reference", "reference", "tracking_number", "table"):
                value = str(structured.get(key) or "").strip()
                if value:
                    references.insert(0, value)
                    break
            if structured.get("total_line"):
                has_total = True

        if is_kitchen_ticket:
            receipt_type = "kitchen"
        elif products and has_total:
            receipt_type = "pos_receipt"
        elif products:
            receipt_type = "product_receipt"
        elif has_total:
            receipt_type = "total_receipt"
        else:
            receipt_type = "unknown"

        fingerprint_source = {
            "receipt_type": receipt_type,
            "refs": references[:4],
            "products": products,
            "total_line": structured.get("total_line") if isinstance(structured, dict) else "",
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_source, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8",
                errors="replace",
            )
        ).hexdigest()[:12]
        return {
            "receipt_type": receipt_type,
            "receipt_ref": " | ".join(dict.fromkeys(references[:4]))[:120],
            "product_count": len(products),
            "products": " | ".join(products[:8]),
            "fingerprint": fingerprint,
        }

    def _drop_kitchen_image_lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        skipped = 0
        for line in lines:
            if isinstance(line, dict) and str(line.get("type") or "") == "image":
                skipped += 1
                continue
            filtered.append(line)
        if skipped:
            _logger.info(
                "Kitchen ESC/POS images skipped skipped_images=%s lines_before=%s lines_after=%s",
                skipped,
                len(lines),
                len(filtered),
            )
        return filtered

    def _build_structured_receipt_lines(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        loyalty_cards = [card for card in (receipt.get("loyalty_cards") or []) if isinstance(card, dict)]
        loyalty_card_names = {
            str(card.get("name") or "").strip().lower()
            for card in loyalty_cards
            if str(card.get("name") or "").strip()
        }
        _perf_log(
            "[IOT ESCPOS PAYLOAD] "
            + json.dumps(
                {
                    "summary_lines": receipt.get("summary_lines") or [],
                    "total_line": receipt.get("total_line") or "",
                    "payment_lines": receipt.get("payment_lines") or [],
                    "change_line": receipt.get("change_line") or "",
                    "discount_line": receipt.get("discount_line") or "",
                },
                ensure_ascii=False,
            )
        )

        logo = receipt.get("logo") if isinstance(receipt.get("logo"), dict) else None
        logo_src = str(logo.get("src") or "").strip() if logo else ""
        if logo_src:
            logo_src = self._with_logo_cache_buster(logo_src)
            lines.append(
                {
                    "type": "image",
                    "src": logo_src,
                    "align": "left",
                    "classes": ["pos-receipt-logo"],
                    "width": 480,
                    "height": 150,
                    "image_kind": "logo",
                }
            )

        company_section_added = False
        company_lines, inferred_reference = self._split_company_and_reference_lines(receipt.get("company_lines") or [])
        for text, is_bold in company_lines:
            company_section_added = True
            lines.append(
                {
                    "text": text,
                    "align": "center",
                    "bold": is_bold,
                    "double_width": False,
                    "classes": ["company-info"],
                }
            )

        order_info_lines: list[dict[str, Any]] = []
        for value in (
            inferred_reference,
            receipt.get("reference_text"),
            receipt.get("date_text"),
            receipt.get("cashier_text"),
        ):
            text = self._normalize_order_info_text(value)
            if text:
                upper_text = text.upper()
                is_table_line = upper_text.startswith("MESA ") or upper_text.startswith("TABLE ")
                order_info_lines.append(
                    {
                        "text": text,
                        "align": "center",
                        "bold": is_table_line,
                        "double_width": is_table_line,
                        "double_height": is_table_line,
                        "classes": ["table-info"] if is_table_line else ["order-info"],
                    }
                )

        if company_section_added and order_info_lines:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "company-order-spacer"],
                }
            )
        lines.extend(order_info_lines)

        # Detect whether this receipt carries a table (MESA/TABLE) marker.
        # Receipts without a table should stay compact: no extra blank lines
        # and no doubled separators around the barcode/product section.
        has_table = False
        for order_line in order_info_lines:
            upper = str(order_line.get("text") or "").upper()
            if "MESA" in upper or "TABLE" in upper:
                has_table = True
                break
        if not has_table:
            for header_text in receipt.get("header_lines") or []:
                upper = str(header_text or "").upper()
                if "MESA" in upper or "TABLE" in upper:
                    has_table = True
                    break

        # ── Simplified invoice info (Factura Simplificada) ─────────────
        factura_number = str(receipt.get("factura_simplificada_number") or "").strip()
        if factura_number:
            lines.append(
                {
                    "text": "*" * 26,
                    "align": "center",
                    "classes": ["invoice-asterisk-border"],
                }
            )
            lines.append(
                {
                    "text": "Factura Simplificada",
                    "align": "center",
                    "bold": True,
                    "double_width": False,
                    "double_height": False,
                    "classes": ["simplified-invoice-title"],
                }
            )
            lines.append(
                {
                    "text": factura_number,
                    "align": "center",
                    "bold": False,
                    "double_width": False,
                    "double_height": False,
                    "classes": ["simplified-invoice-number"],
                }
            )
            lines.append(
                {
                    "text": "*" * 26,
                    "align": "center",
                    "classes": ["invoice-asterisk-border"],
                }
            )
            if has_table:
                lines.append(
                    {
                        "type": "spacer",
                        "align": "left",
                        "classes": ["receipt-spacer", "company-order-spacer"],
                    }
                )

        for item in receipt.get("company_lines") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            # Remaining header lines that weren't promoted into the fixed
            # company/order sections stay out to avoid duplicated content.
            continue

        for text in receipt.get("header_lines") or []:
            text = str(text or "").strip()
            if text:
                lines.append({"text": text, "align": "center", "bold": False, "double_width": False, "classes": []})

        customer = receipt.get("customer") if isinstance(receipt.get("customer"), dict) else None
        if not customer:
            customer = self._load_customer_from_reference_text(receipt.get("reference_text"))
        customer_rows = [
            str(customer.get("name") or "").strip() if customer else "",
            str(customer.get("vat") or "").strip() if customer else "",
            str(customer.get("address") or "").strip() if customer else "",
            str(customer.get("region") or "").strip() if customer else "",
        ]
        customer_rows = [text for text in customer_rows if text]
        if customer_rows:
            lines.append(
                {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "customer-info-separator"],
                }
            )
            for text in customer_rows:
                lines.append(
                    {
                        "text": text,
                        "align": "center",
                        "bold": False,
                        "double_width": False,
                        "classes": ["customer-info"],
                    }
                )
            lines.append(
                {
                    "text": "-" * self._escpos_line_width(),
                    "align": "left",
                    "classes": ["receipt-separator", "customer-info-separator"],
                }
            )

        barcode = receipt.get("barcode") if isinstance(receipt.get("barcode"), dict) else None
        barcode_src = str(barcode.get("src") or "").strip() if barcode else ""
        if barcode_src:
            barcode_is_qr = self._is_qr_receipt_image_src(barcode_src, barcode)
            barcode_width = int(barcode.get("width") or (200 if barcode_is_qr else 260))
            barcode_height = int(barcode.get("height") or (barcode_width if barcode_is_qr else 58))
            barcode_classes = ["order-qr-img"] if barcode_is_qr else ["order-barcode-img"]
            if not has_table:
                # Compact receipts without a table: don't print the extra
                # separator line right after the barcode.
                barcode_classes.append("no-barcode-separator")
            lines.append(
                {
                    "type": "image",
                    "src": barcode_src,
                    "align": "center",
                    "classes": barcode_classes,
                    "width": barcode_width,
                    "height": barcode_height,
                    "image_kind": "qr" if barcode_is_qr else "barcode",
                }
            )

        receipt_items = [item for item in (receipt.get("items") or []) if isinstance(item, dict)]
        for item_index, item in enumerate(receipt_items):
            qty = str(item.get("qty") or "").strip()
            raw_name = str(item.get("name") or "").strip()
            total = str(item.get("total") or "").strip()
            if not qty or not raw_name or not total:
                continue
            name = raw_name
            if loyalty_card_names and name.strip().lower() in loyalty_card_names:
                continue
            combo_items = [
                str(combo).strip() for combo in (item.get("combo_items") or []) if str(combo).strip()
            ]
            # NOTE: the product column header ("Uds. Producto Importe") and its
            # leading separator are intentionally NOT generated here. They are
            # printed once by _build_escpos_bytes() when it encounters the first
            # product_line, using _build_product_header_text() (fully aligned).
            # Generating them here would produce a flattened duplicate header.
            lines.append(
                {
                    "type": "product_line",
                    "qty": qty,
                    "name": name,
                    "unit_price": str(item.get("unit_price") or "").strip(),
                    "total": total,
                    "combo_items": combo_items,
                    "discount_text": str(item.get("discount_text") or "").strip(),
                    "original_total": str(item.get("original_total") or "").strip(),
                }
            )
            customer_note = str(item.get("customer_note") or "").strip()
            if customer_note:
                lines.append(
                    {
                        "text": customer_note,
                        "align": "left",
                        "bold": False,
                        "double_width": False,
                        "classes": ["customer-note"],
                    }
                )
        summary_lines = [str(text or "").strip() for text in receipt.get("summary_lines") or [] if str(text or "").strip()]
        if summary_lines:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "summary-line-spacer"],
                }
            )
        for index, text in enumerate(summary_lines):
            if text:
                next_text = summary_lines[index + 1].lower() if index + 1 < len(summary_lines) else ""
                is_subtotal_line = text.lower().startswith("subtotal")
                next_is_tax_line = self._looks_like_tax_summary_line(next_text)
                lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": []})
                is_last_summary_line = index == len(summary_lines) - 1
                if (
                    not self._looks_like_tax_summary_line(text)
                    and not (is_subtotal_line and next_is_tax_line)
                    and not is_last_summary_line
                ):
                    lines.append(
                        {
                            "type": "spacer",
                            "align": "left",
                            "classes": ["receipt-spacer", "summary-line-spacer"],
                        }
                    )

        total_line = str(receipt.get("total_line") or "").strip()
        if total_line:
            lines.append(
                {
                    "text": total_line,
                    "align": "center",
                    "bold": True,
                    "double_width": True,
                    "classes": ["receipt-total"],
                }
            )

        for item in receipt.get("payment_lines") or []:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": ["paymentlines"]})
                lines.append(
                    {
                        "type": "spacer",
                        "align": "left",
                        "classes": ["receipt-spacer", "summary-line-spacer"],
                    }
                )

        for value in (receipt.get("change_line"), receipt.get("discount_line")):
            text = str(value or "").strip()
            if text:
                lines.append({"text": text, "align": "left", "bold": False, "double_width": False, "classes": []})

        for receipt_item in receipt.get("payment_terminal_receipts") or []:
            if not isinstance(receipt_item, dict):
                continue
            terminal_logo_src = str(receipt_item.get("logoSrc") or "").strip()
            if terminal_logo_src:
                image_classes = (
                    ["payment-terminal-nfc-icon"]
                    if self._is_payment_terminal_nfc_src(terminal_logo_src)
                    else ["payment-terminal-logo"]
                )
                lines.append(
                    {
                        "type": "image",
                        "src": terminal_logo_src,
                        "align": "center",
                        "classes": image_classes,
                        "width": 96,
                        "height": 48,
                        "image_kind": "image",
                    }
                )
            for text in self._iter_payment_terminal_receipt_lines(receipt_item):
                text = self._normalize_payment_terminal_line(text)
                if text:
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": False,
                            "double_width": False,
                            "classes": ["payment-terminal-line", "pos-payment-terminal-receipt"],
                        }
                    )

        portal = receipt.get("portal") if isinstance(receipt.get("portal"), dict) else None
        if portal and portal.get("show"):
            qr_src = str(
                portal.get("qrSrc")
                or portal.get("qr_src")
                or portal.get("qr")
                or portal.get("src")
                or ""
            ).strip()
            if qr_src:
                qr_size = max(180, int(os.getenv("IOT_PORTAL_QR_SIZE", "200") or "200"))
                lines.append(
                    {
                        "type": "image",
                        "src": qr_src,
                        "align": "center",
                        "classes": ["m-0", "portal-qr"],
                        "width": qr_size,
                        "height": qr_size,
                        "image_kind": "qr",
                    }
                )
            for value, bold in (
                (portal.get("title"), True),
                (portal.get("url"), False),
                (portal.get("code"), False),
            ):
                text = str(value or "").strip()
                if text:
                    classes: list[str] = []
                    if value == portal.get("url"):
                        classes.append("portal-url")
                    elif value == portal.get("code"):
                        classes.append("unique-code")
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": bold,
                            "double_width": False,
                            "classes": classes,
                        }
                    )

        for loyalty_card in loyalty_cards:
            lines.append(
                {
                    "type": "spacer",
                    "align": "left",
                    "classes": ["receipt-spacer", "gift-card-spacer"],
                }
            )
            loyalty_card_code = str(loyalty_card.get("code") or "").strip()
            for value, bold, classes in (
                (loyalty_card.get("name"), True, ["gift-card-title"]),
                (loyalty_card_code, False, ["gift-card-code"]),
            ):
                text = str(value or "").strip()
                if text:
                    lines.append(
                        {
                            "text": text,
                            "align": "center",
                            "bold": bold,
                            "double_width": False,
                            "classes": classes,
                        }
                    )
            gift_card_barcode_src = ""
            if loyalty_card_code:
                encoded_query = urlencode(
                    {
                        "barcode_type": "Code128",
                        "value": loyalty_card_code,
                        "width": 360,
                        "height": 80,
                    }
                )
                gift_card_barcode_src = f"/report/barcode?{encoded_query}"
            qr_src = str(loyalty_card.get("qrSrc") or "").strip()
            if gift_card_barcode_src:
                lines.append(
                    {
                        "type": "image",
                        "src": gift_card_barcode_src,
                        "align": "center",
                        "classes": ["gift-card-barcode"],
                        "width": 360,
                        "height": 80,
                        "image_kind": "barcode",
                    }
                )
            elif qr_src:
                lines.append(
                    {
                        "type": "image",
                        "src": qr_src,
                        "align": "center",
                        "classes": ["gift-card-qr"],
                        "width": 125,
                        "height": 125,
                        "image_kind": "qr",
                    }
                )
            amount_field, amount_text = self._gift_card_display_amount(loyalty_card)
            _logger.info(
                "Gift card payload code=%s amount_field=%s display_amount=%s payload=%s",
                loyalty_card_code or "<missing>",
                amount_field or "<none>",
                amount_text or "<empty>",
                json.dumps(loyalty_card, ensure_ascii=False, default=str),
            )
            if amount_text:
                lines.append(
                    {
                        "text": amount_text,
                        "align": "center",
                        "bold": True,
                        "double_width": True,
                        "classes": ["gift-card-amount"],
                    }
                )
        # Footer lines: skip any line that duplicates the company name shown at
        # the top (e.g. a trailing "My Company") to avoid repetition.
        company_names = {
            str(item.get("text") or "").strip()
            for item in (receipt.get("company_lines") or [])
            if isinstance(item, dict)
            and item.get("bold")
            and str(item.get("text") or "").strip()
        }
        for text in receipt.get("footer_lines") or []:
            text = str(text or "").strip()
            if not text or text in company_names:
                continue
            lines.append(
                {
                    "text": text,
                    "align": "center",
                    "bold": False,
                    "double_width": False,
                    "classes": ["pos-config-name"],
                }
            )

        return lines

    def _split_receipt_product_name_and_options(self, raw_name: str) -> tuple[str, list[str]]:
        text = str(raw_name or "").strip()
        if not text:
            return "", []
        match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", text)
        if not match:
            return text, []
        base_name = match.group(1).strip() or text
        options_text = match.group(2).strip()
        if not options_text:
            return base_name, []
        options = [part.strip() for part in options_text.split(",") if part.strip()]
        return base_name, options

    def _write_debug_payload(self, lines: list[dict[str, Any]]) -> None:
        debug_path = self.spool_dir / "last_escpos_payload.json"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(
                json.dumps(lines, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _write_debug_request_payload(self, payload: dict[str, Any]) -> None:
        debug_path = self.spool_dir / "last_escpos_request.json"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _send_raw_to_printer(self, device: Device, target_file: Path) -> bool:
        raw_endpoint = self._raw_tcp_endpoint(device)
        _logger.debug(
            "Send raw to printer device=%s printer_name=%s has_raw_endpoint=%s "
            "windows_printing_available=%s target=%s",
            device.identifier,
            self._printer_name(device) or "<none>",
            "yes" if raw_endpoint else "no",
            "yes" if self._is_windows_native_printing_available() else "no",
            target_file,
        )
        if raw_endpoint:
            return self._send_raw_to_tcp_printer(raw_endpoint[0], raw_endpoint[1], target_file)
        printer_name = self._printer_name(device)
        if self._is_windows_native_printing_available():
            return self._send_raw_to_windows_printer(printer_name, target_file)
        _logger.error(
            "No printer backend available device=%s printer_name=%s has_win32print=%s os_name=%s",
            device.identifier,
            printer_name,
            win32print is not None,
            os.name,
        )
        return False

    def _send_native_image_to_windows_printer(self, device: Device, image: Image.Image) -> bool:
        if os.name != "nt" or win32ui is None or win32con is None or ImageWin is None:
            _logger.error(
                "Windows native image printing unavailable os_name=%s has_win32ui=%s has_imagewin=%s",
                os.name,
                win32ui is not None,
                ImageWin is not None,
            )
            return False

        printer_name = self._printer_name(device)
        resolved_printer = self._resolve_windows_printer(printer_name)
        if not resolved_printer:
            _logger.error("Native image print no Windows printer resolved requested=%s", printer_name)
            return False

        dc = None
        try:
            dc = win32ui.CreateDC()
            dc.CreatePrinterDC(resolved_printer)
            printable_width = int(dc.GetDeviceCaps(win32con.HORZRES))
            printable_height = int(dc.GetDeviceCaps(win32con.VERTRES))
            configured_width = int(os.getenv("IOT_NATIVE_IMAGE_TARGET_WIDTH", "0") or "0")
            target_width = configured_width if configured_width > 0 else image.width
            target_width = max(1, min(target_width, printable_width))
            default_left = max(0, int((printable_width - target_width) / 2))
            left_margin = max(0, int(os.getenv("IOT_NATIVE_IMAGE_MARGIN_LEFT", str(default_left)) or str(default_left)))
            top_margin = max(0, int(os.getenv("IOT_NATIVE_IMAGE_MARGIN_TOP", "0") or "0"))
            scale = target_width / max(1, image.width)
            target_height = max(1, int(round(image.height * scale)))
            if printable_height > 0:
                # Thermal receipt drivers often expose a long virtual page. If
                # they expose a short fixed page, keep the same width and let the
                # driver spool the page instead of creating ESC/POS raster data.
                target_height = min(target_height, max(1, printable_height - top_margin))

            bitmap = ImageWin.Dib(image)
            dc.StartDoc("Odoo Native IoT Receipt Image")
            try:
                dc.StartPage()
                bitmap.draw(dc.GetHandleOutput(), (left_margin, top_margin, left_margin + target_width, top_margin + target_height))
                dc.EndPage()
            finally:
                dc.EndDoc()
            _logger.info(
                "Native image printed through Windows driver printer=%s source=%sx%s target=%sx%s printable=%sx%s",
                resolved_printer,
                image.width,
                image.height,
                target_width,
                target_height,
                printable_width,
                printable_height,
            )
            return True
        except Exception as exc:
            _logger.exception(
                "Native image Windows driver print exception printer=%s error_type=%s error=%s",
                resolved_printer,
                type(exc).__name__,
                str(exc),
            )
            return False
        finally:
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    pass

    def _raw_tcp_endpoint(self, device: Device) -> tuple[str, int] | None:
        config = self.local_config_getter() or {}
        mappings = config.get("raw_printer_hosts") or {}
        if not isinstance(mappings, dict):
            mappings = {}
        candidates = [
            device.identifier,
            str(device.name or "").strip(),
            str(device.metadata.get("windows_printer") or "").strip(),
            "printer_main" if device.identifier == self._configured_printer_identifier() else "",
        ]
        raw_value = ""
        for candidate in candidates:
            if candidate and candidate in mappings:
                raw_value = str(mappings.get(candidate) or "").strip()
                break
        if not raw_value:
            raw_value = str(device.metadata.get("raw_tcp_host") or "").strip()
        if not raw_value:
            return None
        host = raw_value
        port = int(config.get("raw_printer_port", 9100) or 9100)
        if ":" in raw_value:
            host_part, port_part = raw_value.rsplit(":", 1)
            host = host_part.strip()
            if port_part.strip().isdigit():
                port = int(port_part.strip())
        if not host:
            return None
        return host, port

    def _send_raw_to_tcp_printer(self, host: str, port: int, target_file: Path) -> bool:
        try:
            payload = target_file.read_bytes()
        except OSError:
            _logger.exception("Raw TCP print failed reading target=%s", target_file)
            return False
        timeout = max(0.5, float((self.local_config_getter() or {}).get("raw_printer_timeout", 4.0) or 4.0))
        started_at = time()
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(payload)
        except OSError:
            _logger.exception(
                "Raw TCP print failed host=%s port=%s target=%s bytes=%s",
                host,
                port,
                target_file,
                len(payload),
            )
            return False
        _logger.info(
            "Raw TCP print sent host=%s port=%s target=%s bytes=%s duration_ms=%.1f",
            host,
            port,
            target_file,
            len(payload),
            (time() - started_at) * 1000,
        )
        return True

    def _open_cashbox(self, device: Device) -> bool:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"cashbox_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        # ESC p m t1 t2: pulse on pin 2, timings chosen for common Epson-compatible drawers.
        pulse_bytes = b"\x1b@\x1bp\x00\x19\xfa"
        try:
            target.write_bytes(pulse_bytes)
        except OSError:
            return False

        return self._send_raw_to_printer(device, target)

    def _is_windows_native_printing_available(self) -> bool:
        return os.name == "nt" and win32print is not None

    def _printer_name(self, device: Device) -> str | None:
        printer_name = str(device.metadata.get("windows_printer") or "").strip()
        if printer_name:
            return printer_name
        return None

    def _send_raw_to_windows_printer(self, printer_name: str | None, target_file: Path) -> bool:
        if not self._is_windows_native_printing_available():
            _logger.error(
                "Windows native printing unavailable os_name=%s has_win32print=%s",
                os.name,
                win32print is not None,
            )
            dev_log(
                "windows_raw_print_unavailable",
                requested_printer=printer_name,
                target_file=str(target_file),
                os_name=os.name,
                has_win32print=win32print is not None,
            )
            return False
        resolve_started_at = time()
        requested_printer = str(printer_name or "").strip()
        if requested_printer:
            resolved_printer = requested_printer
        else:
            resolved_printer = self._resolve_windows_printer(None)
        resolve_duration_ms = (time() - resolve_started_at) * 1000
        if not resolved_printer:
            available_queues = self._windows_printer_queues()
            _logger.error(
                "No Windows printer resolved requested=%s available=%s resolve_ms=%.1f",
                printer_name or "<none>",
                available_queues,
                resolve_duration_ms,
            )
            dev_log(
                "windows_raw_print_no_printer",
                requested_printer=printer_name,
                target_file=str(target_file),
                available_queues=available_queues,
                resolve_ms=round(resolve_duration_ms, 1),
            )
            return False
        _logger.debug(
            "Windows printer resolved requested=%s resolved=%s queues=%s resolve_ms=%.1f",
            requested_printer or "<none>",
            resolved_printer,
            self._windows_printer_queues(),
            resolve_duration_ms,
        )
        try:
            read_started_at = time()
            payload = target_file.read_bytes()
            read_duration_ms = (time() - read_started_at) * 1000
            open_started_at = time()
            handle = win32print.OpenPrinter(resolved_printer)
            open_duration_ms = (time() - open_started_at) * 1000
            _logger.debug(
                "Windows printer opened printer=%s open_ms=%.1f",
                resolved_printer,
                open_duration_ms,
            )
            try:
                job_name = f"Custom IoT Raw {target_file.name}"
                doc_started_at = time()
                win32print.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
                try:
                    win32print.StartPagePrinter(handle)
                    write_started_at = time()
                    win32print.WritePrinter(handle, payload)
                    write_duration_ms = (time() - write_started_at) * 1000
                    _logger.debug(
                        "Windows printer write completed printer=%s bytes=%s write_ms=%.1f",
                        resolved_printer,
                        len(payload),
                        write_duration_ms,
                    )
                    win32print.EndPagePrinter(handle)
                finally:
                    win32print.EndDocPrinter(handle)
            finally:
                win32print.ClosePrinter(handle)
        except Exception as exc:
            try:
                payload_bytes = len(payload)
            except NameError:
                payload_bytes = -1
            _logger.error(
                "Windows raw print exception printer=%s target=%s error_type=%s error=%s bytes=%s",
                resolved_printer,
                target_file,
                type(exc).__name__,
                str(exc),
                payload_bytes,
            )
            dev_log(
                "windows_raw_print_exception",
                requested_printer=printer_name,
                resolved_printer=resolved_printer,
                target_file=str(target_file),
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            return False
        dev_log(
            "windows_raw_print_success",
            requested_printer=printer_name,
            resolved_printer=resolved_printer,
            target_file=str(target_file),
            bytes=len(payload),
            resolve_ms=round(resolve_duration_ms, 1),
            read_ms=round(read_duration_ms, 1),
            open_ms=round(open_duration_ms, 1),
            write_ms=round(write_duration_ms, 1),
        )
        _perf_log(
            "Windows raw print "
            f"printer={resolved_printer} "
            f"file={target_file.name} "
            f"bytes={len(payload)} "
            f"resolve_ms={resolve_duration_ms:.1f} "
            f"read_ms={read_duration_ms:.1f} "
            f"open_ms={open_duration_ms:.1f} "
            f"write_ms={write_duration_ms:.1f}"
        )
        return True

    def _resolve_windows_printer(self, printer_name: str | None) -> str | None:
        queues = self._windows_printer_queues()
        _logger.debug(
            "Resolve Windows printer requested=%s available_queues=%s",
            printer_name or "<none>",
            queues,
        )
        if printer_name and printer_name in queues:
            _logger.debug("Windows printer resolved by exact match printer=%s", printer_name)
            return printer_name
        if printer_name:
            _logger.warning(
                "Strict printer binding rejected missing Windows queue=%s available=%s",
                printer_name,
                queues,
            )
            return None
        default_queue = self._windows_default_queue(queues)
        fallback_queue = queues[0] if queues else None
        resolved = default_queue or fallback_queue
        _logger.debug(
            "Windows printer resolved by fallback default=%s fallback=%s resolved=%s",
            default_queue,
            fallback_queue,
            resolved,
        )
        return resolved

    def _build_escpos_bytes(
        self,
        lines: list[dict[str, Any]],
        cut: bool = True,
        payload: dict[str, Any] | None = None,
        normalize_lines: bool = True,
    ) -> bytes:
        width = self._escpos_line_width()
        encoding, codepage = self._escpos_encoding_config(payload=payload, lines=lines)
        product_header_rendered = False
        is_kitchen_ticket = self._is_kitchen_ticket_lines(lines)
        is_rendered_receipt_image = self._is_rendered_receipt_image_lines(lines)
        chunks = [
            b"\x1b@",
            self._escpos_encoding_init(encoding, codepage, payload),
            b"" if is_kitchen_ticket or is_rendered_receipt_image else self._escpos_line_spacing_command(),
        ]
        rendered_lines_source = self._normalize_receipt_lines(lines) if normalize_lines else lines
        for raw_line in rendered_lines_source:
            if not isinstance(raw_line, dict):
                continue
            if raw_line.get("type") == "job_split":
                split_feed_lines = max(0, int(os.getenv("IOT_ESCPOS_SPLIT_FEED_LINES", "4")))
                chunks.append(b"\n" * split_feed_lines)
                chunks.append(b"\x1dV\x00")
                chunks.append(b"\x1b@")
                chunks.append(self._escpos_encoding_init(encoding, codepage, payload))
                chunks.append(b"" if is_kitchen_ticket or is_rendered_receipt_image else self._escpos_line_spacing_command())
                product_header_rendered = False
                continue
            if raw_line.get("type") == "image":
                image_bytes = self._build_escpos_image(raw_line)
                if image_bytes:
                    image_kind = str(raw_line.get("image_kind") or "")
                    image_align = str(raw_line.get("align") or "center")
                    if image_kind in {"qr", "logo"}:
                        image_align = "center"
                    chunks.append(self._escpos_align(image_align))
                    chunks.append(image_bytes)
                    chunks.append(b"\n")
                    if image_kind == "barcode":
                        image_classes = (
                            [str(cls) for cls in raw_line.get("classes") or []]
                            if isinstance(raw_line.get("classes"), list)
                            else []
                        )
                        if "no-barcode-separator" not in image_classes:
                            chunks.append(self._escpos_align("left"))
                            chunks.append(self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace"))
                            chunks.append(b"\n")
                continue
            if raw_line.get("type") == "header_meta_line":
                product_header_rendered = False
            if raw_line.get("type") == "product_header":
                # structured receipt already carries the product header line;
                # mark it as rendered so product_line does not print it again.
                product_header_rendered = True
            raw_line_classes = (
                [str(cls) for cls in raw_line.get("classes") or []]
                if isinstance(raw_line.get("classes"), list)
                else []
            )
            if raw_line.get("type") == "spacer" and "product-line-spacer" in raw_line_classes:
                continue
            if raw_line.get("type") == "product_line":
                if not is_kitchen_ticket and not product_header_rendered:
                    chunks.append(self._escpos_align("left"))
                    chunks.append(self._escpos_emphasis(True))
                    chunks.append(self._escpos_safe_text(self._build_product_header_text(width), encoding).encode(encoding, errors="replace"))
                    chunks.append(self._escpos_text_newline())
                    chunks.append(self._escpos_emphasis(False))
                    chunks.append(self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace"))
                    chunks.append(self._escpos_text_newline())
                    product_header_rendered = True
                chunks.extend(self._build_product_line_chunks(raw_line, width, encoding))
                continue
            if raw_line.get("type") == "service_info_block":
                chunks.extend(self._build_service_info_chunks(raw_line, width, encoding))
                continue
            rendered_lines = self._render_escpos_lines(raw_line, width)
            if not rendered_lines:
                continue
            chunks.append(self._escpos_align(str(raw_line.get("align") or "left")))
            chunks.append(self._escpos_emphasis(bool(raw_line.get("bold"))))
            width_multiplier, height_multiplier = self._line_size_multipliers(raw_line)
            chunks.append(
                self._escpos_size(
                    width_multiplier,
                    height_multiplier,
                )
            )
            for rendered in rendered_lines:
                chunks.append(self._escpos_safe_text(rendered, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
            chunks.append(self._escpos_emphasis(False))
            chunks.append(self._escpos_size(False, False))
        if not is_rendered_receipt_image:
            chunks.append(b"\n")
        default_feed_lines = "1" if is_rendered_receipt_image else ("4" if is_kitchen_ticket else "10")
        feed_lines = max(0, int(os.getenv("IOT_ESCPOS_FEED_LINES", default_feed_lines)))
        chunks.append(b"\n" * feed_lines)
        if cut:
            chunks.append(b"\x1dV\x00")
        return b"".join(chunks)

    def _render_kitchen_ticket_as_native_image(
        self,
        device: Device,
        lines: list[dict[str, Any]],
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Render kitchen ticket lines as a PIL image and print via Windows GDI.

        This completely bypasses ESC/POS byte generation. Instead, the ticket
        text is rendered as a bitmap image and sent to the Windows printer
        driver through the GDI API (win32ui). This is the "native" printing
        path — the printer driver handles all formatting, font selection, and
        paper handling.

        Returns True if the image was sent to the printer successfully.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            _logger.error("Kitchen native image print failed: PIL not available")
            return False

        # ── Build plain text from kitchen ticket lines ──────────────
        text_lines: list[str] = []
        width = self._escpos_line_width()
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            line_type = str(raw_line.get("type") or "")

            if line_type in {"image", "spacer", "job_split"}:
                continue

            if line_type == "product_line":
                qty = str(raw_line.get("qty") or "").strip()
                name = str(raw_line.get("name") or "").strip()
                total = str(raw_line.get("total") or "").strip()
                combo_items = [
                    str(item).strip()
                    for item in (raw_line.get("combo_items") or [])
                    if str(item).strip()
                ]
                if not qty or not name:
                    continue
                line_text = f"{qty} x {name}"
                if total:
                    line_text += f"  {total}"
                text_lines.append(line_text)
                for combo in combo_items:
                    text_lines.append(f"  + {combo}")
                continue

            if line_type == "header_meta_line":
                left = str(raw_line.get("left_text") or "").strip()
                right = str(raw_line.get("right_text") or "").strip()
                if left and right:
                    gap = max(1, width - len(left) - len(right))
                    text_lines.append(left + (" " * gap) + right)
                else:
                    text = left or right
                    if text:
                        text_lines.append(text)
                continue

            if line_type in {"service_info_block", "product_header"}:
                continue

            text = str(raw_line.get("text") or "").strip()
            if not text:
                continue

            classes = (
                [str(cls) for cls in raw_line.get("classes") or []]
                if isinstance(raw_line.get("classes"), list)
                else []
            )

            if self._is_separator_line(text) and "invoice-asterisk-border" not in classes:
                text_lines.append("-" * width)
                continue

            if "invoice-asterisk-border" in classes:
                continue

            text_lines.append(text)

        if not text_lines:
            _logger.warning("Kitchen ticket native image print skipped: no text content")
            return False

        # ── Render text as PIL image ────────────────────────────────
        # Font selection: try common monospace fonts for clean alignment
        font_size = 22
        font = None
        for font_name in ("cour.ttf", "consola.ttf", "lucon.ttf", "CascadiaMono.ttf"):
            try:
                font_path = f"C:\\Windows\\Fonts\\{font_name}"
                font = ImageFont.truetype(font_path, font_size)
                break
            except (IOError, OSError):
                continue
        if font is None:
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
            except (IOError, OSError):
                font = ImageFont.load_default()

        # Calculate character cell size for layout
        sample_char = "W"
        try:
            bbox = font.getbbox(sample_char)
            char_width = bbox[2] - bbox[0]
            char_height = bbox[3] - bbox[1]
        except AttributeError:
            try:
                char_width, char_height = font.getsize(sample_char)
            except AttributeError:
                char_width = font_size
                char_height = font_size

        line_spacing = max(char_height + 4, int(font_size * 1.25))
        margin = 24
        image_width = max(320, char_width * width + margin * 2)
        image_height = max(100, len(text_lines) * line_spacing + margin * 2)

        img = Image.new("RGB", (image_width, image_height), "white")
        draw = ImageDraw.Draw(img)

        y = margin
        for text_line in text_lines:
            draw.text((margin, y), text_line, fill="black", font=font)
            y += line_spacing

        # ── Convert to ESC/POS raster and send as raw data ──────────
        # Thermal receipt printers (like COCINA) do NOT support GDI-based
        # printing (win32ui.CreateDC). They expect raw ESC/POS data.
        # We convert the PIL image to ESC/POS raster format and send it
        # via the raw Windows print spooler (win32print.WritePrinter).
        _logger.info(
            "Kitchen ticket rendered as image lines=%s image=%sx%s char_width=%s char_height=%s",
            len(text_lines),
            image_width,
            image_height,
            char_width,
            char_height,
        )
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        target = self.spool_dir / f"native_receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.png"
        try:
            img.save(target, format="PNG")
        except Exception:
            pass
        # Convert PIL image to 1-bit black/white for ESC/POS raster
        bw_img = img.convert("1")
        escpos_bytes = self._image_to_escpos_raster(bw_img)
        escpos_bytes += b"\n" * 4 + (b"\x1dV\x00" if payload and payload.get("cut", True) else b"")
        spool_target = self.spool_dir / f"receipt_{int(time() * 1000)}_{uuid4().hex[:8]}.bin"
        try:
            spool_target.write_bytes(escpos_bytes)
        except Exception:
            pass
        return self._send_raw_to_printer(device, spool_target)

    def _build_kitchen_from_order_data(self, order_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Build kitchen ticket lines from native Odoo getOrderData() format.

        This handles the data format produced by the Odoo POS Restaurant
        module's OrderChangeReceipt template rendering.

        Product line font size is controlled by the kitchen printer's
        ``kitchen_font_mode`` configuration field (normal/double_width/
        double_height), passed via order_data from the POS JS.
        """
        lines: list[dict[str, Any]] = []

        # Read kitchen_font_mode from the printer configuration
        # Values: 'normal' (default), 'double_width', 'double_height'
        kitchen_font_mode = str(order_data.get("kitchen_font_mode") or "normal").strip().lower()
        if kitchen_font_mode == "double_width":
            product_dw: Any = True
            product_dh: Any = False
        elif kitchen_font_mode == "double_height":
            product_dw = False
            product_dh = True
        else:
            product_dw = False
            product_dh = False
        _logger.info(
            "Kitchen font mode=%s product_double_width=%s product_double_height=%s",
            kitchen_font_mode, product_dw, product_dh,
        )

        # Kitchen ticket header: order #, table, type, status
        # Each on its own line, double width/height bold
        tracking = str(order_data.get("tracking_number") or "").strip()
        order_ref = f"#{tracking}" if tracking else ""

        table_name = str(order_data.get("table_name") or "").strip()
        table_number = str(order_data.get("table_number") or "").strip()
        # If table name is just a number, prefix with "MESA "
        if table_name and table_name.isdigit():
            table_display = f"MESA {table_name}"
        elif table_number and table_number.isdigit():
            table_display = f"MESA {table_number}"
        else:
            table_display = (table_name or table_number or "").upper()

        preset_name = str(order_data.get("preset_name") or "DINE IN").strip()
        change_title = str(order_data.get("changes", {}).get("title") or "NEW").strip()

        # Row 1: Order number
        if order_ref:
            lines.append({
                "text": order_ref,
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })

        lines.append({"type": "vspace"})

        # Row 2: Table number
        if table_display:
            lines.append({
                "text": table_display,
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })

        lines.append({"type": "vspace"})

        # Row 3: Order type
        if preset_name:
            lines.append({
                "text": preset_name.upper(),
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })

        lines.append({"type": "vspace"})

        # Row 4: Order status
        if change_title:
            lines.append({
                "text": change_title.upper(),
                "align": "center", "bold": True,
                "double_width": True, "double_height": True,
            })

        lines.append({"text": "-" * 48, "align": "left"})

        # Product lines from changes.data, respecting course grouping
        grouped_data = order_data.get("changes", {}).get("groupedData") or []
        changes_data = order_data.get("changes", {}).get("data") or []

        def _render_item(item):
            if not isinstance(item, dict):
                return
            qty = str(item.get("quantity") or item.get("qty") or "1")
            name = str(item.get("basic_name") or item.get("name") or "").strip()
            if not name:
                return
            lines.append({
                "type": "product_line",
                "qty": qty,
                "name": name,
                "total": "",
                "double_width": product_dw,
                "double_height": product_dh,
                "classes": ["kitchen-product-line"],
            })
            # Attributes
            attr_names = item.get("attribute_value_names") or []
            if isinstance(attr_names, list):
                for attr in attr_names:
                    lines.append({
                        "text": f"  + {attr}",
                        "align": "left",
                        "classes": ["kitchen-note"],
                    })
            # Note
            note = str(item.get("note") or "").strip()
            if note:
                lines.append({
                    "text": f"  Note: {note}",
                    "align": "left", "bold": True,
                    "classes": ["kitchen-note"],
                })
            # Customer note
            cnote = str(item.get("customer_note") or "").strip()
            if cnote:
                lines.append({
                    "text": f"  Note: {cnote}",
                    "align": "left", "bold": True,
                    "classes": ["kitchen-note"],
                })

        if grouped_data:
            # Render products grouped by course
            for group in grouped_data:
                if not isinstance(group, dict):
                    continue
                group_name = str(group.get("name") or "").strip()
                if group_name:
                    lines.append({
                        "text": f"** {group_name} **",
                        "align": "center", "bold": True,
                        "double_width": True, "double_height": True,
                    })
                for item in (group.get("data") or []):
                    _render_item(item)
        else:
            # No course grouping, render flat product list
            for item in changes_data:
                _render_item(item)

        lines.append({"text": "-" * 48, "align": "left"})

        # Config name (shop/restaurant)
        config_name = str(order_data.get("config_name") or "").strip()
        if config_name:
            lines.append({"text": config_name, "align": "center"})

        # Time
        time_text = str(order_data.get("time") or "").strip()
        if time_text:
            lines.append({"text": time_text, "align": "center"})

        _logger.info(
            "Built kitchen ticket from order_data lines=%s preset=%s changes_title=%s items=%s",
            len(lines), preset_name, change_title, len(changes_data),
        )
        return lines

    def _build_kitchen_escpos_bytes(
        self,
        lines: list[dict[str, Any]],
        cut: bool = True,
        payload: dict[str, Any] | None = None,
    ) -> bytes:
        """Build ESC/POS bytes for kitchen tickets with formatting.

        Supports alignment, bold, and double-width/height for title,
        order type, table number, and other emphasized lines. Images
        (logos, QR codes, barcodes) are dropped. Product lines use
        the plain kitchen format (qty x name).
        """
        width = self._escpos_line_width()
        encoding, codepage = self._escpos_encoding_config(payload=payload, lines=lines)
        chunks: list[bytes] = [
            b"\x1b@",
            self._escpos_encoding_init(encoding, codepage, payload),
        ]
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            line_type = str(raw_line.get("type") or "")

            if line_type in {"image", "spacer", "job_split"}:
                continue

            if line_type == "vspace":
                chunks.append(b"\n")
                continue

            if line_type == "product_line":
                qty = str(raw_line.get("qty") or "").strip()
                name = str(raw_line.get("name") or "").strip()
                total = str(raw_line.get("total") or "").strip()
                combo_items = [
                    str(item).strip()
                    for item in (raw_line.get("combo_items") or [])
                    if str(item).strip()
                ]
                if not qty or not name:
                    continue
                line_text = f"{qty} x {name}"
                if total:
                    line_text += f"  {total}"
                dw = bool(raw_line.get("double_width"))
                dh = bool(raw_line.get("double_height"))
                chunks.append(self._escpos_align("left"))
                chunks.append(self._escpos_emphasis(True))
                if dw or dh:
                    chunks.append(self._escpos_size(dw, dh))
                chunks.append(
                    self._escpos_safe_text(line_text, encoding).encode(encoding, errors="replace")
                )
                if dw or dh:
                    chunks.append(self._escpos_size(False, False))
                chunks.append(self._escpos_emphasis(False))
                chunks.append(b"\n")
                for combo in combo_items:
                    chunks.append(self._escpos_align("left"))
                    chunks.append(
                        self._escpos_safe_text(f"  - {combo}", encoding).encode(encoding, errors="replace")
                    )
                    chunks.append(b"\n")
                continue

            if line_type == "header_meta_line":
                left = str(raw_line.get("left_text") or "").strip()
                right = str(raw_line.get("right_text") or "").strip()
                if left and right:
                    gap = max(1, width - len(left) - len(right))
                    text = left + (" " * gap) + right
                else:
                    text = left or right
                if text:
                    is_bold = bool(raw_line.get("bold"))
                    dw = bool(raw_line.get("double_width"))
                    dh = bool(raw_line.get("double_height"))
                    chunks.append(self._escpos_align("center"))
                    chunks.append(self._escpos_emphasis(is_bold))
                    if dw or dh:
                        chunks.append(self._escpos_size(dw, dh))
                    chunks.append(
                        self._escpos_safe_text(text, encoding).encode(encoding, errors="replace")
                    )
                    if dw or dh:
                        chunks.append(self._escpos_size(False, False))
                    chunks.append(self._escpos_emphasis(False))
                    chunks.append(b"\n")
                continue

            if line_type == "service_info_block":
                table_text = str(raw_line.get("table_text") or "").strip()
                guests_text = str(raw_line.get("guests_text") or "").strip()
                served_by_text = str(raw_line.get("served_by_text") or "").strip()
                for text in (table_text, guests_text, served_by_text):
                    if text:
                        chunks.append(self._escpos_align("center"))
                        chunks.append(self._escpos_emphasis(True))
                        chunks.append(
                            self._escpos_safe_text(text, encoding).encode(encoding, errors="replace")
                        )
                        chunks.append(self._escpos_emphasis(False))
                        chunks.append(b"\n")
                continue

            if line_type == "product_header":
                continue

            text = str(raw_line.get("text") or "").strip()
            if not text:
                continue

            classes = (
                [str(cls) for cls in raw_line.get("classes") or []]
                if isinstance(raw_line.get("classes"), list)
                else []
            )

            if self._is_separator_line(text) and "invoice-asterisk-border" not in classes:
                chunks.append(self._escpos_align("left"))
                chunks.append(
                    self._escpos_safe_text("-" * width, encoding).encode(encoding, errors="replace")
                )
                chunks.append(b"\n")
                continue

            if "invoice-asterisk-border" in classes:
                continue

            # Apply formatting for regular text lines
            align = str(raw_line.get("align") or "left")
            is_bold = bool(raw_line.get("bold"))
            dw = bool(raw_line.get("double_width"))
            dh = bool(raw_line.get("double_height"))

            chunks.append(self._escpos_align(align))
            chunks.append(self._escpos_emphasis(is_bold))
            if dw or dh:
                chunks.append(self._escpos_size(dw, dh))
            chunks.append(
                self._escpos_safe_text(text, encoding).encode(encoding, errors="replace")
            )
            if dw or dh:
                chunks.append(self._escpos_size(False, False))
            chunks.append(self._escpos_emphasis(False))
            chunks.append(b"\n")

        default_feed_lines = "4"
        feed_lines = max(0, int(os.getenv("IOT_ESCPOS_FEED_LINES", default_feed_lines)))
        chunks.append(b"\n" * feed_lines)
        if cut:
            chunks.append(b"\x1dV\x00")
        return b"".join(chunks)

    def _escpos_encoding_init(
        self,
        encoding: str,
        codepage: int,
        payload: dict[str, Any] | None = None,
    ) -> bytes:
        if encoding == "utf-8":
            return b""
        if encoding == "gb18030":
            payload = payload or {}
            chinese_mode = str(
                payload.get("cn_init_mode")
                or os.getenv("IOT_ESCPOS_CN_INIT_MODE", "esc_r_15_esc_t_255")
            ).strip().lower()
            if chinese_mode == "fs_amp":
                return b"\x1c&"
            if chinese_mode == "esc_t_255_fs_amp":
                return b"\x1bt\xff\x1c&"
            if chinese_mode == "esc_r_15_esc_t_255":
                return b"\x1bR\x0f\x1bt\xff"
            return b"\x1bt\xff"
        return bytes([0x1B, 0x74, codepage])

    def _escpos_line_spacing_command(self) -> bytes:
        return bytes([0x1B, 0x33, 48])

    def _escpos_text_newline(self) -> bytes:
        return b"\n"

    def _build_product_line_chunks(self, line: dict[str, Any], width: int, encoding: str) -> list[bytes]:
        qty = str(line.get("qty") or "").strip()
        product_name = str(line.get("name") or "").strip()
        unit_price = str(line.get("unit_price") or "").strip()
        total = str(line.get("total") or "").strip()
        discount_text = str(line.get("discount_text") or "").strip()
        original_total = str(line.get("original_total") or "").strip()
        combo_items = [str(item).strip() for item in (line.get("combo_items") or []) if str(item).strip()]
        if not qty or not product_name:
            return []
        classes = [str(item) for item in (line.get("classes") or [])]
        is_kitchen = "kitchen-product-line" in classes
        if total:
            qty_width, total_width, name_width = self._receipt_column_layout(width)
        else:
            qty_width = max(2, min(3, len(qty)))
            total_width = 0
            name_width = max(8, width - qty_width - 1)
        name_lines = self._wrap_text(product_name, name_width) or [""]
        first_line_prefix = self._pad_right(qty, qty_width) if is_kitchen or not total else self._pad_center(qty, qty_width)
        if total:
            first_line_suffix = " " + self._pad_right(name_lines[0], name_width) + " " + self._pad_left(total, total_width)
        else:
            first_line_suffix = " " + self._pad_right(name_lines[0], name_width)

        chunks = [
            self._escpos_align("left"),
            self._escpos_emphasis(True),
            self._escpos_size(*self._line_size_multipliers(line)),
            self._escpos_safe_text(first_line_prefix, encoding).encode(encoding, errors="replace"),
            self._escpos_emphasis(False),
            self._escpos_safe_text(first_line_suffix, encoding).encode(encoding, errors="replace"),
            self._escpos_text_newline(),
        ]
        for extra_name in name_lines[1:]:
            continuation_line = " " * (qty_width + 1) + self._pad_right(extra_name, name_width)
            if total:
                continuation_line += " " + " " * total_width
            chunks.append(self._escpos_safe_text(continuation_line, encoding).encode(encoding, errors="replace"))
            chunks.append(self._escpos_text_newline())
        if combo_items:
            combo_indent = " " * 3
            combo_width = max(8, name_width - len(combo_indent))
            for combo_item in combo_items:
                combo_rows = self._wrap_text(combo_item, combo_width) or [combo_item]
                for row in combo_rows:
                    combo_line = " " * (qty_width + 1) + self._pad_right(combo_indent + row, name_width)
                    if total:
                        combo_line += " " + " " * total_width
                    chunks.append(self._escpos_safe_text(combo_line, encoding).encode(encoding, errors="replace"))
                    chunks.append(self._escpos_text_newline())
        if discount_text:
            discount_label = self._format_discount_text(discount_text, total, original_total)
            discount_rows = self._wrap_text(discount_label, name_width) or [discount_label]
            for row in discount_rows:
                discount_line = (
                    " " * (qty_width + 1)
                    + self._pad_right(row, name_width)
                    + " "
                    + " " * total_width
                )
                chunks.append(self._escpos_safe_text(discount_line, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
        chunks.append(self._escpos_size(False, False))
        return chunks

    def _build_service_info_chunks(self, line: dict[str, Any], width: int, encoding: str) -> list[bytes]:
        table_text = str(line.get("table_text") or "").strip()
        guests_text = str(line.get("guests_text") or "").strip()
        served_by_text = str(line.get("served_by_text") or "").strip()
        if not table_text:
            return []

        chunks: list[bytes] = []

        chunks.append(self._escpos_align("center"))
        chunks.append(self._escpos_emphasis(True))
        chunks.append(self._escpos_size(True))
        for row in self._wrap_text(table_text, max(16, width // 2)) or [table_text]:
            chunks.append(self._escpos_safe_text(row, encoding).encode(encoding, errors="replace"))
            chunks.append(self._escpos_text_newline())
        chunks.append(self._escpos_emphasis(False))
        chunks.append(self._escpos_size(False))

        if guests_text:
            chunks.append(self._escpos_align("center"))
            for row in self._wrap_text(guests_text, width) or [guests_text]:
                chunks.append(self._escpos_safe_text(row, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
        if served_by_text:
            chunks.append(self._escpos_align("center"))
            for row in self._wrap_text(served_by_text, width) or [served_by_text]:
                chunks.append(self._escpos_safe_text(row, encoding).encode(encoding, errors="replace"))
                chunks.append(self._escpos_text_newline())
        return chunks

    def _normalize_receipt_lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        discount_total = Decimal("0")
        index = 0
        while index < len(lines):
            line = self._normalize_receipt_line_text(lines[index])
            if self._should_skip_ticket_prefix_line(line):
                index += 1
                continue
            if isinstance(line, dict) and str(line.get("type") or "") == "product_line":
                normalized.append(line)
                discount_total += self._product_discount_amount(line)
                index += 1
                continue
            if isinstance(line, dict) and str(line.get("type") or "") == "spacer":
                normalized.append(line)
                index += 1
                continue
            gift_card_block = self._consume_gift_card_section(lines, index)
            if gift_card_block:
                merged_lines, next_index = gift_card_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            invoice_block = self._consume_invoice_section(lines, index)
            if invoice_block:
                merged_lines, next_index = invoice_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            separator_block = self._prepend_separator_for_reference(lines, index)
            if separator_block:
                merged_lines, next_index = separator_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            header_block = self._consume_header_block(lines, index)
            if header_block:
                merged_lines, next_index = header_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            header_meta_block = self._consume_header_meta_pair(lines, index)
            if header_meta_block:
                merged_lines, next_index = header_meta_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            customer_block = self._consume_customer_block(lines, index)
            if customer_block:
                merged_lines, next_index = customer_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            centered_customer_block = self._consume_centered_customer_block(lines, index)
            if centered_customer_block:
                merged_lines, next_index = centered_customer_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            service_block = self._consume_service_info_block(lines, index)
            if service_block:
                merged_line, next_index = service_block
                normalized.append(merged_line)
                index = next_index
                continue
            change_block = self._consume_change_line(lines, index)
            if change_block:
                merged_line, next_index = change_block
                normalized.append(merged_line)
                index = next_index
                continue
            total_can_skip = self._skip_total_can_block(lines, index)
            if total_can_skip:
                index = total_can_skip
                continue
            summary_block = self._consume_summary_amount_line(lines, index)
            if summary_block:
                merged_line, next_index = summary_block
                normalized.append(merged_line)
                index = next_index
                continue
            label_amount_block = self._consume_label_amount_line(lines, index, discount_total)
            if label_amount_block:
                merged_line, next_index = label_amount_block
                normalized.append(merged_line)
                index = next_index
                continue
            total_block = self._consume_emphasized_total(lines, index)
            if total_block:
                merged_lines, next_index = total_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            duplicate_skip = self._skip_duplicate_summary(lines, index)
            if duplicate_skip:
                index = duplicate_skip
                continue
            payment_terminal_block = self._consume_payment_terminal_receipt(lines, index)
            if payment_terminal_block:
                merged_lines, next_index = payment_terminal_block
                normalized.extend(merged_lines)
                index = next_index
                continue
            product_block = self._consume_product_block(lines, index)
            if product_block:
                merged_line, next_index = product_block
                normalized.append(merged_line)
                discount_total += self._product_discount_amount(merged_line)
                index = next_index
                continue
            kitchen_product_block = self._consume_kitchen_product_line(lines, index)
            if kitchen_product_block:
                merged_line, next_index = kitchen_product_block
                normalized.append(merged_line)
                index = next_index
                continue
            tracking_block = self._consume_tracking_number_line(lines, index)
            if tracking_block:
                normalized.append(tracking_block)
                index += 1
                continue
            if self._should_skip_orphan_weight_fragment(lines, index, normalized):
                index += 1
                continue
            normalized.append(line)
            index += 1
        normalized = self._ensure_discount_summary_line(normalized, discount_total)
        normalized = self._remove_discount_adjacent_separators(normalized)
        deduped: list[dict[str, Any]] = []
        previous_gift_card_title = False
        seen_receipt_code_lines: set[str] = set()
        for item in normalized:
            item_classes = [str(cls) for cls in item.get("classes") or []] if isinstance(item, dict) and isinstance(item.get("classes"), list) else []
            is_gift_card_title = isinstance(item, dict) and "gift-card-title" in item_classes
            if is_gift_card_title and previous_gift_card_title:
                continue
            if self._is_invoice_prompt_line(item):
                continue
            receipt_code_key = self._receipt_code_line_key(item)
            if receipt_code_key:
                if receipt_code_key in seen_receipt_code_lines:
                    continue
                seen_receipt_code_lines.add(receipt_code_key)
            deduped.append(item)
            previous_gift_card_title = is_gift_card_title
        return deduped

    def _normalize_receipt_line_text(self, value: Any):
        if isinstance(value, dict):
            normalized = {key: self._normalize_receipt_line_text(item) for key, item in value.items()}
            return self._normalize_print_line_content(normalized)
        if isinstance(value, list):
            return [self._normalize_receipt_line_text(item) for item in value]
        if isinstance(value, str):
            return self._normalize_print_text(str(value))
        return value

    def _normalize_print_line_content(self, line: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(line)
        line_type = str(normalized.get("type") or "").strip().lower()
        classes = (
            [str(cls).strip().lower() for cls in normalized.get("classes") or []]
            if isinstance(normalized.get("classes"), list)
            else []
        )

        total_like_line = False
        if "receipt-total" in classes or "receipt-total-emphasized" in classes or "label-total" in classes:
            total_like_line = True
        if line_type == "header_meta_line":
            left_text = str(normalized.get("left_text") or "").strip().lower()
            total_like_line = left_text.startswith("total")
        elif line_type != "product_line":
            text = str(normalized.get("text") or "").strip().lower()
            total_like_line = text.startswith("total")

        for key in ("text", "name", "left_text", "right_text", "qty", "total", "unit_price", "original_total"):
            value = normalized.get(key)
            if not isinstance(value, str) or not value:
                continue
            cleaned = self._normalize_print_text(value)
            if key in {"right_text", "total"} and total_like_line:
                amount = self._parse_decimal(cleaned)
                normalized[key] = self._format_amount_like("0.00 Eur", amount) if amount is not None else cleaned.replace("€", "Eur")
            else:
                normalized[key] = self._strip_currency_symbol(cleaned)
        return normalized

    def _normalize_print_text(self, text: str) -> str:
        normalized = self._repair_receipt_mojibake(str(text or ""))
        normalized = self._normalize_spanish_text(normalized)
        if normalized.strip().lower().startswith("table "):
            table_value = normalized.strip()[6:].strip()
            return f"MESA {table_value}".strip().upper()
        return normalized

    def _receipt_code_line_key(self, line: Any) -> str:
        if not isinstance(line, dict):
            return ""
        text = str(line.get("text") or "").strip()
        if not text:
            return ""
        normalized = self._normalize_print_text(text).strip().lower()
        normalized = normalized.replace("c贸digo", "codigo").replace("código", "codigo")
        normalized = re.sub(r"\s+", " ", normalized)
        match = re.search(r"\b(?:codigo|code)\s*:\s*([a-z0-9_-]+)\b", normalized)
        return f"codigo:{match.group(1).lower()}" if match else ""

    def _is_invoice_prompt_line(self, line: Any) -> bool:
        if not isinstance(line, dict):
            return False
        text = str(line.get("text") or "").strip()
        if not text:
            return False
        normalized = self._normalize_print_text(text).strip().lower()
        normalized = re.sub(r"\s+", " ", normalized)
        return (
            "need an invoice" in normalized
            or ("necesita" in normalized and "factura" in normalized)
        )

    def _ensure_receipt_qr_line(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        has_qr = any(
            isinstance(line, dict)
            and str(line.get("type") or "") == "image"
            and str(line.get("image_kind") or "").lower() == "qr"
            for line in lines
        )
        if has_qr:
            return lines

        ticket_url = ""
        insert_at = len(lines)
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()
            if text.startswith(("http://", "https://")) and "/pos/ticket" in text:
                ticket_url = text
                insert_at = index
                break
        if not ticket_url:
            return lines

        qr_size = max(180, int(os.getenv("IOT_PORTAL_QR_SIZE", "200") or "200"))
        qr_line = {
            "type": "image",
            "src": f"/report/barcode?{urlencode({'barcode_type': 'QR', 'value': ticket_url, 'width': qr_size, 'height': qr_size})}",
            "align": "center",
            "classes": ["portal-qr", "auto-receipt-qr"],
            "width": qr_size,
            "height": qr_size,
            "image_kind": "qr",
        }
        _logger.info("Inserted missing receipt QR image for ticket_url=%s", ticket_url)
        return lines[:insert_at] + [qr_line] + lines[insert_at:]

    def _strip_currency_symbol(self, text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"(?i)\bEUR\b", "", cleaned)
        cleaned = cleaned.replace("€", "").replace("$", "")
        return re.sub(r"\s{2,}", " ", cleaned).strip()

    def _ensure_discount_summary_line(self, lines: list[dict[str, Any]], discount_total: Decimal) -> list[dict[str, Any]]:
        if discount_total <= 0:
            return lines

        # If any product line already carries its own discount description
        # (e.g. "50% de descuento en 540,87"), skip the aggregated summary
        # line to avoid duplication and wrong-format amounts.
        for line in lines:
            if isinstance(line, dict) and str(line.get("type") or "") == "product_line":
                if str(line.get("discount_text") or "").strip():
                    return lines

        for line in lines:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip().lower()
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            if "label-discount" in classes or text.startswith("discount"):
                return lines

        discount_line = {
            "type": "header_meta_line",
            "left_text": "Discount",
            "right_text": f"-{self._format_amount_like('$ 0.00', discount_total)}",
            "align": "left",
            "classes": ["label-discount"],
        }

        insert_at = len(lines)
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            text = str(line.get("text") or "").strip().lower()
            if "receipt-total-emphasized" in classes or text.startswith("total "):
                insert_at = index
                break

        if insert_at > 0:
            previous = lines[insert_at - 1]
            if isinstance(previous, dict):
                previous_text = str(previous.get("text") or "").strip()
                previous_classes = (
                    [str(cls) for cls in previous.get("classes") or []]
                    if isinstance(previous.get("classes"), list)
                    else []
                )
                if "receipt-separator" in previous_classes or self._is_separator_line(previous_text):
                    insert_at -= 1

        updated = list(lines)
        updated.insert(insert_at, discount_line)
        return updated

    def _remove_discount_adjacent_separators(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        total_lines = len(lines)
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                cleaned.append(line)
                continue
            classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
            text = str(line.get("text") or "").strip()
            is_separator = "receipt-separator" in classes or self._is_separator_line(text)
            if not is_separator:
                cleaned.append(line)
                continue

            previous_line = lines[index - 1] if index > 0 else None
            next_line = lines[index + 1] if index + 1 < total_lines else None
            previous_classes = (
                [str(cls) for cls in previous_line.get("classes") or []]
                if isinstance(previous_line, dict) and isinstance(previous_line.get("classes"), list)
                else []
            )
            next_classes = (
                [str(cls) for cls in next_line.get("classes") or []]
                if isinstance(next_line, dict) and isinstance(next_line.get("classes"), list)
                else []
            )
            if "label-discount" in previous_classes or "label-discount" in next_classes:
                continue
            cleaned.append(line)
        return cleaned

    def _consume_gift_card_section(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if {"gift-card-title", "gift-card-code", "gift-card-amount", "gift-card-qr", "gift-card-barcode"}.intersection(classes):
            return None
        if text.lower() != "gift card":
            return None

        code_line = lines[start + 1] if start + 1 < len(lines) else None
        qr_line = lines[start + 2] if start + 2 < len(lines) else None
        amount_line = lines[start + 3] if start + 3 < len(lines) else None
        if not all(isinstance(item, dict) for item in [code_line, qr_line]):
            return None
        if str(qr_line.get("type") or "") != "image" or str(qr_line.get("image_kind") or "") not in {"qr", "barcode"}:
            return None

        merged_lines = [
            {**line, "align": "center", "bold": True},
            {**code_line, "align": "center"},
            {**qr_line, "align": "center"},
        ]
        next_index = start + 3
        if isinstance(amount_line, dict):
            amount_text = self._normalize_gift_card_amount(str(amount_line.get("text") or "").strip())
            if amount_text and self._looks_like_amount(amount_text):
                merged_lines.append(
                    {
                        "text": amount_text,
                        "align": "center",
                        "bold": True,
                        "double_width": True,
                        "classes": ["gift-card-amount"],
                    }
                )
                next_index = start + 4
        return merged_lines, next_index

    def _normalize_gift_card_amount(self, text: str) -> str:
        amount = self._extract_amount(text)
        return amount or text.strip()

    def _gift_card_display_amount(self, loyalty_card: dict[str, Any]) -> tuple[str, str]:
        preferred_fields = (
            "point",
            "balance",
            "remaining_balance",
            "current_balance",
            "available_balance",
            "remaining_amount",
            "balance_amount",
            "amount",
        )
        for field_name in preferred_fields:
            raw_value = loyalty_card.get(field_name)
            text = str(raw_value or "").strip()
            if not text:
                continue
            if field_name == "point":
                normalized_number = self._normalize_gift_card_amount(text)
                if normalized_number:
                    return field_name, self._format_amount_like("$ 0.00", self._parse_decimal(normalized_number) or Decimal("0"))
            normalized = self._normalize_gift_card_amount(text)
            if normalized:
                return field_name, normalized
        return "", ""

    def _consume_header_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "ticket-name-prefix" not in classes:
            return None

        ref_line = lines[start + 1] if start + 1 < len(lines) else None
        date_line = lines[start + 2] if start + 2 < len(lines) else None
        served_line = lines[start + 3] if start + 3 < len(lines) else None
        barcode_line = lines[start + 4] if start + 4 < len(lines) else None
        if not all(isinstance(item, dict) for item in [ref_line, date_line, served_line, barcode_line]):
            return None
        if str(barcode_line.get("type") or "") != "image" or str(barcode_line.get("image_kind") or "") != "barcode":
            return None

        ref_text = str(ref_line.get("text") or "").strip()
        date_text = str(date_line.get("text") or "").strip()
        served_text = str(served_line.get("text") or "").strip()
        ticket_text = str(line.get("text") or "").strip()
        if not ref_text or not date_text or not served_text:
            return None

        return (
            [
                {**line, "align": "center"},
                {**ref_line, "align": "center"},
                {**barcode_line, "align": "center"},
                {
                    "type": "header_meta_line",
                    "left_text": date_text,
                    "right_text": served_text,
                },
            ],
            start + 5,
        )

    def _consume_header_meta_pair(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        date_line = lines[start]
        served_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(date_line, dict) or not isinstance(served_line, dict):
            return None

        date_text = str(date_line.get("text") or "").strip()
        served_text = str(served_line.get("text") or "").strip()
        if not self._looks_like_header_date_line(date_text):
            return None
        if not self._parse_served_by_line(served_text):
            return None

        return (
            [
                {
                    "type": "header_meta_line",
                    "left_text": date_text,
                    "right_text": served_text,
                }
            ],
            start + 2,
        )

    def _consume_service_info_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "pos-receipt-contact" not in classes:
            return None

        text = str(line.get("text") or "").strip()
        served_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(served_line, dict):
            return None
        served_text = str(served_line.get("text") or "").strip()

        table_text, table_value, guests_value = self._parse_service_contact_line(text)
        served_value = self._parse_served_by_line(served_text)
        if not table_text or not table_value or not guests_value or not served_value:
            return None

        return (
            {
                "type": "service_info_block",
                "table_text": table_text,
                "guests_text": f"Guests: {guests_value}",
                "served_by_text": f"Served by: {served_value}",
            },
            start + 2,
        )

    def _consume_customer_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        if line.get("type") == "image":
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if not text or classes or line.get("align") != "left":
            return None
        if self._is_separator_line(text):
            return None
        if self._is_orphan_weight_fragment(text):
            return None
        if self._looks_like_amount(text) or self._looks_like_tax_summary_line(text) or text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "pos:", "time:")):
            return None

        collected: list[dict[str, Any]] = []
        index = start
        while index < len(lines):
            candidate = lines[index]
            if not isinstance(candidate, dict):
                break
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if (
                not candidate_text
                or candidate_classes
                or candidate.get("align") != "left"
                or candidate.get("type") == "image"
                or self._is_separator_line(candidate_text)
                or self._is_orphan_weight_fragment(candidate_text)
                or self._looks_like_amount(candidate_text)
                or self._looks_like_tax_summary_line(candidate_text)
                or candidate_text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "served by:", "pos:", "time:"))
                or candidate_text.isdigit()
            ):
                break
            collected.append({**candidate, "align": "center"})
            index += 1

        if not collected:
            return None

        merged: list[dict[str, Any]] = [
            {"text": "=" * self._escpos_line_width(), "align": "left", "classes": ["receipt-separator"]},
            *collected,
            {"text": "", "align": "left", "classes": ["customer-spacer"]},
            {"text": "=" * self._escpos_line_width(), "align": "left", "classes": ["receipt-separator"]},
        ]
        return merged, index

    def _consume_centered_customer_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        if line.get("type") == "image":
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if not text or classes or line.get("align") != "center":
            return None
        if self._is_separator_line(text):
            return None
        if self._looks_like_amount(text) or self._looks_like_tax_summary_line(text) or text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "served by:", "pos:", "time:")):
            return None

        if start <= 0:
            return None

        previous = lines[start - 1]
        if not isinstance(previous, dict):
            return None

        previous_text = str(previous.get("text") or "").strip().lower()
        previous_classes = (
            [str(cls) for cls in previous.get("classes") or []]
            if isinstance(previous.get("classes"), list)
            else []
        )
        if (
            "qty" in previous_classes
            or "product-price" in previous_classes
            or self._looks_like_amount(previous_text)
            or not previous_text.startswith("served by:")
        ):
            return None

        collected: list[dict[str, Any]] = []
        index = start
        while index < len(lines):
            candidate = lines[index]
            if not isinstance(candidate, dict):
                break
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if (
                not candidate_text
                or candidate_classes
                or candidate.get("type") == "image"
                or candidate.get("align") != "center"
                or self._is_separator_line(candidate_text)
                or self._looks_like_amount(candidate_text)
                or self._looks_like_tax_summary_line(candidate_text)
                or candidate_text.lower().startswith(("subtotal", "tax", "total", "change", "code:", "served by:", "pos:", "time:"))
            ):
                break
            collected.append(candidate)
            index += 1

        if not collected:
            return None

        merged: list[dict[str, Any]] = [
            *collected,
            {"text": "", "align": "left", "classes": ["customer-spacer"]},
            {"text": "=" * self._escpos_line_width(), "align": "left", "classes": ["receipt-separator"]},
        ]
        return merged, index

    def _consume_change_line(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if "receipt-change" not in classes or not text:
            return None
        amount = self._extract_signed_amount(text)
        label = text
        if amount:
            label = text[: text.rfind(amount)].strip().rstrip("-").strip()
        next_index = start + 1
        for candidate_index in range(start + 1, min(len(lines), start + 4)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip().lower()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if candidate_text.startswith("change") or "label-change" in candidate_classes or "pos-receipt-right-align" in candidate_classes:
                next_index = candidate_index + 1
        return (
            {
                "text": f"{label}  {amount}".strip(),
                "align": "left",
                "classes": ["merged-label-amount", "label-change"],
            },
            next_index,
        )

    def _consume_tracking_number_line(self, lines: list[dict[str, Any]], start: int) -> dict[str, Any] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "tracking-number" not in classes:
            return None
        return {**line, "align": "center"}

    def _consume_payment_terminal_receipt(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if not {"payment-terminal-line", "pos-payment-terminal-receipt"}.intersection(classes):
            return None

        text = str(line.get("text") or "").strip()
        if not text:
            return None

        next_index = start + 1
        merged_terminal_lines: list[str] = []
        split_lines = self._split_payment_terminal_receipt_text(text)
        if split_lines:
            merged_terminal_lines.extend(split_lines)

        while next_index < len(lines):
            candidate = lines[next_index]
            if not isinstance(candidate, dict):
                break
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if not {"payment-terminal-line", "pos-payment-terminal-receipt"}.intersection(candidate_classes):
                break
            candidate_text = str(candidate.get("text") or "").strip()
            if candidate_text:
                merged_terminal_lines.extend(self._split_payment_terminal_receipt_text(candidate_text))
            next_index += 1

        if not merged_terminal_lines:
            return None

        deduped_terminal_lines: list[str] = []
        seen: set[str] = set()
        for item in merged_terminal_lines:
            normalized = re.sub(r"\s+", " ", str(item or "")).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped_terminal_lines.append(normalized)

        normalized_lines: list[dict[str, Any]] = []
        normalized_lines.extend([
            {
                **line,
                "text": item,
                "align": "center",
                "classes": [*classes, "payment-terminal-detail"],
            }
            for item in deduped_terminal_lines
        ])
        normalized_lines.append({"text": "", "align": "left", "classes": ["receipt-spacer", "payment-terminal-spacer"]})
        return normalized_lines, next_index

    def _consume_summary_amount_line(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if not text:
            return None
        if "merged-label-amount" in classes:
            return None

        candidate_text = text
        next_offset = 1
        if text.lower() == "el":
            next_line = lines[start + 1] if start + 1 < len(lines) else None
            base_line = lines[start + 2] if start + 2 < len(lines) else None
            amount_line = lines[start + 3] if start + 3 < len(lines) else None
            if not isinstance(next_line, dict) or not isinstance(base_line, dict) or not isinstance(amount_line, dict):
                return None
            next_text = str(next_line.get("text") or "").strip()
            base_text = str(base_line.get("text") or "").strip()
            amount_text = str(amount_line.get("text") or "").strip()
            amount_classes = (
                [str(cls) for cls in amount_line.get("classes") or []]
                if isinstance(amount_line.get("classes"), list)
                else []
            )
            if "impuesto" not in next_text.lower():
                return None
            if not self._extract_amount(base_text) or not self._extract_amount(amount_text):
                return None
            if "ms-auto" not in amount_classes and "pos-receipt-right-align" not in amount_classes:
                return None
            candidate_text = f"{next_text} {text} {base_text}".strip()
            next_offset = 3

        mergeable_labels = {"subtotal", "impuesto", "tax"}
        if not any(key in candidate_text.lower() for key in mergeable_labels):
            return None

        amount = self._extract_amount(candidate_text) or ""
        if amount:
            label_text = candidate_text
            amount_index = label_text.rfind(amount)
            if amount_index >= 0:
                label_text = label_text[:amount_index].strip()
            merged_line = {
                "text": f"{label_text}  {amount}".strip(),
                "align": "left",
                "classes": ["merged-label-amount", *classes],
            }
            return (merged_line, start + 1)
        consumed_offset = next_offset
        for offset in range(next_offset, min(next_offset + 3, len(lines) - start)):
            next_line = lines[start + offset]
            if not isinstance(next_line, dict):
                continue
            next_text = str(next_line.get("text") or "").strip()
            candidate_amount = self._extract_amount(next_text)
            if not candidate_amount:
                continue
            amount = candidate_amount
            consumed_offset = offset
            break
        if not amount:
            return None
        merged_line = {
            "text": f"{candidate_text}  {amount}",
            "align": "left",
            "classes": ["merged-label-amount", *classes],
        }
        if "subtotal" in candidate_text.lower():
            return (
                merged_line,
                start + consumed_offset + 1,
            )
        return (merged_line, start + consumed_offset + 1)

    def _build_total_emphasis_block(self, label: str, amount: str) -> list[dict[str, Any]]:
        clean_label = str(label or "").strip().rstrip(":")
        clean_amount = str(amount or "").strip()
        merged_text = f"{clean_label} {clean_amount}".strip() if clean_amount else clean_label
        return [
            {
                "text": "-" * self._escpos_line_width(),
                "align": "left",
                "classes": ["receipt-separator"],
            },
            {
                "text": merged_text,
                "align": "center",
                "bold": True,
                "double_width": True,
                "classes": ["receipt-total-emphasized"],
            },
        ]

    def _consume_invoice_section(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        if line.get("type") != "image" or str(line.get("image_kind") or "") != "qr":
            return None

        next_index = start + 1
        url_line = None
        code_line = None
        trailing_lines: list[dict[str, Any]] = []
        for candidate_index in range(start + 1, min(len(lines), start + 5)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            text = str(candidate.get("text") or "").strip()
            classes = [str(cls) for cls in candidate.get("classes") or []] if isinstance(candidate.get("classes"), list) else []
            if {"payment-terminal-line", "pos-payment-terminal-receipt", "payment-terminal-logo", "payment-terminal-nfc-icon"}.intersection(classes):
                break
            if not text:
                continue
            if self._is_invoice_prompt_line(candidate):
                next_index = candidate_index + 1
                continue
            is_portal_url = "portal-url" in classes or text.startswith(("http://", "https://"))
            if url_line is None and is_portal_url:
                url_line = candidate
                next_index = candidate_index + 1
                continue
            is_unique_code = "unique-code" in classes or text.lower().startswith("code:")
            if code_line is None and is_unique_code:
                code_line = candidate
                next_index = candidate_index + 1
                continue
            if "pos-config-name" in classes:
                next_index = candidate_index + 1
                continue
            trailing_lines.append(candidate)

        merged: list[dict[str, Any]] = [{**line, "align": "center"}]
        if url_line:
            merged.append({**url_line, "align": "center"})
        if code_line:
            merged.append({**code_line, "align": "center"})
        merged.extend({**item, "align": "center"} for item in trailing_lines)
        return merged, next_index

    def _prepend_separator_for_reference(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "pos-receipt-vat" not in classes:
            return None
        # VAT rows already sit between subtotal/total blocks which add their own
        # separators. Prepending another one creates the repeated dashed lines
        # visible around the tax section.
        merged_classes = ["merged-label-amount", *classes] if self._extract_amount(str(line.get("text") or "").strip()) else classes
        return (
            [
                {
                    **line,
                    "align": "left",
                    "classes": merged_classes,
                }
            ],
            start + 1,
        )

    def _consume_label_amount_line(
        self, lines: list[dict[str, Any]], start: int, discount_total: Decimal
    ) -> tuple[dict[str, Any], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if not text:
            return None
        mergeable = {"paymentlines", "label-discount"}
        if not mergeable.intersection(classes):
            return None

        if "label-discount" in classes:
            amount = self._format_amount_like("$ 0.00", discount_total)
            return (
                {
                    "text": f"{text} {amount}",
                    "align": "left",
                    "classes": ["merged-label-amount", *classes],
                },
                min(len(lines), start + 2),
            )

        inline_amount = self._extract_amount(text)
        if inline_amount:
            label_text = text
            amount_index = label_text.rfind(inline_amount)
            if amount_index >= 0:
                label_text = label_text[:amount_index].strip()
            return (
                {
                    "text": f"{label_text} {inline_amount}".strip(),
                    "align": "left",
                    "classes": ["merged-label-amount", *classes],
                },
                start + 1,
            )

        for offset in range(1, 4):
            index = start + offset
            if index >= len(lines):
                break
            candidate = lines[index]
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            amount = self._extract_amount(candidate_text)
            if not amount:
                continue
            if "pos-receipt-right-align" not in candidate_classes and offset > 1:
                continue
            return (
                {
                    "text": f"{text} {amount}",
                    "align": "left",
                    "classes": ["merged-label-amount", *classes],
                },
                index + 1,
            )
        return None

    def _product_discount_amount(self, line: dict[str, Any]) -> Decimal:
        original_total = self._parse_decimal(str(line.get("original_total") or ""))
        final_total = self._parse_decimal(str(line.get("total") or ""))
        if original_total is None or final_total is None:
            return Decimal("0")
        diff = original_total - final_total
        if diff <= 0:
            return Decimal("0")
        return diff

    def _skip_total_can_block(self, lines: list[dict[str, Any]], start: int) -> int | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip().replace(" ", "").lower()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "totalcan" not in text:
            return None
        if "receipt-total" not in classes and "label-total" not in classes:
            return None

        next_index = start + 1
        for candidate_index in range(start + 1, min(len(lines), start + 4)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip().replace(" ", "").lower()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if "totalcan" in candidate_text or "label-total" in candidate_classes or "pos-receipt-right-align" in candidate_classes:
                next_index = candidate_index + 1
        return next_index

    def _consume_emphasized_total(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        text = str(line.get("text") or "").strip()
        if "receipt-total" not in classes or not text:
            return None

        label = ""
        amount = ""
        next_index = start + 1
        for candidate_index in range(start + 1, min(len(lines), start + 4)):
            candidate = lines[candidate_index]
            if not isinstance(candidate, dict):
                continue
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_amount = self._extract_amount(candidate_text) if candidate_text else ""
            if "me-1" in candidate_classes and candidate_text:
                label = candidate_text
                next_index = candidate_index + 1
                continue
            if not amount and candidate_text:
                extracted_amount = candidate_amount
                if extracted_amount and self._looks_like_amount(self._extract_amount(candidate_text) or candidate_text):
                    amount = extracted_amount
            if (
                "me-1" in candidate_classes
                or "label-total" in candidate_classes
                or "pos-receipt-right-align" in candidate_classes
            ):
                next_index = candidate_index + 1
        if not amount:
            amount = self._extract_amount(text)
        if not label:
            if amount and amount in text:
                return (self._build_total_emphasis_block(text.strip(), ""), next_index)
            label = text
            if amount:
                compact_amount = amount.strip()
                amount_index = label.rfind(compact_amount)
                if amount_index >= 0:
                    label = label[:amount_index].strip()
        label = re.sub(r"[\s:$]+$", "", label).strip()
        return (self._build_total_emphasis_block(label, amount), next_index)

    def _skip_duplicate_summary(self, lines: list[dict[str, Any]], start: int) -> int | None:
        line = lines[start]
        if not isinstance(line, dict):
            return None
        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "receipt-total" not in classes or not text:
            return None
        if "fs-1" not in classes and "label-total" not in classes:
            compact = text.replace(" ", "").lower()
            if compact.startswith("totalcan"):
                return start + 1

        combined_text = text.replace(" ", "")
        window = lines[start + 1 : start + 4]
        if not window:
            return None

        label_index = None
        amount_index = None
        label_text = ""
        amount_text = ""
        for offset, candidate in enumerate(window, start=1):
            if not isinstance(candidate, dict):
                continue
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            candidate_text = str(candidate.get("text") or "").strip()
            if not candidate_text:
                continue
            if label_index is None and ("label-total" in candidate_classes or "me-1" in candidate_classes):
                label_index = start + offset
                label_text = candidate_text
                continue
            if amount_index is None and (
                "pos-receipt-right-align" in candidate_classes or self._looks_like_amount(self._extract_amount(candidate_text))
            ):
                amount_index = start + offset
                amount_text = self._extract_amount(candidate_text) or candidate_text

        if label_index is None:
            return None

        combined_next = f"{label_text}{amount_text}".replace(" ", "")
        if combined_next and (
            combined_text == combined_next
            or combined_text.endswith(amount_text.replace(" ", ""))
            or combined_text.startswith(label_text.replace(" ", ""))
        ):
            return label_index

        return None

    def _consume_product_block(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        qty_line = self._line_with_class(lines, start, "qty")
        name_line = None
        product_price_line = None
        extra_line = None
        combo_items: list[str] = []
        last_index = start

        if qty_line:
            window = lines[start + 1 : start + 6]
            qty = str(qty_line.get("text") or "").strip()
            for offset, candidate in enumerate(window, start=1):
                candidate_index = start + offset
                if not isinstance(candidate, dict):
                    continue
                classes = (
                    [str(cls) for cls in candidate.get("classes") or []]
                    if isinstance(candidate.get("classes"), list)
                    else []
                )
                if name_line is None and "d-inline" in classes:
                    name_line = candidate
                    last_index = candidate_index
                    continue
                if product_price_line is None and "product-price" in classes:
                    product_price_line = candidate
                    last_index = candidate_index
                    continue
                if extra_line is None and "price-per-unit" in classes:
                    extra_line = candidate
                    last_index = candidate_index
                    continue
                if "qty" in classes or "product-price" in classes:
                    break
        else:
            current = lines[start]
            if not isinstance(current, dict):
                return None
            classes = (
                [str(cls) for cls in current.get("classes") or []]
                if isinstance(current.get("classes"), list)
                else []
            )
            if "d-inline" not in classes:
                return None
            qty = "1"
            name_line = current
            for offset, candidate in enumerate(lines[start + 1 : start + 4], start=1):
                candidate_index = start + offset
                if not isinstance(candidate, dict):
                    continue
                candidate_classes = (
                    [str(cls) for cls in candidate.get("classes") or []]
                    if isinstance(candidate.get("classes"), list)
                    else []
                )
                if product_price_line is None and "product-price" in candidate_classes:
                    product_price_line = candidate
                    last_index = candidate_index
                    continue
                if extra_line is None and "price-per-unit" in candidate_classes:
                    extra_line = candidate
                    last_index = candidate_index
                    continue
                if "d-inline" in candidate_classes or "qty" in candidate_classes:
                    break

        if not name_line or not product_price_line:
            return None

        raw_product_price = str(product_price_line.get("text") or "").strip()
        extra_text = str(extra_line.get("text") or "").strip() if extra_line else ""
        total = self._normalize_amount_display(self._extract_amount(raw_product_price), raw_product_price)
        if not qty or not total:
            return None

        name = str(name_line.get("text") or "").strip()
        extra_amount = self._extract_amount(extra_text)
        qty = self._recover_product_qty(qty, total, extra_text)
        lowered_extra = extra_text.lower()
        if extra_text and ("descuento" in lowered_extra or "discount" in lowered_extra):
            discount_text = extra_text
            unit_price = self._derive_unit_price(qty, total, raw_product_price)
            original_total = self._normalize_amount_display(extra_amount, extra_text)
        else:
            discount_text = ""
            unit_price = self._normalize_amount_display(extra_amount, extra_text) if extra_amount else self._derive_unit_price(qty, total, raw_product_price)
            original_total = total

        combo_start_index = last_index + 1
        for offset, candidate in enumerate(lines[combo_start_index : combo_start_index + 5]):
            candidate_index = combo_start_index + offset
            if not isinstance(candidate, dict):
                continue
            candidate_text = str(candidate.get("text") or "").strip()
            candidate_classes = (
                [str(cls) for cls in candidate.get("classes") or []]
                if isinstance(candidate.get("classes"), list)
                else []
            )
            if not candidate_text:
                continue
            if "d-inline" in candidate_classes and "product-price" not in candidate_classes:
                next_candidate = lines[candidate_index + 1] if candidate_index + 1 < len(lines) else None
                if isinstance(next_candidate, dict):
                    next_candidate_classes = (
                        [str(cls) for cls in next_candidate.get("classes") or []]
                        if isinstance(next_candidate.get("classes"), list)
                        else []
                    )
                    if "product-price" in next_candidate_classes:
                        break
                combo_items.append(candidate_text)
                last_index = candidate_index
                continue
            if (
                "qty" in candidate_classes
                or "product-price" in candidate_classes
                or self._looks_like_amount(candidate_text)
                or candidate_text.lower().startswith(("subtotal", "tax", "total", "change", "discount"))
            ):
                break

        merged = {
            "type": "product_line",
            "qty": qty,
            "name": name,
            "unit_price": unit_price,
            "total": total,
            "discount_text": discount_text,
            "original_total": original_total,
            "combo_items": combo_items,
            "align": "left",
            "classes": ["product-line-merged"],
        }
        return merged, last_index + 1

    def _recover_product_qty(self, qty: str, total: str, extra_text: str) -> str:
        qty_value = self._parse_decimal(qty)
        if qty_value is None or qty_value != 0:
            return qty

        unit_amount = self._extract_amount(extra_text)
        unit_value = self._parse_decimal(unit_amount or extra_text)
        total_value = self._parse_decimal(total)
        if unit_value is None or total_value is None or unit_value == 0 or total_value == 0:
            return qty

        recovered = (total_value / unit_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return self._format_quantity_display(recovered, qty)

    def _format_quantity_display(self, value: Decimal, sample: str) -> str:
        normalized = value.normalize()
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if not text:
            text = "0"
        if "," in sample and "." not in sample:
            text = text.replace(".", ",")
        return text

    def _is_orphan_weight_fragment(self, text: str) -> bool:
        return text.strip().lower() in {"on", "kg", "g", "lb", "oz"}

    def _consume_kitchen_product_line(
        self, lines: list[dict[str, Any]], start: int
    ) -> tuple[dict[str, Any], int] | None:
        qty_line = lines[start] if start < len(lines) else None
        name_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(qty_line, dict) or not isinstance(name_line, dict):
            return None

        qty_classes = (
            [str(cls) for cls in qty_line.get("classes") or []]
            if isinstance(qty_line.get("classes"), list)
            else []
        )
        name_classes = (
            [str(cls) for cls in name_line.get("classes") or []]
            if isinstance(name_line.get("classes"), list)
            else []
        )
        if "me-3" not in qty_classes or "product-name" not in name_classes:
            return None

        qty = str(qty_line.get("text") or "").strip()
        name = str(name_line.get("text") or "").strip()
        if not qty or not name:
            return None
        if not re.fullmatch(r"\d+(?:[.,]\d+)?", qty):
            return None

        return (
            {
                "text": f"{qty.rjust(2)} x {name}",
                "align": "left",
                "classes": ["kitchen-product-line"],
            },
            start + 2,
        )

    def _line_with_class(self, lines: list[dict[str, Any]], index: int, class_name: str) -> dict[str, Any] | None:
        if index >= len(lines):
            return None
        line = lines[index]
        if not isinstance(line, dict):
            return None
        classes = line.get("classes")
        if not isinstance(classes, list):
            return None
        if class_name not in [str(cls) for cls in classes]:
            return None
        return line

    def _extract_amount(self, text: str) -> str:
        text = text.strip()
        amount_pattern = r"[$€]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?"
        matches = re.findall(amount_pattern, text)
        if not matches:
            return ""
        return matches[-1].strip()

    def _extract_signed_amount(self, text: str) -> str:
        text = text.strip()
        amount_pattern = r"[-+]?[$€]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?"
        matches = re.findall(amount_pattern, text)
        if not matches:
            return self._extract_amount(text)
        return matches[-1].strip()

    def _derive_unit_price(self, qty: str, total: str, fallback: str) -> str:
        if not self._should_show_unit_price(qty):
            return ""

        qty_value = self._parse_decimal(qty)
        total_value = self._parse_decimal(total)
        if qty_value is not None and total_value is not None and qty_value != 0:
            unit_value = (total_value / qty_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return self._format_amount_like(total, unit_value)

        fallback_amount = self._extract_amount(fallback)
        return fallback_amount or fallback.strip()

    def _normalize_amount_display(self, amount_text: str, sample: str) -> str:
        amount_value = self._parse_decimal(amount_text)
        if amount_value is None:
            return amount_text
        return self._format_amount_like(amount_text or sample, amount_value)

    def _parse_decimal(self, value: str) -> Decimal | None:
        cleaned = self._normalize_currency_text(value).strip()
        cleaned = re.sub(r"(?i)EUR", "", cleaned)
        cleaned = cleaned.replace("€", "").replace("$", "")
        cleaned = cleaned.replace(" ", "")
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "." in cleaned:
            parts = cleaned.split(".")
            if len(parts) > 2:
                cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    def _format_amount_like(self, sample: str, amount: Decimal) -> str:
        stripped = self._normalize_currency_text(str(sample or "").strip())
        currency_symbols = "$€"
        symbol_prefix = ""
        symbol_suffix = ""
        decimal_separator = "."
        thousands_separator = ","

        first_digit_match = re.search(r"\d", stripped)
        first_digit_index = first_digit_match.start() if first_digit_match else -1
        if first_digit_index >= 0:
            prefix_part = stripped[:first_digit_index]
            suffix_part = stripped[first_digit_index + 1 :]
            for char in prefix_part:
                if char in currency_symbols:
                    symbol_prefix = char
                    break
            for char in reversed(suffix_part):
                if char in currency_symbols:
                    symbol_suffix = char
                    break
        elif stripped and stripped[0] in currency_symbols:
            symbol_prefix = stripped[0]
        elif stripped and stripped[-1] in currency_symbols:
            symbol_suffix = stripped[-1]

        separators = re.findall(r"\d([.,])\d", stripped)
        if separators:
            decimal_separator = separators[-1]
            thousands_separator = "." if decimal_separator == "," else ","

        text = f"{amount:.2f}"
        integer_part, decimal_part = text.split(".")
        grouped_integer = f"{int(integer_part):,}".replace(",", thousands_separator)
        text = f"{grouped_integer}{decimal_separator}{decimal_part}"

        if "\u20ac" in stripped:
            return f"{text} \u20ac"

        if symbol_prefix:
            space_after = " " if re.search(rf"{re.escape(symbol_prefix)}\s+\d", stripped) else ""
            return f"{symbol_prefix}{space_after}{text}"
        if symbol_suffix:
            space_before = " " if re.search(rf"\d\s+{re.escape(symbol_suffix)}", stripped) else ""
            return f"{text}{space_before}{symbol_suffix}"
        return text

    def _build_escpos_image(self, line: dict[str, Any]) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            print("[IOT ESCPOS] PIL not available for image rendering")
            return b""

        image_kind = str(line.get("image_kind") or "image")
        src = str(line.get("src") or "").strip()
        if not src:
            print(f"[IOT ESCPOS] empty src for image_kind={image_kind}")
            return b""
        image_data = self._fetch_receipt_image(src)
        if not image_data:
            print(f"[IOT ESCPOS] failed to fetch image_kind={image_kind} src={src[:200]}")
            return b""
        image_data = self._maybe_convert_svg_to_png(src, image_data)
        if not image_data:
            print(f"[IOT ESCPOS] failed svg conversion image_kind={image_kind} src={src[:200]}")
            return b""

        target_width = self._escpos_image_width(line)
        requested_size = self._escpos_requested_image_size(line, target_width)
        cache_key = hashlib.sha1(
            f"{image_kind}|{target_width}|{requested_size}|".encode("utf-8") + image_data
        ).hexdigest()
        cached_raster = self._escpos_raster_cache.get(cache_key)
        if cached_raster is not None:
            return cached_raster

        try:
            with Image.open(BytesIO(image_data)) as img:
                print(
                    f"[IOT ESCPOS] rendering image_kind={image_kind} "
                    f"format={img.format} size={img.size} mode={img.mode}"
                )
                img = self._prepare_receipt_image(img, image_kind, line)
                img = self._resize_escpos_image(img, line, target_width)
                if image_kind == "logo":
                    img = self._finalize_escpos_logo_image(img)
                classes = (
                    {str(cls) for cls in line.get("classes") or []}
                    if isinstance(line.get("classes"), list)
                    else set()
                )
                if "cn-dish-line" in classes:
                    raster = self._image_to_escpos_bit_image(img)
                else:
                    raster = self._image_to_escpos_raster(img)
                self._escpos_raster_cache[cache_key] = raster
                if len(self._escpos_raster_cache) > 64:
                    oldest_key = next(iter(self._escpos_raster_cache))
                    self._escpos_raster_cache.pop(oldest_key, None)
                return raster
        except Exception as exc:
            print(f"[IOT ESCPOS] failed to build raster image_kind={image_kind}: {exc}")
            return b""

    def _prepare_receipt_image(self, img, kind: str, line: dict[str, Any] | None = None):
        try:
            from PIL import Image
        except ImportError:
            return img.convert("1")

        kind = kind or "image"
        classes = (
            {str(cls) for cls in (line or {}).get("classes") or []}
            if isinstance((line or {}).get("classes"), list)
            else set()
        )
        if "A" in img.getbands():
            alpha = img.getchannel("A")
            alpha_bbox = alpha.getbbox()
            if alpha_bbox and {"payment-terminal-logo", "payment-terminal-nfc-icon"}.intersection(classes):
                pad = 4
                left = max(0, alpha_bbox[0] - pad)
                top = max(0, alpha_bbox[1] - pad)
                right = min(img.width, alpha_bbox[2] + pad)
                bottom = min(img.height, alpha_bbox[3] + pad)
                img = img.crop((left, top, right, bottom))
            rgba = img.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            img = Image.alpha_composite(background, rgba)
        if kind == "qr":
            grayscale = img.convert("L")
            inverted_bbox = grayscale.point(lambda px: 255 - px, mode="L").getbbox()
            if inverted_bbox:
                cropped = grayscale.crop(inverted_bbox)
                quiet_zone = max(8, int(max(cropped.width, cropped.height) * 0.12))
                canvas = Image.new(
                    "L",
                    (cropped.width + (quiet_zone * 2), cropped.height + (quiet_zone * 2)),
                    255,
                )
                canvas.paste(cropped, (quiet_zone, quiet_zone))
                grayscale = canvas
            return grayscale.point(lambda px: 0 if px < 180 else 255, mode="1")
        if kind == "barcode":
            grayscale = img.convert("L")
            inverted_bbox = grayscale.point(lambda px: 255 - px, mode="L").getbbox()
            if inverted_bbox:
                cropped = grayscale.crop(inverted_bbox)
                quiet_zone = max(16, int(cropped.width * 0.08))
                canvas = Image.new(
                    "L",
                    (cropped.width + (quiet_zone * 2), cropped.height),
                    255,
                )
                canvas.paste(cropped, (quiet_zone, 0))
                grayscale = canvas
            threshold = int(os.getenv("IOT_ESCPOS_BARCODE_THRESHOLD", "200") or "200")
            threshold = max(1, min(254, threshold))
            return grayscale.point(lambda px: 0 if px < threshold else 255, mode="1")
        if kind == "logo":
            # Keep logos in grayscale until after resizing; thresholding first
            # softens edges and makes small marks look muddy on thermal printers.
            return ImageOps.autocontrast(img.convert("L"), cutoff=1)

        if kind == "receipt":
            img = self._trim_receipt_image_vertical_whitespace(img)

        grayscale = img.convert("L")
        return grayscale.point(lambda px: 0 if px < 180 else 255, mode="1")

    def _trim_receipt_image_vertical_whitespace(self, img):
        grayscale = img.convert("L")
        threshold = int(os.getenv("IOT_RECEIPT_IMAGE_TRIM_THRESHOLD", "245") or "245")
        threshold = max(1, min(254, threshold))

        min_dark_pixels = int(os.getenv("IOT_RECEIPT_IMAGE_MIN_DARK_PIXELS", "0") or "0")
        if min_dark_pixels <= 0:
            min_dark_pixels = max(4, int(img.width * 0.015))

        pixels = grayscale.load()
        content_rows = []
        for y in range(img.height):
            dark_pixels = 0
            for x in range(img.width):
                if pixels[x, y] < threshold:
                    dark_pixels += 1
                    if dark_pixels >= min_dark_pixels:
                        content_rows.append(y)
                        break

        if not content_rows:
            return img

        top = content_rows[0]
        bottom = content_rows[-1] + 1
        vertical_pad = int(os.getenv("IOT_RECEIPT_IMAGE_VERTICAL_PAD", "15") or "15")
        vertical_pad = max(0, min(80, vertical_pad))
        crop_top = max(0, top - vertical_pad)
        crop_bottom = min(img.height, bottom + vertical_pad)
        if crop_top == 0 and crop_bottom == img.height:
            return img

        _logger.info(
            "Trimmed receipt image vertical whitespace original=%sx%s crop_top=%s crop_bottom=%s threshold=%s min_dark_pixels=%s",
            img.width,
            img.height,
            crop_top,
            crop_bottom,
            threshold,
            min_dark_pixels,
        )
        return img.crop((0, crop_top, img.width, crop_bottom))

    def _finalize_escpos_logo_image(self, img):
        grayscale = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        threshold = int(os.getenv("IOT_ESCPOS_LOGO_THRESHOLD", "196") or "196")
        threshold = max(1, min(254, threshold))
        return grayscale.point(lambda px: 0 if px < threshold else 255, mode="1")

    def _render_escpos_lines(self, line: dict[str, Any], width: int) -> list[str]:
        effective_width = max(16, width // 2) if line.get("double_width") else width
        if line.get("type") == "spacer":
            return [""]
        classes = [str(cls) for cls in line.get("classes", [])] if isinstance(line.get("classes"), list) else []
        if line.get("type") == "product_line":
            return self._render_product_line(line, width)
        if line.get("type") == "product_header":
            return [str(line.get("text") or "").strip()]
        if line.get("type") == "header_meta_line":
            return self._render_header_meta_line(line, effective_width)
        text = str(line.get("text") or "").strip()
        if not text and {"receipt-spacer", "customer-spacer", "product-section-spacer", "payment-terminal-spacer"}.intersection(classes):
            return [""]
        if not text:
            return []

        # Remove the currency symbol from subtotal / tax summary lines so the
        # receipt stays compact (the symbol is only shown on the total).
        compact_lower = text.strip().lower()
        if compact_lower.startswith("subtotal") or self._looks_like_tax_summary_line(text):
            text = re.sub(r"\s*[€$]\s*$", "", text)

        if self._is_separator_line(text) and "invoice-asterisk-border" not in classes:
            return ["-" * width]

        align = str(line.get("align") or "left")
        if "merged-label-amount" in classes:
            left_text, right_text = self._split_label_amount_text(text)
            if left_text and right_text:
                return self._wrap_column_line([left_text, right_text], effective_width, classes=classes)
            return self._wrap_aligned_text(text, effective_width, "left")
        if self._looks_like_tax_summary_line(text):
            left_text, right_text = self._split_label_amount_text(text)
            if left_text and right_text:
                return self._wrap_column_line([left_text, right_text], effective_width, classes=["merged-label-amount"])
        if "receipt-total-emphasized" in classes:
            return self._wrap_aligned_text(text, effective_width, "center")
        parts = self._split_receipt_columns(text)
        if len(parts) >= 2 and align == "left":
            return self._wrap_column_line(parts, effective_width, emphasize=bool(line.get("bold")), classes=classes)
        return self._wrap_aligned_text(text, effective_width, align)

    def _looks_like_tax_summary_line(self, text: str) -> bool:
        compact = str(text or "").strip().lower()
        if not compact:
            return False
        if not self._extract_amount(compact):
            return False
        tax_markers = ("igic", "iva", "vat", "tax", "impuesto")
        return any(marker in compact for marker in tax_markers)

    def _render_product_line(self, line: dict[str, Any], width: int) -> list[str]:
        qty = str(line.get("qty") or "").strip()
        product_name = str(line.get("name") or "").strip()
        unit_price = str(line.get("unit_price") or "").strip()
        total = str(line.get("total") or "").strip()
        discount_text = str(line.get("discount_text") or "").strip()
        original_total = str(line.get("original_total") or "").strip()
        combo_items = [str(item).strip() for item in (line.get("combo_items") or []) if str(item).strip()]
        if not qty or not product_name:
            return []

        classes = [str(item) for item in (line.get("classes") or [])]
        is_kitchen = "kitchen-product-line" in classes
        if total:
            qty_width, total_width, name_width = self._receipt_column_layout(width)
        else:
            qty_width = max(2, min(3, len(qty)))
            total_width = 0
            name_width = max(8, width - qty_width - 1)
        name_lines = self._wrap_text(product_name, name_width) or [""]
        first_qty = self._pad_right(qty, qty_width) if is_kitchen or not total else self._pad_center(qty, qty_width)
        first_row = first_qty + " " + self._pad_right(name_lines[0], name_width)
        if total:
            first_row += " " + self._pad_left(total, total_width)
        rows = [first_row]
        for extra_name in name_lines[1:]:
            rows.append(" " * (qty_width + 1) + extra_name)
        if combo_items:
            combo_indent = " " * 3
            combo_width = max(8, width - qty_width - 1 - len(combo_indent))
            for combo_item in combo_items:
                combo_rows = self._wrap_text(combo_item, combo_width) or [combo_item]
                rows.extend((" " * (qty_width + 1) + combo_indent + row) for row in combo_rows)
        if discount_text:
            discount_label = self._format_discount_text(discount_text, total, original_total)
            discount_rows = self._wrap_text(discount_label, name_width) or [discount_label]
            rows.extend((" " * (qty_width + 1)) + row for row in discount_rows)
        if unit_price and self._should_show_unit_price(qty):
            unit_rows = self._wrap_text(unit_price, name_width) or [unit_price]
            rows.extend((" " * (qty_width + 1)) + row for row in unit_rows)
        return rows

    def _render_header_meta_line(self, line: dict[str, Any], width: int) -> list[str]:
        left_text = str(line.get("left_text") or "").strip()
        right_text = str(line.get("right_text") or "").strip()
        if left_text and right_text:
            left_width = self._text_width(left_text)
            right_width = self._text_width(right_text)
            gap = max(1, width - left_width - right_width)
            if left_width + right_width + gap <= width:
                return [left_text + (" " * gap) + right_text]
            rows = self._wrap_aligned_text(left_text, width, "left")
            rows.extend(self._wrap_aligned_text(right_text, width, "right"))
            return rows
        if left_text:
            return self._wrap_aligned_text(left_text, width, "left")
        if right_text:
            return self._wrap_aligned_text(right_text, width, "right")
        return []

    def _format_discount_text(self, text: str, discounted_total: str, original_total: str) -> str:
        clean_text = str(text or "").strip()
        if not clean_text:
            return ""
        compact = clean_text.lower()
        # Full descriptions from the structured payload already include the
        # original price (e.g. "50% de descuento en 540,87 €") — use them
        # directly, only removing the trailing currency symbol.
        if "descuento" in compact or "discount" in compact or "desconto" in compact:
            return re.sub(r"\s*[€$]\s*$", "", clean_text)
        # Bare percentage label (raw order path): "50%" -> "50% discount off on 540,87"
        original_value = self._extract_amount(original_total)
        if not original_value:
            return clean_text
        return f"{clean_text} discount off on {original_value}"

    def _compute_discount_percentage(self, discounted_total: str, original_total: str) -> int | None:
        discounted_value = self._parse_decimal(discounted_total)
        original_value = self._parse_decimal(original_total)
        if discounted_value is None or original_value is None or original_value <= 0:
            return None
        discount_ratio = (original_value - discounted_value) / original_value
        if discount_ratio <= 0:
            return None
        return int((discount_ratio * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _wrap_column_line(
        self,
        parts: list[str],
        width: int,
        emphasize: bool = False,
        classes: list[str] | None = None,
    ) -> list[str]:
        classes = classes or []
        if "merged-label-amount" in classes:
            right = parts[-1].strip()
            left = " ".join(parts[:-1]).strip()
            total_width = max(12, min(18, width // 3))
            left_width = max(8, width - total_width - 1)
            left_lines = self._wrap_text(left, left_width) or [""]
            rows: list[str] = []
            for index, chunk in enumerate(left_lines):
                if index == 0:
                    rows.append(self._pad_right(chunk, left_width) + " " + self._pad_left(right, total_width))
                else:
                    rows.append(chunk)
            if "paymentlines" in classes:
                rows.append("")
            return rows
        product_layout = self._extract_product_line(parts)
        if product_layout:
            qty, product_name, unit_price, total = product_layout
            qty_width, total_width, name_width = self._receipt_column_layout(width)
            name_lines = self._wrap_text(product_name, name_width) or [""]
            rows = [
                self._pad_center(qty, qty_width)
                + " "
                + self._pad_right(name_lines[0], name_width)
                + " "
                + self._pad_left(total, total_width)
            ]
            for extra_name in name_lines[1:]:
                rows.append(" " * (qty_width + 1) + extra_name)
            if self._should_show_unit_price(qty):
                rows.extend(self._wrap_text(unit_price, width))
            return rows

        right = parts[-1]
        left = " ".join(parts[:-1]).strip()
        if len(parts) >= 3 and self._looks_like_amount(parts[-1]) and self._looks_like_amount(parts[-2]):
            right = f"{parts[-2]} {parts[-1]}"
            left = " ".join(parts[:-2]).strip()
        if "pos-receipt-right-align" in classes and len(parts) == 2:
            left, right = parts

        qty_width, right_width, name_width = self._receipt_column_layout(width)
        left_width = qty_width + 1 + name_width
        left_lines = self._wrap_text(left, left_width) or [""]
        rows: list[str] = []
        for index, chunk in enumerate(left_lines):
            if index == 0:
                rows.append(self._pad_right(chunk, left_width) + " " + self._pad_left(right, right_width))
            else:
                rows.append(chunk)
        return rows

    def _receipt_column_layout(self, width: int) -> tuple[int, int, int]:
        safe_width = max(16, width)
        qty_width = 2
        total_width = 8
        name_width = max(8, safe_width - qty_width - total_width - 2)
        return qty_width, total_width, name_width

    def _build_product_header_text(self, width: int) -> str:
        qty_width, total_width, name_width = self._receipt_column_layout(width)
        return (
            self._pad_center("Uds.", qty_width)
            + " "
            + self._pad_right("Producto", name_width)
            + " "
            + self._pad_left("Importe", total_width)
        )

    def _extract_product_line(self, parts: list[str]) -> tuple[str, str, str, str] | None:
        if len(parts) < 3:
            return None

        total = parts[-1].strip()
        if not self._looks_like_amount(total):
            return None

        qty_unit = parts[-2].strip()
        product_name = " ".join(parts[:-2]).strip()
        qty, unit_price = self._split_qty_unit(qty_unit)
        if product_name and qty and unit_price:
            return qty, product_name, unit_price, total

        merged = " ".join(parts).strip()
        patterns = [
            r"^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*[xX*]\s*(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)\s+(?P<total>[$€]?\d+(?:[.,]\d{1,2})?)$",
            r"^(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<name>.+?)\s+(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)\s+(?P<total>[$€]?\d+(?:[.,]\d{1,2})?)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, merged)
            if not match:
                continue
            name = match.group("name").strip()
            qty = match.group("qty").strip()
            unit = match.group("unit").strip()
            total = match.group("total").strip()
            if name and self._looks_like_amount(total):
                return qty, name, unit, total
        return None

    def _split_qty_unit(self, value: str) -> tuple[str, str]:
        value = value.strip()
        match = re.fullmatch(
            r"(?P<qty>\d+(?:[.,]\d+)?)\s*[xX*]\s*(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)",
            value,
        )
        if match:
            return match.group("qty").strip(), match.group("unit").strip()

        match = re.fullmatch(
            r"(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[$€]?\d+(?:[.,]\d{1,2})?)",
            value,
        )
        if match:
            return match.group("qty").strip(), match.group("unit").strip()
        return "", ""

    def _should_show_unit_price(self, qty: str) -> bool:
        normalized = qty.strip().replace(",", ".")
        try:
            return float(normalized) != 1.0
        except ValueError:
            return True

    def _wrap_aligned_text(self, text: str, width: int, align: str) -> list[str]:
        rows = self._wrap_text(text, width)
        if align == "center":
            return [self._pad_center(row, width) for row in rows]
        if align == "right":
            return [self._pad_left(row, width) for row in rows]
        return rows

    def _truncate_to_width(self, text: str, width: int) -> str:
        text = str(text or "").strip()
        if not text or self._text_width(text) <= width:
            return text
        result = ""
        for char in text:
            if self._text_width(result + char) > width:
                break
            result += char
        return result

    def _wrap_text(self, text: str, width: int) -> list[str]:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []
        words = text.split(" ")
        rows: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if self._text_width(candidate) <= width:
                current = candidate
                continue
            if current:
                rows.append(current)
                current = ""
            if self._text_width(word) <= width:
                current = word
                continue
            rows.extend(self._break_long_token(word, width))
        if current:
            rows.append(current)
        return rows

    def _break_long_token(self, token: str, width: int) -> list[str]:
        rows: list[str] = []
        current = ""
        for char in token:
            candidate = current + char
            if current and self._text_width(candidate) > width:
                rows.append(current)
                current = char
            else:
                current = candidate
        if current:
            rows.append(current)
        return rows

    def _split_payment_terminal_receipt_text(self, text: str) -> list[str]:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return []

        labels = [
            "Auth Code",
            "Card",
            "Comercio",
            "ETIQUETAAPP",
            "Factura",
            "Method",
            "Pedido",
            "RTS",
            "Terminal",
        ]
        label_pattern = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
        matches = list(re.finditer(rf"(?P<label>{label_pattern})\s*:", compact))
        if not matches:
            return [compact]

        entries: list[str] = []
        seen: set[str] = set()
        trailing_message = ""

        for index, match in enumerate(matches):
            label = match.group("label").strip()
            value_start = match.end()
            value_end = matches[index + 1].start() if index + 1 < len(matches) else len(compact)
            value = compact[value_start:value_end].strip()
            for trailing_marker in (
                "OPERACION CONTACTLESS. FIRMA NO NECESARIA.",
                "OPERACION CON PIN. FIRMA NO NECESARIA.",
                "AUTORIZADA",
            ):
                marker_index = value.upper().find(trailing_marker)
                if marker_index > 0:
                    value = value[:marker_index].strip()
                    break
            if not value:
                continue

            entry = re.sub(r"\s+", " ", f"{label}: {value}").strip()
            if entry and entry not in seen:
                seen.add(entry)
                entries.append(entry)

        upper_compact = compact.upper()
        if "OPERACION CONTACTLESS. FIRMA NO NECESARIA." in upper_compact:
            trailing_message = "OPERACION CONTACTLESS. FIRMA NO NECESARIA."
        elif "OPERACION CON PIN. FIRMA NO NECESARIA." in upper_compact:
            trailing_message = "OPERACION CON PIN. FIRMA NO NECESARIA."
        elif "AUTORIZADA" in upper_compact:
            trailing_message = "AUTORIZADA"

        if trailing_message:
            normalized_message = re.sub(r"\s+", " ", trailing_message).strip()
            if normalized_message and normalized_message not in seen:
                seen.add(normalized_message)
                entries.append(normalized_message)

        return entries or [compact]

    def _iter_payment_terminal_receipt_lines(self, receipt_item: dict[str, Any]) -> list[str]:
        collected: list[str] = []
        for value in receipt_item.get("lines") or []:
            text = str(value or "").strip()
            if text:
                collected.append(text)

        etiqueta_lines = self._extract_etiquetaapp_lines(receipt_item)
        if etiqueta_lines:
            insert_index = len(collected)
            for index, text in enumerate(collected):
                upper_text = text.upper()
                if "OPERACION CONTACTLESS" in upper_text or "OPERACION CON PIN" in upper_text or upper_text == "AUTORIZADA":
                    insert_index = index
                    break
            collected[insert_index:insert_index] = etiqueta_lines

        unique_lines: list[str] = []
        seen: set[str] = set()
        for text in collected:
            normalized = re.sub(r"\s+", " ", text).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_lines.append(normalized)
        return unique_lines

    def _normalize_payment_terminal_line(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        return compact

    def _is_payment_terminal_nfc_src(self, src: str) -> bool:
        normalized = str(src or "").strip().lower()
        return "nfc" in normalized and normalized.endswith((".png", ".svg", ".jpg", ".jpeg", ".webp"))

    def _extract_etiquetaapp_lines(self, payload: Any) -> list[str]:
        values: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized_key = re.sub(r"[^A-Z0-9]", "", str(key or "").upper())
                    if "ETIQUETAAPP" == normalized_key or "ETIQUETAAPP" in normalized_key:
                        self._append_etiquetaapp_value(values, value)
                    else:
                        walk(value)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)

        unique_lines: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if not text:
                continue
            formatted = f"ETIQUETAAPP: {text}"
            if formatted in seen:
                continue
            seen.add(formatted)
            unique_lines.append(formatted)
        return unique_lines

    def _append_etiquetaapp_value(self, values: list[str], value: Any) -> None:
        if isinstance(value, dict):
            for nested_value in value.values():
                self._append_etiquetaapp_value(values, nested_value)
            return
        if isinstance(value, list):
            for item in value:
                self._append_etiquetaapp_value(values, item)
            return
        text = str(value or "").strip()
        if text:
            values.append(text)

    def _split_payment_terminal_message(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text).strip(" .")
        if not cleaned:
            return []
        chunks = [chunk.strip(" .") for chunk in re.split(r"(?<=[.!?])\s+", cleaned) if chunk.strip(" .")]
        return chunks or [cleaned]

    def _split_receipt_columns(self, text: str) -> list[str]:
        if " x " in text and self._looks_like_amount(text.rsplit(" ", 1)[-1]):
            return [chunk for chunk in re.split(r"\s{2,}|(?<=\S)\s(?=\d+[xX])", text) if chunk]
        return [chunk for chunk in re.split(r"\s{2,}", text) if chunk] or text.split(" ")

    def _split_label_amount_text(self, text: str) -> tuple[str, str]:
        compact = str(text or "").strip()
        if not compact:
            return "", ""
        match = re.search(
            r"(?P<amount>(?:[$€]\s*)?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?)$",
            compact,
        )
        if match:
            amount = match.group("amount").strip()
            label = compact[: match.start()].strip()
            if label:
                return label, amount
        return "", ""

    def _render_service_columns(self, columns: list[dict[str, str]], width: int) -> list[str]:
        if len(columns) != 3:
            return []

        separator = " "
        available_width = max(18, width - (len(columns) - 1) * len(separator))
        base_width = available_width // len(columns)
        column_widths = [base_width] * len(columns)
        column_widths[-1] += available_width - sum(column_widths)

        wrapped_columns: list[list[str]] = []
        for index, column in enumerate(columns):
            label = str(column.get("label") or "").strip()
            value = str(column.get("value") or "").strip()
            if label and value:
                text = f"{label}: {value}"
            else:
                text = value or label
            wrapped_columns.append(self._wrap_text(text, column_widths[index]) or [""])

        row_count = max(len(rows) for rows in wrapped_columns)
        rendered_rows: list[str] = []
        for row_index in range(row_count):
            parts: list[str] = []
            for column_index, rows in enumerate(wrapped_columns):
                cell = rows[row_index] if row_index < len(rows) else ""
                parts.append(self._pad_right(cell, column_widths[column_index]))
            rendered_rows.append(separator.join(parts).rstrip())
        return rendered_rows

    def _split_company_and_reference_lines(self, raw_lines: list[Any]) -> tuple[list[tuple[str, bool]], str]:
        company_lines: list[tuple[str, bool]] = []
        inferred_reference = ""
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            bold = bool(item.get("bold"))
            compact = re.sub(r"\s+", " ", text)
            ticket_match = re.search(r"(?i)\b(ticket\b.*)$", compact)
            if ticket_match:
                before = compact[: ticket_match.start()].strip(" -,:")
                after = ticket_match.group(1).strip()
                if before:
                    company_lines.append((before, bold))
                if after and not inferred_reference:
                    inferred_reference = after
                continue
            company_lines.append((compact, bold))
        return company_lines, inferred_reference

    def _normalize_order_info_text(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        compact = re.sub(r"\s+", " ", text)
        lowered = compact.lower()
        if lowered.startswith("ticket"):
            return compact
        if lowered.startswith("table "):
            table_value = compact[6:].strip()
            return f"MESA {table_value}".strip().upper()
        if lowered.startswith("mesa "):
            return compact.upper()
        if self._looks_like_header_date_line(compact):
            return f"Fecha: {compact}"
        if lowered.startswith("servido por:"):
            return compact
        if lowered.startswith("served by:"):
            return compact
        if lowered.startswith("nif "):
            return compact
        return f"Ticket: {compact}"

    def _load_customer_from_reference_text(self, value: Any) -> dict[str, str] | None:
        reference_text = str(value or "").strip()
        if not reference_text:
            return None
        match = re.search(r"(\d+-\d+-\d+)", reference_text)
        if not match:
            return None
        pos_reference = match.group(1)

        root_dir = Path(__file__).resolve().parents[5]
        query_python = root_dir / ".venv" / "Scripts" / "python.exe"
        config_path = root_dir / "instances" / "dev" / "config" / "odoo.conf"
        if not query_python.exists() or not config_path.exists():
            return None

        query_script = """
import json
import psycopg2
import sys
conn = psycopg2.connect(host='localhost', port=5432, dbname='odoo19_dev', user='odoo', password='odoo')
cur = conn.cursor()
cur.execute(
    '''
    SELECT rp.name, rp.vat, rp.street, rp.city, rp.zip
    FROM pos_order po
    LEFT JOIN res_partner rp ON rp.id = po.partner_id
    WHERE po.pos_reference = %s
    ORDER BY po.id DESC
    LIMIT 1
    ''',
    (sys.argv[1],),
)
row = cur.fetchone()
cur.close()
conn.close()
if not row:
    print('{}')
else:
    name, vat, street, city, zip_code = row
    region = ', '.join([item for item in [city, zip_code] if item])
    print(json.dumps({
        'name': name or '',
        'vat': vat or '',
        'address': street or '',
        'region': region,
    }))
"""
        try:
            result = subprocess.run(
                [str(query_python), "-c", query_script, pos_reference],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        output = str(result.stdout or "").strip()
        if not output or output == "{}":
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "name": str(payload.get("name") or "").strip(),
            "vat": str(payload.get("vat") or "").strip(),
            "address": str(payload.get("address") or "").strip(),
            "region": str(payload.get("region") or "").strip(),
        }

    def _parse_service_contact_line(self, text: str) -> tuple[str, str, str]:
        compact = re.sub(r"\s+", " ", text).strip()
        match = re.search(
            r"(?i)table\s*(?P<table>[^,]+?)\s*,\s*guests?\s*:\s*(?P<guests>\d+)",
            compact,
        )
        if not match:
            return "", "", ""
        table_value = match.group("table").strip()
        guests_value = match.group("guests").strip()
        return f"Mesa {table_value}", table_value, guests_value

    def _parse_served_by_line(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        match = re.search(r"(?i)^served by\s*:\s*(?P<name>.+)$", compact)
        if not match:
            return ""
        return match.group("name").strip()

    def _looks_like_header_date_line(self, text: str) -> bool:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact or self._looks_like_amount(compact):
            return False
        has_time = bool(re.search(r"\b\d{1,2}:\d{2}\b", compact))
        has_date = bool(re.search(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", compact))
        return has_time and has_date

    def _should_skip_ticket_prefix_line(self, line: dict[str, Any] | Any) -> bool:
        if not isinstance(line, dict):
            return False
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if "ticket-name-prefix" not in classes:
            return False
        text = str(line.get("text") or "").strip().lower()
        return text in {"", "undefined", "none", "null"}

    def _should_skip_orphan_weight_fragment(
        self,
        lines: list[dict[str, Any]],
        start: int,
        normalized: list[dict[str, Any]],
    ) -> bool:
        line = lines[start]
        if not isinstance(line, dict):
            return False
        if line.get("type") == "image":
            return False

        text = str(line.get("text") or "").strip()
        classes = [str(cls) for cls in line.get("classes") or []] if isinstance(line.get("classes"), list) else []
        if classes or not text:
            return False

        compact = text.lower()
        if compact not in {"on", "kg", "g", "lb", "oz"}:
            return False

        previous = normalized[-1] if normalized else None
        if isinstance(previous, dict):
            prev_text = str(previous.get("text") or "").strip().lower()
            prev_type = str(previous.get("type") or "").strip().lower()
            if prev_type == "product_line" or prev_text.startswith(("subtotal", "tax", "total")):
                return True

        next_line = lines[start + 1] if start + 1 < len(lines) else None
        if not isinstance(next_line, dict):
            return False
        next_text = str(next_line.get("text") or "").strip().lower()
        next_classes = (
            [str(cls) for cls in next_line.get("classes") or []]
            if isinstance(next_line.get("classes"), list)
            else []
        )
        return next_text.startswith(("subtotal", "tax", "total", "change")) or "ms-auto" in next_classes

    def _split_trailing_amount_text(self, text: str) -> str:
        match = re.match(
            r"^(?P<label>.+?)\s+(?P<amount>[$€]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?:\s*[$€])?)$",
            text.strip(),
        )
        if not match:
            return text
        return f"{match.group('label')}  {match.group('amount')}"

    def _looks_like_amount(self, value: str) -> bool:
        normalized = value.strip().replace(",", "")
        normalized = normalized.replace("$", "").replace("€", "")
        return bool(re.fullmatch(r"[-+]?\d+(?:[.:]\d{1,2})?", normalized))

    def _looks_like_qty_unit(self, value: str) -> bool:
        value = value.strip().replace("€", "").replace("$", "")
        patterns = [
            r"^\d+(?:[.,]\d+)?\s*[xX*]\s*\d+(?:[.,]\d{1,2})?$",
            r"^\d+(?:[.,]\d+)?\s+\d+(?:[.,]\d{1,2})?$",
        ]
        return any(re.fullmatch(pattern, value) for pattern in patterns)

    def _is_separator_line(self, text: str) -> bool:
        compact = text.strip()
        if len(compact) < 3:
            return False
        return len(set(compact)) == 1 and compact[0] in {"-", "=", "_", "*"}

    def _escpos_line_width(self) -> int:
        configured = os.getenv("IOT_ESCPOS_LINE_WIDTH", "").strip()
        if configured.isdigit():
            return max(24, int(configured))
        paper = os.getenv("IOT_ESCPOS_PAPER_WIDTH", "80").strip()
        return 48 if paper == "80" else 32

    def _escpos_encoding_config(
        self,
        payload: dict[str, Any] | None = None,
        lines: list[dict[str, Any]] | None = None,
    ) -> tuple[str, int]:
        payload = payload or {}
        requested_encoding = str(
            payload.get("python_encoding")
            or payload.get("encoding")
            or payload.get("charset")
            or payload.get("codepage")
            or ""
        ).strip().lower()
        if requested_encoding in {"utf-8", "utf8"}:
            return "gb18030", 255
        if requested_encoding in {"gb18030", "gbk", "cp936"}:
            return "gb18030", 255
        if requested_encoding in {"cp858", "ibm858"}:
            return "cp858", 19
        if requested_encoding in {"cp850", "ibm850"}:
            return "cp850", 2
        if requested_encoding in {"cp437", "ibm437"}:
            return "cp437", 0
        if requested_encoding in {"cp1252", "windows-1252", "windows1252"}:
            return "cp1252", 16
        requested_lang = str(payload.get("lang") or "").strip().lower()
        if requested_lang.startswith("zh") and lines and self._is_kitchen_ticket_lines(lines):
            return "gb18030", 255
        if requested_lang.startswith("zh"):
            return "gb18030", 255
        configured = os.getenv("IOT_ESCPOS_ENCODING", "").strip().lower()
        if configured in {"cp858", "ibm858"}:
            return "cp858", 19
        if configured in {"cp850", "ibm850"}:
            return "cp850", 2
        if configured in {"cp437", "ibm437"}:
            return "cp437", 0
        if configured in {"cp1252", "windows-1252", "windows1252"}:
            return "cp1252", 16
        if configured in {"gb18030", "gbk", "cp936"}:
            return "gb18030", 255
        return "gb18030", 255

    def _escpos_image_width(self, line: dict[str, Any]) -> int:
        configured = os.getenv("IOT_ESCPOS_DOTS_WIDTH", "").strip()
        if configured.isdigit():
            return max(128, int(configured))
        kind = str(line.get("image_kind") or "image")
        classes = (
            {str(cls) for cls in line.get("classes") or []}
            if isinstance(line.get("classes"), list)
            else set()
        )
        paper = os.getenv("IOT_ESCPOS_PAPER_WIDTH", "80").strip()
        full_width = 576 if paper == "80" else 384
        if kind == "logo":
            return min(full_width, 380 if paper == "80" else 280)
        if kind == "qr":
            return min(full_width, 320 if paper == "80" else 240)
        if kind == "barcode":
            return min(full_width, 420 if paper == "80" else 300)
        if "payment-terminal-nfc-icon" in classes:
            return min(full_width, 80 if paper == "80" else 64)
        if "payment-terminal-logo" in classes:
            return min(full_width, 72 if paper == "80" else 56)
        return full_width

    def _escpos_requested_image_size(
        self,
        line: dict[str, Any],
        max_width: int,
    ) -> tuple[int | None, int | None]:
        width = self._parse_receipt_image_dimension(line.get("width"))
        height = self._parse_receipt_image_dimension(line.get("height"))
        if width is not None:
            width = min(width, max_width)
        return width, height

    def _resize_escpos_image(self, img, line: dict[str, Any], target_width: int):
        requested_width, requested_height = self._escpos_requested_image_size(line, target_width)
        image_kind = str(line.get("image_kind") or "image")
        if image_kind == "barcode":
            return self._resize_escpos_barcode_image(
                img,
                requested_width=requested_width,
                requested_height=requested_height,
                target_width=target_width,
            )
        width = img.width
        height = img.height

        if requested_width is not None and requested_height is not None:
            width = requested_width
            height = requested_height
        elif requested_width is not None:
            ratio = requested_width / float(max(1, img.width))
            width = requested_width
            height = max(1, int(img.height * ratio))
        elif requested_height is not None:
            ratio = requested_height / float(max(1, img.height))
            height = requested_height
            width = max(1, int(img.width * ratio))
        elif img.width > target_width:
            ratio = target_width / float(img.width)
            width = target_width
            height = max(1, int(img.height * ratio))

        if image_kind == "qr":
            side = max(1, min(width, height, target_width))
            width = side
            height = side

        if width == img.width and height == img.height:
            return img
        resample = Image.Resampling.LANCZOS
        if image_kind == "qr" or image_kind == "barcode":
            resample = Image.Resampling.NEAREST
        return img.resize((max(1, width), max(1, height)), resample=resample)

    def _resize_escpos_barcode_image(
        self,
        img,
        requested_width: int | None,
        requested_height: int | None,
        target_width: int,
    ):
        try:
            from PIL import Image
        except ImportError:
            return img

        width = img.width
        height = img.height

        # Keep the original barcode raster unless we can enlarge it without
        # changing bar proportions. Shrinking to the receipt CSS size tends to
        # make 1D barcodes unreadable on thermal printers.
        desired_width = width
        if requested_width is not None and requested_width > width:
            desired_width = min(requested_width, target_width)

        if desired_width > width:
            scale = desired_width // max(1, width)
            if scale > 1:
                desired_width = width * scale
                if requested_height is not None and requested_height >= height * scale:
                    desired_height = height * scale
                else:
                    desired_height = height * scale
                return img.resize((desired_width, desired_height), Image.Resampling.NEAREST)

        return img

    def _parse_receipt_image_dimension(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return max(1, int(value))
        match = re.search(r"\d+", str(value))
        if not match:
            return None
        return max(1, int(match.group(0)))

    def _fetch_receipt_image(self, src: str) -> bytes:
        cached = self._image_fetch_cache.get(src)
        if cached is not None:
            return cached

        generated = self._build_local_receipt_image(src)
        if generated:
            self._image_fetch_cache[src] = generated
            if len(self._image_fetch_cache) > 64:
                oldest_key = next(iter(self._image_fetch_cache))
                self._image_fetch_cache.pop(oldest_key, None)
            return generated

        if src.startswith("data:image") and "," in src:
            try:
                header, payload_text = src.split(",", 1)
                if ";base64" in header.lower():
                    payload = b64decode(payload_text)
                else:
                    payload = unquote_to_bytes(payload_text)
                self._image_fetch_cache[src] = payload
                return payload
            except Exception:
                _logger.exception("Failed to decode inline receipt image src=%s", src[:200])
                return b""

        if src.startswith(("http://", "https://")):
            url = src
        else:
            base_url = os.getenv("IOT_RECEIPT_BASE_URL", "http://127.0.0.1:8069").rstrip("/") + "/"
            url = urljoin(base_url, src.lstrip("/"))

        try:
            with urlopen(url, timeout=10) as resp:
                payload = resp.read()
                self._image_fetch_cache[src] = payload
                if len(self._image_fetch_cache) > 64:
                    oldest_key = next(iter(self._image_fetch_cache))
                    self._image_fetch_cache.pop(oldest_key, None)
                return payload
        except Exception:
            _logger.exception("Failed to fetch receipt image src=%s resolved_url=%s", src[:200], url)
            return b""

    def _clear_runtime_image_caches(self) -> None:
        self._image_fetch_cache.clear()
        self._escpos_raster_cache.clear()

    def _with_logo_cache_buster(self, src: str) -> str:
        normalized = str(src or "").strip()
        if not normalized or "/web/image" not in normalized.lower():
            return normalized
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)
        query["_iot_logo_v"] = [self._runtime_logo_cache_buster]
        encoded_query = urlencode(query, doseq=True)
        return parsed._replace(query=encoded_query).geturl()

    def _build_local_receipt_image(self, src: str) -> bytes:
        parsed = urlparse(src)
        route = parsed.path or ""
        query = parse_qs(parsed.query)
        if self._is_payment_terminal_nfc_src(route):
            override_path = self.resource_dir / "web" / "nfc_override.png"
            try:
                if override_path.is_file():
                    return override_path.read_bytes()
            except OSError:
                pass
            return self._generate_local_nfc_icon_png()
        if "/report/barcode/QR/" in route:
            payload = route.split("/report/barcode/QR/", 1)[1]
            if not payload:
                return b""
            qr_data = unquote(payload)
            return self._generate_local_qr_png(qr_data, query)
        if "/report/barcode/Code128/" in route:
            payload = route.split("/report/barcode/Code128/", 1)[1]
            if not payload:
                return b""
            barcode_value = unquote(payload)
            return self._generate_local_code128_png(barcode_value, query)
        if route.rstrip("/") == "/report/barcode":
            barcode_type = str((query.get("barcode_type") or query.get("type") or [""])[0]).strip().upper()
            value = str((query.get("value") or query.get("data") or [""])[0]).strip()
            if not barcode_type or not value:
                return b""
            if barcode_type == "QR":
                return self._generate_local_qr_png(unquote(value), query)
            if barcode_type == "CODE128":
                return self._generate_local_code128_png(unquote(value), query)
        return b""

    def _is_qr_receipt_image_src(self, src: str, image_payload: dict[str, Any] | None = None) -> bool:
        payload = image_payload if isinstance(image_payload, dict) else {}
        explicit_kind = str(
            payload.get("image_kind")
            or payload.get("kind")
            or payload.get("barcode_type")
            or payload.get("type")
            or ""
        ).strip().upper()
        if explicit_kind == "QR":
            return True

        parsed = urlparse(str(src or ""))
        route = parsed.path or ""
        query = parse_qs(parsed.query)
        if "/report/barcode/QR/" in route:
            return True
        query_type = str((query.get("barcode_type") or query.get("type") or [""])[0]).strip().upper()
        return route.rstrip("/") == "/report/barcode" and query_type == "QR"

    def _generate_local_nfc_icon_png(self) -> bytes:
        icon = Image.new("RGBA", (220, 90), (255, 255, 255, 255))
        draw = ImageDraw.Draw(icon)
        accent = (0, 0, 0, 255)
        secondary = (255, 255, 255, 255)

        draw.rounded_rectangle((6, 6, 214, 84), radius=20, outline=accent, width=4, fill=secondary)
        draw.rounded_rectangle((22, 22, 74, 62), radius=8, outline=accent, width=4)
        draw.arc((62, 14, 118, 70), start=-60, end=60, fill=accent, width=5)
        draw.arc((80, 8, 146, 76), start=-60, end=60, fill=accent, width=5)
        draw.arc((98, 2, 174, 82), start=-60, end=60, fill=accent, width=5)
        draw.text((154, 31), "NFC", fill=accent)

        output = BytesIO()
        icon.save(output, format="PNG")
        return output.getvalue()

    def _generate_local_qr_png(self, data: str, query: dict[str, list[str]]) -> bytes:
        qrcode = self._import_optional_module("qrcode")
        if qrcode is None:
            _logger.warning("QR receipt image generation skipped because qrcode module is unavailable")
            return b""
        try:
            from PIL import Image
        except ImportError:
            _logger.warning("QR receipt image generation skipped because Pillow is unavailable")
            return b""

        width = self._query_int(query, "width", 200) or self._query_int(query, "img_width", 200)
        height = self._query_int(query, "height", 200) or self._query_int(query, "img_height", 200)
        size = max(64, min(width, height))
        try:
            qr = qrcode.QRCode(border=2, box_size=8)
            qr.add_data(data)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white")
            if hasattr(image, "convert"):
                image = image.convert("RGB")
            if image.size != (size, size):
                image = image.resize((size, size), Image.Resampling.NEAREST)
            output = BytesIO()
            image.save(output, format="PNG")
            _logger.info("Generated local QR receipt image size=%s data_len=%s", image.size, len(data))
            return output.getvalue()
        except Exception:
            _logger.exception("Failed to generate local QR receipt image data_len=%s", len(data))
            return b""

    def _generate_local_code128_png(self, data: str, query: dict[str, list[str]]) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            return b""

        width = self._query_int(query, "width", 400)
        height = self._query_int(query, "height", 70)
        try:
            image = self._render_local_code128_image(data, width, height)
            if image is None:
                return b""
            resized = BytesIO()
            image.save(resized, format="PNG")
            _logger.info("Generated local Code128 receipt image size=%s data_len=%s", image.size, len(data))
            return resized.getvalue()
        except Exception:
            _logger.exception("Failed to generate local Code128 receipt image data=%s", data[:80])
            return b""
        return b""

    def _render_local_code128_image(self, data: str, width: int, height: int):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return None
        code_values = self._encode_code128_values(data)
        if not code_values:
            return None
        pattern = "".join(_CODE128_PATTERNS[value] for value in code_values)
        quiet_modules = 10
        total_modules = quiet_modules + sum(int(module) for module in pattern) + quiet_modules
        configured_module_width = os.getenv("IOT_CODE128_MODULE_WIDTH", "").strip()
        if configured_module_width.isdigit():
            module_width = max(1, int(configured_module_width))
        else:
            module_width = 2
        min_barcode_width = total_modules * module_width
        target_width = max(32, width, min_barcode_width)
        target_height = max(24, height)
        image = Image.new("RGB", (target_width, target_height), "white")
        draw = ImageDraw.Draw(image)
        top_padding = max(6, target_height // 8)
        bottom_padding = max(4, target_height // 10)
        available_height = max(16, target_height - top_padding - bottom_padding)
        barcode_width = total_modules * module_width
        left_padding = max(0, (target_width - barcode_width) // 2)
        x = left_padding + (quiet_modules * module_width)
        is_bar = True
        for token in pattern:
            bar_width = int(token) * module_width
            if is_bar:
                draw.rectangle(
                    (x, top_padding, x + bar_width - 1, top_padding + available_height - 1),
                    fill="black",
                )
            x += bar_width
            is_bar = not is_bar
        return image

    def _encode_code128_values(self, data: str) -> list[int]:
        normalized = str(data or "")
        if not normalized:
            return []
        if normalized.isdigit() and len(normalized) % 2 == 0:
            values = [105]
            for index in range(0, len(normalized), 2):
                values.append(int(normalized[index:index + 2]))
        else:
            values = [104]
            for char in normalized:
                code_point = ord(char)
                if code_point < 32 or code_point > 126:
                    return []
                values.append(code_point - 32)
        checksum = values[0]
        for index, value in enumerate(values[1:], start=1):
            checksum += value * index
        values.append(checksum % 103)
        values.append(106)
        return values

    def _query_int(self, query: dict[str, list[str]], key: str, default: int) -> int:
        raw_values = query.get(key) or []
        raw_value = str(raw_values[0]).strip() if raw_values else ""
        return int(raw_value) if raw_value.isdigit() else default

    def _maybe_convert_svg_to_png(self, src: str, image_data: bytes) -> bytes:
        if "image/svg+xml" not in src and not image_data.lstrip().startswith(b"<svg"):
            return image_data
        try:
            import cairosvg
        except (ImportError, OSError):
            return self._render_simple_svg_to_png(image_data)
        try:
            return cairosvg.svg2png(bytestring=image_data)
        except Exception:
            return self._render_simple_svg_to_png(image_data)

    def _render_simple_svg_to_png(self, image_data: bytes) -> bytes:
        try:
            from PIL import ImageDraw
        except ImportError:
            return b""
        try:
            root = ET.fromstring(image_data.decode("utf-8"))
        except Exception:
            return b""

        view_box = str(root.attrib.get("viewBox") or "").strip().split()
        vb_width = self._parse_svg_number(view_box[2]) if len(view_box) == 4 else None
        vb_height = self._parse_svg_number(view_box[3]) if len(view_box) == 4 else None
        width = self._parse_svg_number(str(root.attrib.get("width") or "")) or vb_width or 128
        height = self._parse_svg_number(str(root.attrib.get("height") or "")) or vb_height or 128
        if width <= 0 or height <= 0:
            return b""

        vb_x = self._parse_svg_number(view_box[0]) if len(view_box) == 4 else 0.0
        vb_y = self._parse_svg_number(view_box[1]) if len(view_box) == 4 else 0.0
        vb_width = vb_width or width
        vb_height = vb_height or height
        scale_x = width / float(vb_width) if vb_width else 1.0
        scale_y = height / float(vb_height) if vb_height else 1.0

        def scale_rect(x: float, y: float, rect_width: float, rect_height: float) -> tuple[float, float, float, float]:
            scaled_x = (x - (vb_x or 0.0)) * scale_x
            scaled_y = (y - (vb_y or 0.0)) * scale_y
            scaled_width = rect_width * scale_x
            scaled_height = rect_height * scale_y
            return scaled_x, scaled_y, scaled_width, scaled_height

        image = Image.new("RGB", (int(round(width)), int(round(height))), "white")
        draw = ImageDraw.Draw(image)

        template_rects: dict[str, tuple[float, float, str]] = {}
        all_elements = list(root.iter())
        href_keys = (
            "{http://www.w3.org/1999/xlink}href",
            "href",
        )
        for element in all_elements:
            tag = element.tag.rsplit("}", 1)[-1]
            if tag != "rect":
                continue
            rect_width = self._parse_svg_number(str(element.attrib.get("width") or "")) or 0
            rect_height = self._parse_svg_number(str(element.attrib.get("height") or "")) or 0
            fill = str(element.attrib.get("fill") or "#000000").strip() or "#000000"
            rect_id = str(element.attrib.get("id") or "").strip()
            x = self._parse_svg_number(str(element.attrib.get("x") or "0")) or 0
            y = self._parse_svg_number(str(element.attrib.get("y") or "0")) or 0
            if rect_id:
                template_rects[rect_id] = (rect_width, rect_height, fill)
                continue
            if rect_width > 0 and rect_height > 0 and fill.lower() not in {"#ffffff", "white", "none"}:
                scaled_x, scaled_y, scaled_width, scaled_height = scale_rect(x, y, rect_width, rect_height)
                draw.rectangle(
                    [
                        scaled_x,
                        scaled_y,
                        scaled_x + scaled_width - 1,
                        scaled_y + scaled_height - 1,
                    ],
                    fill=fill,
                )

        for element in all_elements:
            tag = element.tag.rsplit("}", 1)[-1]
            if tag != "use":
                continue
            href = ""
            for key in href_keys:
                href = str(element.attrib.get(key) or "").strip()
                if href:
                    break
            if not href.startswith("#"):
                continue
            template = template_rects.get(href[1:])
            if not template:
                continue
            rect_width, rect_height, fill = template
            if rect_width <= 0 or rect_height <= 0 or fill.lower() in {"#ffffff", "white", "none"}:
                continue
            x = self._parse_svg_number(str(element.attrib.get("x") or "0")) or 0
            y = self._parse_svg_number(str(element.attrib.get("y") or "0")) or 0
            scaled_x, scaled_y, scaled_width, scaled_height = scale_rect(x, y, rect_width, rect_height)
            draw.rectangle(
                [
                    scaled_x,
                    scaled_y,
                    scaled_x + scaled_width - 1,
                    scaled_y + scaled_height - 1,
                ],
                fill=fill,
            )

        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _parse_svg_number(self, raw: str) -> float | None:
        text = str(raw or "").strip()
        if not text:
            return None
        match = re.match(r"^-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    def _image_to_escpos_raster(self, image) -> bytes:
        width, height = image.size
        max_band_height = int(os.getenv("IOT_ESCPOS_RASTER_BAND_HEIGHT", "240") or "240")
        max_band_height = max(24, min(512, max_band_height))
        if height > max_band_height:
            return b"".join(
                self._image_to_escpos_raster_band(image.crop((0, band_top, width, min(height, band_top + max_band_height))))
                for band_top in range(0, height, max_band_height)
            )

        return self._image_to_escpos_raster_band(image)

    def _image_to_escpos_raster_band(self, image) -> bytes:
        width, height = image.size
        width_bytes = (width + 7) // 8
        data = bytearray()
        pixels = image.load()
        for y in range(height):
            row = 0
            bit_count = 0
            for x in range(width):
                bit = 0 if pixels[x, y] else 1
                row = (row << 1) | bit
                bit_count += 1
                if bit_count == 8:
                    data.append(row)
                    row = 0
                    bit_count = 0
            if bit_count:
                row <<= 8 - bit_count
                data.append(row)
        xL = width_bytes % 256
        xH = width_bytes // 256
        yL = height % 256
        yH = height // 256
        return b"\x1dv0\x00" + bytes([xL, xH, yL, yH]) + bytes(data)

    def _image_to_escpos_bit_image(self, image) -> bytes:
        width, height = image.size
        width_bytes = (width + 7) // 8
        pixels = image.load()
        chunks = bytearray()
        for band_top in range(0, height, 24):
            band_height = min(24, height - band_top)
            chunks.extend(b"\x1b\x33\x18")
            chunks.extend(b"\x1b*\x21" + bytes([width_bytes % 256, width_bytes // 256]))
            for x_byte in range(width_bytes):
                for stripe in range(3):
                    value = 0
                    for bit in range(8):
                        y = band_top + (stripe * 8) + bit
                        x_base = x_byte * 8
                        byte_value = 0
                        for x_offset in range(8):
                            x = x_base + x_offset
                            pixel_on = y < height and x < width and not pixels[x, y]
                            byte_value = (byte_value << 1) | (1 if pixel_on else 0)
                        chunks.append(byte_value)
            chunks.extend(b"\n")
            if band_height < 24:
                chunks.extend(b"\n")
        chunks.extend(b"\x1b\x32")
        return bytes(chunks)

    def _normalize_currency_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = str(text)
        for variant in ("芒鈥毬?", "\u0080"):
            normalized = normalized.replace(variant, "€")
        return normalized

    def _normalize_spanish_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = str(text)
        normalized = normalized.replace("驴", "?").replace("隆", "!")
        normalized = normalized.replace("潞", "o").replace("陋", "a")
        decomposed = unicodedata.normalize("NFKD", normalized)
        return "".join(char for char in decomposed if not unicodedata.combining(char))

    def _repair_receipt_mojibake(self, text: str) -> str:
        if not text:
            return ""
        source = str(text)
        if not self._looks_like_receipt_mojibake(source):
            return source
        candidates = [source]
        for source_encoding in ("gbk", "gb18030"):
            try:
                repaired = source.encode(source_encoding, errors="ignore").decode("utf-8", errors="ignore")
            except Exception:
                continue
            if repaired and repaired not in candidates:
                candidates.append(repaired)
        return max(candidates, key=self._receipt_text_quality)

    def _looks_like_receipt_mojibake(self, text: str) -> bool:
        if not text:
            return False
        suspicious_fragments = (
            "鈧",
            "锟",
            "�",
            "Ã",
            "Â",
            "ð",
            "Ñ",
            "æ",
            "ç",
            "铆",
            "涓",
            "闂",
            "閿",
            "鍙",
            "浠",
            "銆",
        )
        if any(fragment in text for fragment in suspicious_fragments):
            return True
        if any(marker in text for marker in ("娑", "閸", "妤", "閺", "閳", "閿", "閵", "闂")):
            return True
        return any(unicodedata.category(char) in {"Cc", "Cf", "Co"} for char in text)

    def _receipt_text_quality(self, text: str) -> int:
        score = 0
        suspicious_fragments = ("铆", "涓", "闂", "閿", "鍙", "浠", "銆", "鈧", "锟", "Ã", "Â")
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                score += 4
            elif char.isalnum():
                score += 1
            elif char == "?":
                score -= 3
            elif unicodedata.category(char).startswith("C"):
                score -= 5
        if any(fragment in text for fragment in suspicious_fragments):
            score -= 20
        if any(marker in text for marker in ("娑", "閸", "妤", "閺", "閳", "閿", "閵", "闂")):
            score -= 12
        return score

    def _escpos_safe_text(self, text: str, encoding: str) -> str:
        normalized = self._normalize_currency_text(self._normalize_print_text(text))
        try:
            normalized.encode(encoding)
            return normalized
        except UnicodeEncodeError:
            sanitized = self._normalize_spanish_text(normalized)
            try:
                sanitized.encode(encoding)
                return sanitized
            except UnicodeEncodeError:
                return sanitized.replace("€", "EUR")

    def _text_width(self, text: str) -> int:
        text = self._normalize_currency_text(text)
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in {"W", "F"}:
                width += 2
            else:
                width += 1
        return width

    def _pad_right(self, text: str, width: int) -> str:
        return text + " " * max(0, width - self._text_width(text))

    def _pad_left(self, text: str, width: int) -> str:
        return " " * max(0, width - self._text_width(text)) + text

    def _pad_center(self, text: str, width: int) -> str:
        total = max(0, width - self._text_width(text))
        left = total // 2
        right = total - left
        return (" " * left) + text + (" " * right)

    def _escpos_align(self, align: str) -> bytes:
        if align == "center":
            return b"\x1ba\x01"
        if align == "right":
            return b"\x1ba\x02"
        return b"\x1ba\x00"

    def _escpos_emphasis(self, enabled: bool) -> bytes:
        return b"\x1bE\x01" if enabled else b"\x1bE\x00"

    def _normalize_escpos_multiplier(self, value: Any) -> int:
        if isinstance(value, bool):
            return 2 if value else 1
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(8, normalized))

    def _line_size_multipliers(self, line: dict[str, Any]) -> tuple[int, int]:
        width_multiplier = self._normalize_escpos_multiplier(
            line.get("width_multiplier", line.get("double_width"))
        )
        height_multiplier = self._normalize_escpos_multiplier(
            line.get("height_multiplier", line.get("double_height"))
        )
        return width_multiplier, height_multiplier

    def _escpos_size(self, double_width: Any, double_height: Any = False) -> bytes:
        width_multiplier = self._normalize_escpos_multiplier(double_width)
        height_multiplier = self._normalize_escpos_multiplier(double_height)
        size = ((width_multiplier - 1) << 4) | (height_multiplier - 1)
        return bytes([0x1D, 0x21, size])
