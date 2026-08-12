from __future__ import annotations

import unittest
from unittest.mock import patch

from app.devices.discovery import DeviceDiscoveryMixin
from app.models import Device
from app.printing.network_printer import NetworkPrinterMixin


class DiscoveryHarness(DeviceDiscoveryMixin):
    def __init__(self, config: dict) -> None:
        self._config = config

    def local_config_getter(self) -> dict:
        return self._config

    @staticmethod
    def _is_windows_native_printing_available() -> bool:
        return False


class NetworkPrinterHarness(NetworkPrinterMixin):
    def __init__(self, config: dict) -> None:
        self._config = config

    def local_config_getter(self) -> dict:
        return self._config

    @staticmethod
    def _configured_printer_identifier() -> str:
        return ""


class EpsonNetworkDiscoveryTests(unittest.TestCase):
    def test_reachable_tcp_9100_host_is_registered_as_raw_network_printer(self):
        config = {
            "epson_discovery_enabled": True,
            "epson_printer_hosts": ["192.168.10.25"],
            "epson_discovery_subnets": [],
        }
        with patch.object(DiscoveryHarness, "_local_ipv4_hosts", return_value=set()), patch.object(
            DiscoveryHarness, "_tcp_port_is_open", return_value=True
        ):
            manager = DiscoveryHarness(config)
            devices = manager._discover_printer_devices()

        device = devices["epson_tcp_192_168_10_25"]
        self.assertEqual(device.connection, "network")
        self.assertEqual(device.manufacturer, "EPSON")
        self.assertEqual(device.metadata["raw_tcp_host"], "192.168.10.25")
        self.assertEqual(device.metadata["raw_tcp_port"], 9100)
        self.assertIs(devices["printer_main"], device)

    def test_device_specific_tcp_port_is_used_for_printing(self):
        config = {"epson_discovery_enabled": False, "raw_printer_port": 9999}
        manager = NetworkPrinterHarness(config)
        device = Device(
            identifier="epson_tcp_192_168_10_25",
            name="Epson Network Printer",
            type="printer",
            connection="network",
            metadata={"raw_tcp_host": "192.168.10.25", "raw_tcp_port": 9100},
        )
        self.assertEqual(manager._raw_tcp_endpoint(device), ("192.168.10.25", 9100))


if __name__ == "__main__":
    unittest.main()
