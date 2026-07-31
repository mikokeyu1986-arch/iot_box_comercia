from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

try:
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

BASE_DIR = Path(__file__).resolve().parent
RUN_HTTP_SCRIPT = BASE_DIR / "run_http.py"
RUN_HTTPS_SCRIPT = BASE_DIR / "run_https.py"
CONFIG_PATH = BASE_DIR / "runtime_config.json"
CONFIG_HTTP_PATH = BASE_DIR / "runtime_config_http.json"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "IoTBoxRuntime"


class TrayApp:
    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._is_running = False
        self._port = 8069
        self._host = "0.0.0.0"
        self._use_https = False  # 默认 HTTP
        self._status_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._icon: Any = None
        self._settings_window: Any = None

    @property
    def is_service_running(self) -> bool:
        return self._is_running and self._process is not None and self._process.poll() is None

    def _load_server_config(self) -> dict[str, Any]:
        """从配置文件加载服务器设置"""
        # 优先读取 HTTP 配置文件
        config_file = CONFIG_HTTP_PATH if CONFIG_HTTP_PATH.exists() else CONFIG_PATH
        try:
            if config_file.exists():
                data = json.loads(config_file.read_text(encoding="utf-8"))
                local = data.get("local_config", {})
                return {
                    "ssl_engine": str(local.get("ssl_engine", "plain_http")),
                    "local_url": str(local.get("local_url", "")),
                    "host": str(data.get("host", self._host)),
                    "port": int(data.get("port", self._port)),
                }
        except Exception:
            pass

        # 回退到 runtime_config.json
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                local = data.get("local_config", {})
                return {
                    "ssl_engine": str(local.get("ssl_engine", "plain_http")),
                    "local_url": str(local.get("local_url", "")),
                    "host": str(data.get("host", self._host)),
                    "port": int(data.get("port", self._port)),
                }
        except Exception:
            pass
        return {"ssl_engine": "plain_http", "local_url": "", "host": self._host, "port": self._port}

    def _get_server_url(self) -> str:
        config = self._load_server_config()
        local_url = config.get("local_url", "")
        if local_url:
            return local_url
        port = config.get("port", self._port)
        scheme = "https" if config.get("ssl_engine") == "secure_https" else "http"
        return f"{scheme}://127.0.0.1:{port}"

    def get_current_protocol(self) -> str:
        """返回当前协议 'http' 或 'https'"""
        config = self._load_server_config()
        return "https" if config.get("ssl_engine") == "secure_https" else "http"

    def start_service(self, protocol: str | None = None) -> dict[str, Any]:
        """启动服务，protocol 可选 'http' 或 'https'"""
        if self.is_service_running:
            return {"status": "already_running", "protocol": self.get_current_protocol()}

        # 确定协议
        if protocol in ("http", "https"):
            self._use_https = (protocol == "https")
        else:
            config = self._load_server_config()
            self._use_https = (config.get("ssl_engine") == "secure_https")

        script = RUN_HTTPS_SCRIPT if self._use_https else RUN_HTTP_SCRIPT
        proto_label = "HTTPS" if self._use_https else "HTTP"

        python_exe = self._find_python()
        if not python_exe:
            return {"status": "error", "message": "Python not found"}

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["IOT_ESCPOS_ENCODING"] = "gb18030"

        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_suffix = "https" if self._use_https else "http"
        stdout_log = log_dir / f"{log_suffix}_stdout.log"
        stderr_log = log_dir / f"{log_suffix}_stderr.log"

        try:
            with open(stdout_log, "a", encoding="utf-8") as fout, open(stderr_log, "a", encoding="utf-8") as ferr:
                self._process = subprocess.Popen(
                    [python_exe, str(script)],
                    cwd=str(BASE_DIR),
                    env=env,
                    stdout=fout,
                    stderr=ferr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            self._is_running = True
            self._start_status_monitor()
            return {"status": "started", "pid": self._process.pid, "protocol": proto_label}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def switch_protocol(self, protocol: str) -> dict[str, Any]:
        """切换 HTTP/HTTPS 并重启服务"""
        was_running = self.is_service_running
        if was_running:
            self.stop_service()
            time.sleep(1)
        # 更新配置
        self._use_https = (protocol == "https")
        try:
            self._save_protocol_config(protocol)
        except Exception as e:
            return {"status": "error", "message": f"更新配置失败: {e}"}
        if was_running:
            return self.start_service(protocol=protocol)
        return {"status": "switched", "protocol": protocol.upper()}

    def _save_protocol_config(self, protocol: str) -> None:
        """保存协议配置到文件"""
        config_file = CONFIG_HTTP_PATH if CONFIG_HTTP_PATH.exists() else CONFIG_PATH
        if config_file.exists():
            data = json.loads(config_file.read_text(encoding="utf-8"))
        else:
            data = {}

        if "local_config" not in data:
            data["local_config"] = {}
        data["local_config"]["ssl_engine"] = "secure_https" if protocol == "https" else "plain_http"

        if protocol == "https":
            data["port"] = data.get("port", 8443)
        else:
            data["port"] = data.get("port", 8069)

        config_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def stop_service(self) -> dict[str, Any]:
        self._stop_event.set()
        if self._process is not None:
            try:
                if os.name == "nt":
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                else:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
            except Exception:
                pass
            self._process = None
        self._is_running = False
        return {"status": "stopped"}

    def restart_service(self) -> dict[str, Any]:
        self.stop_service()
        time.sleep(1)
        return self.start_service()

    def _find_python(self) -> str:
        if sys.executable and "python" in sys.executable.lower():
            return sys.executable
        for name in ("python", "python3", "py"):
            try:
                result = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    return name
            except Exception:
                continue
        return ""

    def _start_status_monitor(self) -> None:
        self._stop_event.clear()

        def monitor():
            while not self._stop_event.is_set():
                if self._process and self._process.poll() is not None:
                    self._is_running = False
                    self._update_icon_menu()
                    break
                self._stop_event.wait(2)

        self._status_thread = threading.Thread(target=monitor, daemon=True)
        self._status_thread.start()

    def _update_icon_menu(self) -> None:
        pass

    def open_web_ui(self) -> None:
        url = self._get_server_url()
        webbrowser.open(url)

    def open_settings(self) -> None:
        """打开 GUI 设置窗口"""
        self._launch_gui()

    def _launch_gui(self) -> None:
        """启动 GUI 设置窗口"""
        try:
            from gui_app import SettingsWindow
            import tkinter as tk
            from app.config_store import ConfigStore

            # 如果已有窗口，则置顶
            if self._settings_window is not None:
                try:
                    self._settings_window.deiconify()
                    self._settings_window.lift()
                    self._settings_window.focus_force()
                    return
                except Exception:
                    pass

            root = tk.Tk()
            root.withdraw()
            self._settings_window = SettingsWindow(root, ConfigStore())
            # 劫持窗口关闭，不退出整个程序
            self._settings_window.mainloop()
        except Exception as e:
            print(f"无法启动 GUI: {e}")
            self.open_web_ui()

    def _check_update_notify(self, icon=None) -> None:
        """在后台检查更新并弹窗通知"""
        def do_check():
            try:
                from app.updater import UpdateManager, UpdateSource, DEFAULT_UPDATE_MANIFEST_URL
                mgr = UpdateManager(current_version="2026.07.31", base_dir=BASE_DIR)
                result = mgr.check_for_updates()
                if result.can_update:
                    if icon:
                        icon.notify(
                            f"发现新版本: {result.latest_version}\n{result.message}",
                            "IoT Box 更新",
                        )
                    else:
                        print(f"[更新] 发现新版本: {result.latest_version}")
                else:
                    if icon:
                        icon.notify("已是最新版本", "IoT Box 更新")
                    else:
                        print("[更新] 已是最新版本")
            except Exception as e:
                if icon:
                    icon.notify(f"检查更新失败: {e}", "IoT Box 更新")

        threading.Thread(target=do_check, daemon=True).start()

    def open_logs_folder(self) -> None:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(log_dir))  # type: ignore[attr-defined]
        else:
            webbrowser.open(str(log_dir))

    def set_auto_start(self, enabled: bool) -> None:
        if os.name != "nt":
            return
        try:
            import winreg
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY)
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, str(Path(sys.executable)))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception:
            pass

    def is_auto_start_enabled(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ)
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return bool(value)
        except (FileNotFoundError, OSError):
            return False

    def create_icon_image(self) -> Any:
        try:
            img = Image.new("RGB", (64, 64), (30, 41, 59))
            draw = ImageDraw.Draw(img)
            draw.ellipse([16, 8, 48, 40], fill=(59, 130, 246), outline=(147, 197, 253), width=2)
            draw.rectangle([20, 40, 44, 56], fill=(59, 130, 246))
            return img
        except Exception:
            return None

    def run(self) -> None:
        if not HAS_TRAY:
            print("pystray and Pillow are required for tray mode.")
            print("Install with: pip install pystray pillow")
            print()
            print("You can still run the service directly with:")
            print(f"  python {RUN_HTTPS_SCRIPT}")
            print()
            # 降级：启动 GUI 设置窗口
            self._launch_gui()
            return

        icon_image = self.create_icon_image()

        def on_start_http(icon, item):
            result = self.start_service(protocol="http")
            if result.get("status") in ("started", "already_running"):
                self.open_web_ui()

        def on_start_https(icon, item):
            result = self.start_service(protocol="https")
            if result.get("status") in ("started", "already_running"):
                self.open_web_ui()

        def on_stop(icon, item):
            self.stop_service()

        def on_restart(icon, item):
            self.restart_service()

        def on_open(icon, item):
            self.open_web_ui()

        def on_settings(icon, item):
            self.open_settings()

        def on_switch_http(icon, item):
            self.switch_protocol("http")

        def on_switch_https(icon, item):
            self.switch_protocol("https")

        def on_check_update(icon, item):
            self._check_update_notify(icon)

        def on_logs(icon, item):
            self.open_logs_folder()

        def on_auto_start(icon, item):
            current = self.is_auto_start_enabled()
            self.set_auto_start(not current)

        def on_quit(icon, item):
            self.stop_service()
            icon.stop()

        def is_running_check(item):
            return self.is_service_running

        def is_not_running_check(item):
            return not self.is_service_running

        menu = pystray.Menu(
            item(lambda icon, item: f"IoT Box {'运行中' if self.is_service_running else '已停止'}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("⚙  打开设置面板", on_settings),
            pystray.Menu.SEPARATOR,
            item("▶  启动 (HTTP)", on_start_http, enabled=is_not_running_check),
            item("▶  启动 (HTTPS)", on_start_https, enabled=is_not_running_check),
            item("■  停止服务", on_stop, enabled=is_running_check),
            item("↻  重启服务", on_restart, enabled=is_running_check),
            pystray.Menu.SEPARATOR,
            item("切换到 HTTP", on_switch_http, enabled=is_running_check),
            item("切换到 HTTPS", on_switch_https, enabled=is_running_check),
            pystray.Menu.SEPARATOR,
            item("打开网页管理端", on_open),
            item("查看日志", on_logs),
            item("检查更新", on_check_update),
            pystray.Menu.SEPARATOR,
            item("开机自启", on_auto_start, checked=lambda item: self.is_auto_start_enabled()),
            pystray.Menu.SEPARATOR,
            item("退出", on_quit),
        )

        self._icon = pystray.Icon(
            "iot_box",
            icon_image,
            "IoT Box Runtime",
            menu,
        )

        self._icon.run()


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print("IoT Box Runtime - 系统托盘管理程序")
        print()
        print("用法:")
        print("  python tray_app.py                # 启动托盘程序")
        print("  python tray_app.py --start        # 启动服务（HTTP）")
        print("  python tray_app.py --start-https  # 启动服务（HTTPS）")
        print("  python tray_app.py --stop         # 停止服务")
        print("  python tray_app.py --restart      # 重启服务")
        print("  python tray_app.py --open         # 打开管理页面")
        print("  python tray_app.py --settings     # 打开 GUI 设置面板")
        print("  python tray_app.py --http         # 切换到 HTTP 并重启")
        print("  python tray_app.py --https        # 切换到 HTTPS 并重启")
        print("  python tray_app.py --check-update # 检查更新")
        print()
        print("依赖: pystray, pillow, pyserial")
        return

    app = TrayApp()

    if "--start-https" in sys.argv:
        result = app.start_service(protocol="https")
        print(f"启动 (HTTPS): {result}")
        if result.get("status") in ("started", "already_running"):
            app.open_web_ui()
        return

    if "--start" in sys.argv:
        result = app.start_service(protocol="http")
        print(f"启动 (HTTP): {result}")
        if result.get("status") in ("started", "already_running"):
            app.open_web_ui()
        return

    if "--stop" in sys.argv:
        result = app.stop_service()
        print(f"停止: {result}")
        return

    if "--restart" in sys.argv:
        result = app.restart_service()
        print(f"重启: {result}")
        return

    if "--open" in sys.argv:
        app.open_web_ui()
        return

    if "--settings" in sys.argv or "--gui" in sys.argv:
        app.open_settings()
        return

    if "--http" in sys.argv:
        result = app.switch_protocol("http")
        print(f"切换 HTTP: {result}")
        return

    if "--https" in sys.argv:
        result = app.switch_protocol("https")
        print(f"切换 HTTPS: {result}")
        return

    if "--check-update" in sys.argv:
        app._check_update_notify()
        return

    app.run()


if __name__ == "__main__":
    main()
