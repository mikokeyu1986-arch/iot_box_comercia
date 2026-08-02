from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from time import time
from typing import Any, Callable

from .dev_logger import dev_log, summarize_action
from .event_bus import EventBus
from .models import Device, IoTEvent
from .devices.discovery import DeviceDiscoveryMixin
from .printing.barcode import BarcodeMixin
from .printing.escpos import EscposEncodingMixin
from .printing.image_renderer import ImageRendererMixin
from .printing.network_printer import NetworkPrinterMixin
from .printing.normalization import ReceiptNormalizationMixin
from .printing.product_parser import ProductParserMixin
from .printing.receipt_metadata import ReceiptMetadataMixin
from .printing.section_consumers import ReceiptSectionConsumerMixin
from .printing.text_layout import TextLayoutMixin
from .printing.windows_printer import WindowsPrinterMixin
from .receipts.processing import ReceiptProcessingMixin
from .receipts.structured import StructuredReceiptMixin

_logger = logging.getLogger(__name__)

class DeviceManager(
    DeviceDiscoveryMixin,
    ReceiptProcessingMixin,
    StructuredReceiptMixin,
    NetworkPrinterMixin,
    WindowsPrinterMixin,
    EscposEncodingMixin,
    ReceiptNormalizationMixin,
    ReceiptSectionConsumerMixin,
    ProductParserMixin,
    ReceiptMetadataMixin,
    ImageRendererMixin,
    BarcodeMixin,
    TextLayoutMixin,
):
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
