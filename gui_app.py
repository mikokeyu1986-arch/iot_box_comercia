"""
IoT Box Desktop - 桌面 GUI 管理工具

功能：
- 系统托盘图标 & 右键菜单
- 服务控制（启动/停止/重启）
- HTTP / HTTPS 协议切换
- 服务器 URL 配置（Odoo 连接）
- 电子秤配置（串口 / 品牌 / 协议）
- 在线更新检查 & 安装
- 服务状态实时监控
"""

from __future__ import annotations

import logging
import json
import re
import os
import platform
import subprocess
import socket
import sys
import threading
import time
import webbrowser
import ctypes
from urllib.request import Request, urlopen
from pathlib import Path
import tkinter as tk
from tkinter import Tk, Frame, Label, Button, Entry, StringVar, IntVar, BooleanVar
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
from typing import Any

# 将项目根目录加入 path
BASE_DIR = Path(__file__).resolve().parent
REDSYS_DIR = BASE_DIR / "redsys"
REDSYS_SCRIPT = REDSYS_DIR / "server" / "main.py"
REDSYS_CONFIG = REDSYS_DIR / "config.yaml"
sys.path.insert(0, str(BASE_DIR))

from app.config_store import ConfigStore
from app.updater import UpdateManager, UpdateSource, GitHubUpdateSource, DEFAULT_UPDATE_MANIFEST_URL

_logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

APP_NAME = "IoT Box Desktop"
APP_VERSION = "2026.08.02"
_CONFIG_HTTP = BASE_DIR / "runtime_config_http.json"
_CONFIG_DEFAULT = BASE_DIR / "runtime_config.json"
CONFIG_FILE = _CONFIG_HTTP if _CONFIG_HTTP.exists() else _CONFIG_DEFAULT

DEFAULT_ODOO_URL = "http://192.168.1.1:8069"
DEFAULT_TOKEN_URL = "http://192.168.1.1:8069"
DEFAULT_SCALE_PORT = "COM3"
DEFAULT_SCALE_BAUDRATE = 9600

# IoT Box 服务端口（与 run_http.py / run_https.py 一致）
# HTTP 模式: IOT_HTTP_PORT 默认 8399；HTTPS 模式: IOT_PORT 默认 8398
HTTP_PORT = 8399
HTTPS_PORT = 8398

# 电子秤品牌预设 —— 与 app/drivers/scale.py 中 ScaleConfig 支持的品牌一致。
# runtime 只认 zfoc 和 epelsa 两个 brand，其他品牌不会被 ScaleMonitor 正确解析。
SCALE_PRESETS: dict[str, dict[str, Any]] = {
    "Gram Zfoc-p (ZFOC)": {
        "brand": "zfoc",
        "baudrate": 9600,
        "timeout": 1.2,
        "inter_command_delay": 0.05,
    },
    "Epelsa 56 PPI": {
        "brand": "epelsa",
        "baudrate": 9600,
        "timeout": 1.2,
        "inter_command_delay": 0.05,
    },
}

# 服务控制脚本
HTTP_SCRIPT = BASE_DIR / "run_http.py"
HTTPS_SCRIPT = BASE_DIR / "run_https.py"


# ============================================================================
# 设置窗口
# ============================================================================


class SettingsWindow(tk.Toplevel):
    """主设置窗口（从托盘菜单打开）"""

    def __init__(self, master: tk.Tk | None, config_store: ConfigStore) -> None:
        super().__init__(master)
        self.config_store = config_store
        self.title("IoT Box — 设备管理器")
        self.geometry("750x580")
        self.minsize(680, 480)
        self.resizable(True, True)

        # 居中
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # 图标（如可用）
        self._set_icon()

        # 构建 UI
        self._build_notebook()
        self._load_config()

        # 自动启动服务
        if self.auto_start_var.get():
            self.after(500, self._on_start)
        if self.customer_display_enabled_var.get():
            self.after(1800, self._launch_customer_display_screen)
        self.after(60000, self._auto_update_check)

    # ------------------------------------------------------------------

    def _set_icon(self) -> None:
        try:
            ico = BASE_DIR / "web" / "favicon.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

    def _on_window_close(self) -> None:
        """退出 IoT Box 时同步关闭独立客户显示屏。"""
        self._close_customer_display_screen()
        self.destroy()

    # ------------------------------------------------------------------

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        # ---- Tab 1: 服务控制 ----
        self.tab_service = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_service, text="  服务控制  ")
        self._build_service_tab()

        # ---- Tab 2: 服务器配置 ----
        self.tab_server = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_server, text="  服务器  ")
        self._build_server_tab()

        # ---- Tab 3: 电子秤配置 ----
        self.tab_scale = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_scale, text="  电子秤  ")
        self._build_scale_tab()
        self.tab_customer_display = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_customer_display, text="  Pantalla del cliente  ")
        self._build_customer_display_tab()
        self.tab_redsys = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_redsys, text="  Redsys  ")
        self._build_redsys_tab()

        # ---- Tab 4: 关于 & 更新 ----
        self.tab_about = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_about, text="  关于 & 更新  ")
        self._build_about_tab()

    # ==================================================================
    # 服务控制 Tab
    # ==================================================================

    def _build_service_tab(self) -> None:
        f = self.tab_service

        # -- 状态区 --
        status_frame = ttk.LabelFrame(f, text="服务状态", padding=10)
        status_frame.pack(fill="x", padx=10, pady=10)

        self.lbl_status = ttk.Label(status_frame, text="● 服务状态: 未运行", font=("Segoe UI", 11))
        self.lbl_status.pack(anchor="w")

        self.lbl_protocol = ttk.Label(status_frame, text="协议: --", font=("Segoe UI", 10))
        self.lbl_protocol.pack(anchor="w", pady=(5, 0))

        self.lbl_uptime = ttk.Label(status_frame, text="运行时间: --", font=("Segoe UI", 10))
        self.lbl_uptime.pack(anchor="w")

        # -- 协议切换区 --
        proto_frame = ttk.LabelFrame(f, text="协议切换", padding=10)
        proto_frame.pack(fill="x", padx=10, pady=10)

        self.proto_var = StringVar(value="https")
        rb_http = ttk.Radiobutton(proto_frame, text=f"HTTP  (端口 {HTTP_PORT})", variable=self.proto_var, value="http")
        rb_https = ttk.Radiobutton(proto_frame, text=f"HTTPS (端口 {HTTPS_PORT})", variable=self.proto_var, value="https")
        rb_http.pack(side="left", padx=(0, 20))
        rb_https.pack(side="left")

        self.auto_start_var = BooleanVar(value=True)
        ttk.Checkbutton(proto_frame, text="启动 GUI 后自动启动服务", variable=self.auto_start_var,
                        command=self._save_service_preferences).pack(side="left", padx=(20, 0))
        ttk.Button(proto_frame, text="保存协议设置", command=self._save_service_preferences).pack(side="right")

        # -- 动作按钮 --
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=10, pady=10)

        self.btn_start = ttk.Button(btn_frame, text="▶  启动服务", command=self._on_start)
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = ttk.Button(btn_frame, text="■  停止服务", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_restart = ttk.Button(btn_frame, text="↻  重启服务", command=self._on_restart, state="disabled")
        self.btn_restart.pack(side="left", padx=(0, 8))

        self.btn_open_web = ttk.Button(btn_frame, text="🌐  打开网页界面", command=self._on_open_web)
        self.btn_open_web.pack(side="left")

        # -- 日志区 --
        log_frame = ttk.LabelFrame(f, text="服务日志", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.log_text = ScrolledText(log_frame, height=10, state="disabled", wrap="word",
                                      font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=True)

        self._append_log("IoT Box Desktop 已启动")

    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        proto = self.proto_var.get()
        port = HTTPS_PORT if proto == "https" else HTTP_PORT
        if self._is_local_port_open(port):
            # A service started outside this GUI is still a valid running service.
            self._on_service_started(proto)
        self._append_log(f"正在以 {proto.upper()} 模式启动服务…")
        script = HTTP_SCRIPT if proto == "http" else HTTPS_SCRIPT
        threading.Thread(target=self._run_service, args=(script, proto), daemon=True).start()
        threading.Thread(target=self._run_redsys_service, daemon=True).start()

    def _run_redsys_service(self) -> None:
        if not self.redsys_enabled_var.get():
            self.after(0, self._append_log, "Redsys 服务已禁用")
            return
        if not REDSYS_SCRIPT.exists() or not REDSYS_CONFIG.exists():
            self.after(0, self._append_log, "Redsys 服务未找到，已跳过启动")
            return
        if getattr(self, "_redsys_proc", None) and self._redsys_proc.poll() is None:
            return
        if self._is_local_port_open(6971):
            self.after(0, self._append_log, "Redsys 6971 已有服务运行，不重复启动")
            return
        try:
            self._redsys_proc = subprocess.Popen(
                [sys.executable, str(REDSYS_SCRIPT), "--config", str(REDSYS_CONFIG)],
                cwd=str(REDSYS_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.redsys_status_var.set("Redsys 服务运行中: 127.0.0.1:6971")
            self.after(0, self._append_log, "Redsys 服务已启动: http://127.0.0.1:6971")
            # 服务启动后自动执行一次真实刷卡机连接，不需要用户再按按钮。
            threading.Thread(target=self._check_redsys_status, daemon=True).start()
            for line in self._redsys_proc.stdout:
                self.after(0, self._append_log, f"[Redsys] {line.strip()}")
        except Exception as exc:
            self.after(0, self._append_log, f"Redsys 启动失败: {exc}")

    def _check_redsys_status(self) -> None:
        try:
            with urlopen("http://127.0.0.1:6971/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            if health.get("simulate"):
                self.after(0, self.redsys_status_var.set, "Redsys 服务已启动（模拟模式）")
                return
            with urlopen("http://127.0.0.1:6971/connect", timeout=130) as response:
                result = json.loads(response.read().decode("utf-8"))
            connected = bool(result.get("connected"))
            message = str(result.get("message") or result.get("error") or "")
            status = "Redsys 刷卡机已连接" if connected else f"Redsys 刷卡机连接失败: {message}"
            self.after(0, self.redsys_status_var.set, status)
            self.after(0, self._append_log, status)
        except Exception as exc:
            self.after(0, self.redsys_status_var.set, f"Redsys 服务连接失败: {exc}")

    def _save_service_preferences(self) -> None:
        protocol = self.proto_var.get().strip().lower()
        if protocol not in {"http", "https"}:
            protocol = "https"
        self.config_store.update_local_config(
            service_protocol=protocol,
            auto_start_service=bool(self.auto_start_var.get()),
        )
        self._append_log(f"服务设置已保存: protocol={protocol}, auto_start={self.auto_start_var.get()}")

    def _run_service(self, script: Path, proto: str) -> None:
        try:
            port = HTTPS_PORT if proto == "https" else HTTP_PORT
            if self._is_local_port_open(port):
                self.after(0, self._append_log, f"端口 {port} 已有服务运行，不重复启动")
                return
            service_env = os.environ.copy()
            service_env["IOT_CONFIG_PATH"] = str(CONFIG_FILE)
            # European thermal-printer code page with a real Euro symbol.
            service_env["IOT_ESCPOS_ENCODING"] = "cp858"
            self._service_proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(BASE_DIR),
                env=service_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
            self.after(0, self._on_service_started, proto)
            for line in self._service_proc.stdout:
                self.after(0, self._append_log, line.strip())
        except Exception as e:
            self.after(0, self._append_log, f"启动失败: {e}")

    @staticmethod
    def _is_local_port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
                return True
        except OSError:
            return False

    def _on_service_started(self, proto: str) -> None:
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_restart.config(state="normal")
        self.lbl_status.config(text=f"● 服务状态: 运行中 ({proto.upper()})", foreground="green")
        self.lbl_protocol.config(text=f"协议: {proto.upper()}")
        self._start_time = time.time()
        self._update_uptime()

    def _stop_redsys_process(self) -> None:
        redsys_proc = getattr(self, "_redsys_proc", None)
        if not redsys_proc:
            return
        redsys_proc.terminate()
        try:
            redsys_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            redsys_proc.kill()
        self._redsys_proc = None

    def _stop_service_process(self) -> None:
        """停止服务子进程并等待退出（线程安全，不操作 GUI widget）。

        terminate() 后等待进程真正退出并释放端口，避免重启时端口仍被占用。
        """
        proc = getattr(self, "_service_proc", None)
        if not proc:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._service_proc = None
        self._stop_redsys_process()

    def _update_service_stopped_ui(self) -> None:
        """更新 GUI 显示服务已停止（仅主线程调用）"""
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_restart.config(state="disabled")
        self.lbl_status.config(text="● 服务状态: 已停止", foreground="red")
        self.lbl_uptime.config(text="运行时间: --")

    def _on_stop(self) -> None:
        self._stop_service_process()
        self._update_service_stopped_ui()
        self._append_log("服务已停止")

    def _on_restart(self) -> None:
        self._on_stop()
        self.after(1000, self._on_start)

    def _on_open_web(self) -> None:
        proto = self.proto_var.get()
        port = HTTP_PORT if proto == "http" else HTTPS_PORT
        url = f"{proto}://127.0.0.1:{port}"
        webbrowser.open(url)

    def _update_uptime(self) -> None:
        if hasattr(self, "_start_time") and self._start_time:
            elapsed = int(time.time() - self._start_time)
            h, m = divmod(elapsed, 3600)
            m, s = divmod(m, 60)
            self.lbl_uptime.config(text=f"运行时间: {h:02d}:{m:02d}:{s:02d}")
        if self.btn_start.cget("state") == "disabled":
            self.after(1000, self._update_uptime)

    def _append_log(self, text: str) -> None:
        # The GUI log is reserved for actionable failures.  Normal service
        # output (startup, status, successful prints, health checks, etc.) is
        # intentionally kept out of the panel so errors remain visible.
        message = str(text or "")
        error_markers = (
            "error", "exception", "traceback", "failed", "failure",
            "fatal", "critical", "stderr", "错误", "失败", "异常", "报错",
        )
        if not any(marker in message.casefold() for marker in error_markers):
            return
        self.log_text.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ==================================================================
    # 服务器配置 Tab（精简：单一 URL + 绑定状态）
    # ==================================================================

    def _build_server_tab(self) -> None:
        f = self.tab_server

        # -- Token 连接 URL --
        url_frame = ttk.LabelFrame(f, text="Odoo 连接地址", padding=15)
        url_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(url_frame, text="粘贴 Odoo 生成的 Token 连接地址:", font=("Segoe UI", 10)).pack(anchor="w")
        self.token_url_var = StringVar()
        url_entry = ttk.Entry(url_frame, textvariable=self.token_url_var, width=60)
        url_entry.pack(fill="x", pady=(5, 5))
        ttk.Label(url_frame, text="格式: http://地址:端口?token=xxx&db_uuid=xxx&enterprise_code=&db_name=xxx",
                  foreground="gray", font=("Segoe UI", 8)).pack(anchor="w")

        btn_row = ttk.Frame(url_frame)
        btn_row.pack(fill="x", pady=(10, 0))
        self.btn_connect = ttk.Button(btn_row, text="🔗  连接并绑定", command=self._on_connect_server)
        self.btn_connect.pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="断开", command=self._on_disconnect_server).pack(side="left")

        # -- 绑定状态 --
        status_frame = ttk.LabelFrame(f, text="绑定状态", padding=15)
        status_frame.pack(fill="x", padx=10, pady=10)

        self.lbl_bound_status = ttk.Label(status_frame, text="⏳ 未绑定", font=("Segoe UI", 11, "bold"),
                                           foreground="gray")
        self.lbl_bound_status.pack(anchor="w")

        self.lbl_bound_detail = ttk.Frame(status_frame)
        self.lbl_bound_detail.pack(fill="x", pady=(8, 0))
        self.lbl_bound_url = ttk.Label(self.lbl_bound_detail, text="", font=("Segoe UI", 9))
        self.lbl_bound_url.pack(anchor="w")
        self.lbl_bound_db = ttk.Label(self.lbl_bound_detail, text="", font=("Segoe UI", 9))
        self.lbl_bound_db.pack(anchor="w")
        self.lbl_bound_sync = ttk.Label(self.lbl_bound_detail, text="", font=("Segoe UI", 9))
        self.lbl_bound_sync.pack(anchor="w")

        # 初始加载绑定状态
        self._refresh_bound_status()

    def _refresh_bound_status(self) -> None:
        """从配置文件读取并显示当前绑定状态"""
        conn = self.config_store.get_connection()
        if conn.get("connected") and conn.get("url"):
            self.lbl_bound_status.config(text="✅ 已绑定", foreground="green")
            self.lbl_bound_url.config(text=f"服务器: {conn.get('url', '')}")
            self.lbl_bound_db.config(text=f"数据库: {conn.get('db_name', '')}")
            sync_ok = conn.get("last_sync_ok", False)
            sync_msg = conn.get("last_sync_message", "")
            if sync_ok:
                self.lbl_bound_sync.config(text="同步: ✅ 正常", foreground="green")
            elif sync_msg:
                self.lbl_bound_sync.config(text=f"同步: ❌ {sync_msg}", foreground="red")
            else:
                self.lbl_bound_sync.config(text="同步: 等待同步…", foreground="gray")
            self.token_url_var.set(
                f"{conn.get('url', '')}?token={conn.get('token', '')}"
                f"&db_uuid={conn.get('db_uuid', '')}"
                f"&enterprise_code={conn.get('enterprise_code', '')}"
                f"&db_name={conn.get('db_name', '')}"
            )
        else:
            self.lbl_bound_status.config(text="⏳ 未绑定", foreground="gray")
            self.lbl_bound_url.config(text="")
            self.lbl_bound_db.config(text="")
            self.lbl_bound_sync.config(text="")

    def _on_connect_server(self) -> None:
        """解析 token URL 并绑定"""
        token_url = self.token_url_var.get().strip()
        if not token_url:
            messagebox.showwarning("提示", "请先粘贴 Odoo Token 连接地址")
            return

        try:
            self.btn_connect.config(state="disabled", text="连接中…")
            self.config_store.connect_from_token_url(token_url)
            local_url = str(self.config_store.get_local_config().get("local_url") or "http://127.0.0.1:8399").rstrip("/")
            request = Request(
                f"{local_url}/api/connect",
                data=json.dumps({"token_url": token_url}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("server_connection"):
                self.config_store.update_connection(**result["server_connection"])
            self._refresh_bound_status()
            self._append_log(f"已绑定到服务器: {self.config_store.get_connection().get('url', '')}")
            messagebox.showinfo("成功", "已成功绑定 Odoo 服务器!")
        except ValueError as e:
            messagebox.showerror("绑定失败", str(e))
        except Exception as e:
            messagebox.showerror("错误", f"绑定失败: {e}")
        finally:
            self.btn_connect.config(state="normal", text="🔗  连接并绑定")

    def _on_disconnect_server(self) -> None:
        """解除绑定"""
        if not messagebox.askyesno("确认", "确定要断开与 Odoo 服务器的绑定吗?"):
            return
        try:
            self.config_store.reset_connection()
            self._refresh_bound_status()
            self.token_url_var.set("")
            self._append_log("已解除服务器绑定")
        except Exception as e:
            messagebox.showerror("错误", f"断开失败: {e}")

    # ==================================================================
    # 电子秤配置 Tab
    # ==================================================================

    def _build_scale_tab(self) -> None:
        f = self.tab_scale

        # 预设品牌
        brand_frame = ttk.LabelFrame(f, text="电子秤品牌预设", padding=10)
        brand_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(brand_frame, text="选择品牌快速应用预设参数:", font=("Segoe UI", 10)).pack(anchor="w")

        self.scale_brand_var = StringVar(value=list(SCALE_PRESETS.keys())[0])
        cb = ttk.Combobox(brand_frame, textvariable=self.scale_brand_var,
                           values=list(SCALE_PRESETS.keys()), state="readonly", width=30)
        cb.pack(pady=5)
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())

        ttk.Button(brand_frame, text="应用预设", command=self._apply_preset).pack(pady=5)

        # 串口设置
        port_frame = ttk.LabelFrame(f, text="串口设置", padding=10)
        port_frame.pack(fill="x", padx=10, pady=10)

        row1 = ttk.Frame(port_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="串口:", width=14).pack(side="left")
        self.scale_port_var = StringVar(value=DEFAULT_SCALE_PORT)
        self.port_combo = ttk.Combobox(row1, textvariable=self.scale_port_var, width=20)
        self.port_combo.pack(side="left")
        ttk.Button(row1, text="刷新串口", command=self._refresh_ports).pack(side="left", padx=5)
        self.lbl_port_status = ttk.Label(row1, text="", foreground="gray")
        self.lbl_port_status.pack(side="left", padx=5)

        row2 = ttk.Frame(port_frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="波特率:", width=14).pack(side="left")
        self.scale_baudrate_var = IntVar(value=DEFAULT_SCALE_BAUDRATE)
        ttk.Combobox(row2, textvariable=self.scale_baudrate_var,
                                values=[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200],
                                width=18).pack(side="left")

        row3 = ttk.Frame(port_frame)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="超时(秒):", width=14).pack(side="left")
        self.scale_timeout_var = StringVar(value="1.2")
        ttk.Entry(row3, textvariable=self.scale_timeout_var, width=20).pack(side="left")

        row4 = ttk.Frame(port_frame)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="指令间隔(秒):", width=14).pack(side="left")
        self.scale_inter_command_delay_var = StringVar(value="0.05")
        ttk.Entry(row4, textvariable=self.scale_inter_command_delay_var, width=20).pack(side="left")
        ttk.Label(row4, text="主动探测之间的最小间隔", foreground="gray").pack(side="left", padx=5)

        # 按钮
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="🧪  测试连接", command=self._on_test_scale).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="💾  保存电子秤配置", command=self._on_save_scale).pack(side="left")

    # ------------------------------------------------------------------

    def _build_customer_display_tab(self) -> None:
        frame = self.tab_customer_display
        local = self.config_store.get_local_config()
        self.customer_display_enabled_var = BooleanVar(value=bool(local.get("customer_display_enabled", False)))
        self.customer_display_url_var = StringVar(value=str(local.get("customer_display_url", "http://127.0.0.1:8070/pos/customer-display")))
        self.customer_display_status_var = StringVar(value="未配置")

        ttk.Checkbutton(
            frame,
            text="启用 Pantalla del cliente（客户显示屏）",
            variable=self.customer_display_enabled_var,
            command=self._save_customer_display_config,
        ).pack(anchor="w", padx=12, pady=12)
        form = ttk.LabelFrame(frame, text="客户显示屏连接", padding=10)
        form.pack(fill="x", padx=12, pady=5)
        for label, var in (("客户屏 URL", self.customer_display_url_var),):
            row = ttk.Frame(form)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, width=12).pack(side="left")
            ttk.Entry(row, textvariable=var, width=28).pack(side="left")
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", padx=12, pady=10)
        ttk.Button(buttons, text="保存配置", command=self._save_customer_display_config).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="启动第二屏", command=self._launch_customer_display_screen).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="关闭第二屏", command=self._close_customer_display_screen).pack(side="left")
        ttk.Label(frame, textvariable=self.customer_display_status_var, foreground="blue").pack(anchor="w", padx=12)

    def _save_customer_display_config(self) -> None:
        self.config_store.update_local_config(
            customer_display_enabled=bool(self.customer_display_enabled_var.get()),
            customer_display_url=self.customer_display_url_var.get().strip() or "http://127.0.0.1:8070/pos/customer-display",
        )
        self.customer_display_status_var.set("客户显示屏配置已保存，重启服务后生效")

    def _launch_customer_display_screen(self) -> None:
        self._save_customer_display_config()
        self._close_customer_display_screen()
        url = self.customer_display_url_var.get().strip()
        try:
            monitors = []
            callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long * 4), ctypes.c_double)
            def enum_proc(_monitor, _dc, rect, _data):
                monitors.append(tuple(rect.contents))
                return True
            ctypes.windll.user32.EnumDisplayMonitors(None, None, callback(enum_proc), 0)
            if len(monitors) < 2:
                self.customer_display_status_var.set("未检测到第二个显示器")
                return
            left, top, right, bottom = monitors[1]
            width, height = right - left, bottom - top
            helper = BASE_DIR / "customer_display_app.py"
            self.customer_display_proc = subprocess.Popen(
                [sys.executable, str(helper), "--url", url, "--x", str(left), "--y", str(top),
                 "--width", str(width), "--height", str(height)],
                cwd=str(BASE_DIR),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.customer_display_status_var.set(f"第二屏已启动: {right-left}x{bottom-top}")
        except Exception as exc:
            self.customer_display_status_var.set(f"启动第二屏失败: {exc}")

    def _close_customer_display_screen(self) -> None:
        self._stop_all_customer_display_processes()
        process = getattr(self, "customer_display_proc", None)
        if process is not None and process.poll() is None:
            process.terminate()
        self.customer_display_proc = None
        window = getattr(self, "customer_display_webview", None)
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
            self.customer_display_webview = None
        window = getattr(self, "customer_display_window", None)
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
            self.customer_display_window = None

    def _stop_all_customer_display_processes(self) -> None:
        """清理可能由旧版 GUI 留下的重复客户屏进程。"""
        try:
            subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -match 'customer_display_app.py' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
                ],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
        except Exception:
            pass

    def _run_customer_display_webview(self, url: str, left: int, top: int, width: int, height: int) -> None:
        try:
            import webview
            self.customer_display_webview = webview.create_window(
                "Pantalla del cliente",
                url=url,
                x=left,
                y=top,
                width=width,
                height=height,
                fullscreen=True,
                resizable=False,
            )
            webview.start(gui="edgechromium", debug=False)
        except Exception as exc:
            self.after(0, self.customer_display_status_var.set, f"客户屏幕启动失败: {exc}")

    def _build_redsys_tab(self) -> None:
        frame = self.tab_redsys
        self.redsys_enabled_var = BooleanVar(
            value=bool(self.config_store.get_local_config().get("redsys_enabled", True))
        )
        self.redsys_simulate_var = BooleanVar(value=False)
        self.redsys_port_var = StringVar(value="6971")
        self.redsys_merchant_var = StringVar(value="")
        self.redsys_terminal_var = StringVar(value="1")
        self.redsys_key_var = StringVar(value="")
        self.redsys_serial_var = StringVar(value="COM9")
        self.redsys_version_var = StringVar(value="6.1")
        self.redsys_status_var = StringVar(value="Redsys 服务未启动")

        self._load_redsys_config_values()

        ttk.Checkbutton(frame, text="启用 Redsys 服务", variable=self.redsys_enabled_var,
                        command=self._save_redsys_config).pack(anchor="w", padx=12, pady=10)
        form = ttk.LabelFrame(frame, text="Redsys 配置", padding=10)
        form.pack(fill="x", padx=12, pady=5)
        fields = [
            ("服务端口", self.redsys_port_var),
            ("商户号", self.redsys_merchant_var),
            ("终端号", self.redsys_terminal_var),
            ("签名密钥/密码", self.redsys_key_var),
            ("刷卡机串口", self.redsys_serial_var),
            ("TPV Version", self.redsys_version_var),
        ]
        for label, variable in fields:
            row = ttk.Frame(form)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=14).pack(side="left")
            if label == "服务端口":
                ttk.Entry(row, textvariable=variable, width=35, state="readonly").pack(side="left")
            elif label == "刷卡机串口":
                combo = ttk.Combobox(row, textvariable=variable, width=32, state="readonly")
                combo.pack(side="left")
                combo["values"] = self._list_serial_ports()
                if variable.get() and variable.get() not in combo["values"]:
                    combo["values"] = tuple(combo["values"]) + (variable.get(),)
                ttk.Button(row, text="刷新", command=lambda c=combo: self._refresh_redsys_ports(c)).pack(side="left", padx=5)
            else:
                entry_options = {"show": "*"} if variable is self.redsys_key_var else {}
                ttk.Entry(row, textvariable=variable, width=35, **entry_options).pack(side="left")
        ttk.Checkbutton(form, text="模拟模式", variable=self.redsys_simulate_var).pack(anchor="w", pady=5)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", padx=12, pady=8)
        ttk.Button(buttons, text="启动 Redsys", command=self._start_redsys_from_gui).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="连接刷卡机", command=self._connect_redsys_from_gui).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="停止 Redsys", command=self._stop_redsys_from_gui).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="保存 Redsys 配置", command=self._save_redsys_config).pack(side="left")
        ttk.Button(buttons, text="刷新状态", command=self._refresh_redsys_status).pack(side="left", padx=(8, 0))
        ttk.Label(frame, textvariable=self.redsys_status_var, foreground="blue").pack(anchor="w", padx=12)
        self.after(1500, self._poll_redsys_status)

    def _poll_redsys_status(self) -> None:
        self._refresh_redsys_status()
        self.after(5000, self._poll_redsys_status)

    def _refresh_redsys_status(self) -> None:
        threading.Thread(target=self._check_redsys_health, daemon=True).start()

    def _check_redsys_health(self) -> None:
        try:
            with urlopen("http://127.0.0.1:6971/health", timeout=3) as response:
                health = json.loads(response.read().decode("utf-8"))
            if health.get("simulate"):
                status = "服务在线（模拟模式）"
            else:
                configured_port = self.redsys_serial_var.get().strip().upper()
                detected_ports = [str(port).strip().upper() for port in self._list_serial_ports()]
                if configured_port and configured_port not in detected_ports:
                    status = f"服务在线，但未检测到 {configured_port} 刷卡机"
                else:
                    status = "服务在线，等待刷卡机连接"
            self.after(0, self.redsys_status_var.set, status)
        except Exception:
            self.after(0, self.redsys_status_var.set, "服务未启动或无法访问（127.0.0.1:6971）")

    def _refresh_redsys_ports(self, combo: ttk.Combobox) -> None:
        ports = self._list_serial_ports()
        current = self.redsys_serial_var.get().strip()
        if current and current not in ports:
            ports.append(current)
        combo["values"] = ports

    def _load_redsys_config_values(self) -> None:
        if not REDSYS_CONFIG.exists():
            return
        try:
            text = REDSYS_CONFIG.read_text(encoding="utf-8")
            patterns = {
                "redsys_port_var": r"(?m)^\s*port:\s*['\"]?([^'\"\s]+)",
                "redsys_merchant_var": r"(?m)^\s*comercio:\s*['\"]?([^'\"\s]+)",
                "redsys_terminal_var": r"(?m)^\s*terminal:\s*['\"]?([^'\"\s]+)",
                "redsys_key_var": r"(?m)^\s*clave_firma:\s*['\"]?([^'\"\s]+)",
                "redsys_serial_var": r"(?m)^\s*puerto:\s*['\"]?([^'\"\s]+)",
                "redsys_version_var": r"(?m)^\s*version:\s*['\"]?([^'\"\s]+)",
            }
            for variable_name, pattern in patterns.items():
                match = re.search(pattern, text)
                if match:
                    getattr(self, variable_name).set(match.group(1))
            simulate = re.search(r"(?m)^\s*simulate:\s*(true|false)", text, re.IGNORECASE)
            if simulate:
                self.redsys_simulate_var.set(simulate.group(1).lower() == "true")
        except OSError as exc:
            self.redsys_status_var.set(f"读取配置失败: {exc}")

    def _save_redsys_config(self) -> None:
        if not REDSYS_CONFIG.exists():
            self.redsys_status_var.set("Redsys 配置文件不存在")
            return
        try:
            self.config_store.update_local_config(redsys_enabled=bool(self.redsys_enabled_var.get()))
            text = REDSYS_CONFIG.read_text(encoding="utf-8")
            replacements = {
                r"(?m)^(\s*port:)\s*.*$": f"\\1 '{self.redsys_port_var.get().strip()}'",
                r"(?m)^(\s*comercio:)\s*.*$": f"\\1 '{self.redsys_merchant_var.get().strip()}'",
                r"(?m)^(\s*terminal:)\s*.*$": f"\\1 '{self.redsys_terminal_var.get().strip()}'",
                r"(?m)^(\s*clave_firma:)\s*.*$": f"\\1 '{self.redsys_key_var.get().strip()}'",
                r"(?m)^(\s*puerto:)\s*.*$": f"\\1 '{self.redsys_serial_var.get().strip()}'",
                r"(?m)^(\s*version:)\s*.*$": f"\\1 '{self.redsys_version_var.get().strip()}'",
                r"(?m)^(\s*simulate:)\s*.*$": f"\\1 {'true' if self.redsys_simulate_var.get() else 'false'}",
            }
            for pattern, replacement in replacements.items():
                text, count = re.subn(pattern, replacement, text, count=1)
                if count == 0:
                    raise ValueError(f"配置项未找到: {pattern}")
            REDSYS_CONFIG.write_text(text, encoding="utf-8")
            self.redsys_status_var.set("Redsys 配置已保存，重启服务后生效")
        except Exception as exc:
            self.redsys_status_var.set(f"保存失败: {exc}")

    def _start_redsys_from_gui(self) -> None:
        self.redsys_enabled_var.set(True)
        self._save_redsys_config()
        threading.Thread(target=self._run_redsys_service, daemon=True).start()

    def _connect_redsys_from_gui(self) -> None:
        """启动服务后，显式调用 Redsys 桥接 DLL 连接刷卡机。"""
        self._save_redsys_config()
        self.redsys_status_var.set("正在连接刷卡机，请稍候...")
        threading.Thread(target=self._check_redsys_status, daemon=True).start()

    def _stop_redsys_from_gui(self) -> None:
        self.redsys_enabled_var.set(False)
        self.config_store.update_local_config(redsys_enabled=False)
        self._stop_redsys_process()
        self.redsys_status_var.set("Redsys 服务已停止")

    def _refresh_ports(self) -> None:
        """扫描可用串口"""
        ports = self._list_serial_ports()
        self.port_combo["values"] = ports
        if ports:
            self.lbl_port_status.config(text=f"找到 {len(ports)} 个串口", foreground="green")
        else:
            self.lbl_port_status.config(text="未找到串口", foreground="red")

    @staticmethod
    def _list_serial_ports() -> list[str]:
        """列出系统可用串口（pyserial 的 comports() 已足够可靠，无需逐个探测）"""
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
            if ports:
                return sorted(set(ports), key=str.upper)
        except ImportError:
            pass
        # 某些 USB 虚拟串口（例如 VfiUniUSBPort）不会被 pyserial 枚举，
        # 但 Windows 注册表仍会登记实际 COM 号。
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
                ports = []
                for index in range(winreg.QueryInfoKey(key)[1]):
                    _, value, _ = winreg.EnumValue(key, index)
                    ports.append(str(value))
                return sorted(set(ports), key=str.upper)
        except (OSError, ImportError):
            return []

    def _apply_preset(self) -> None:
        brand_label = self.scale_brand_var.get()
        preset = SCALE_PRESETS.get(brand_label)
        if not preset:
            return
        self.scale_baudrate_var.set(preset.get("baudrate", 9600))
        self.scale_timeout_var.set(str(preset.get("timeout", 1.2)))
        self.scale_inter_command_delay_var.set(str(preset.get("inter_command_delay", 0.05)))
        self._refresh_ports()
        self._append_log(f"已应用 {brand_label} 预设 (brand={preset.get('brand')})")

    def _on_save_scale(self) -> None:
        """保存电子秤配置到 config_store（直接写入 local_config，runtime 立即生效）"""
        try:
            brand_label = self.scale_brand_var.get()
            preset = SCALE_PRESETS.get(brand_label, {})
            brand_value = preset.get("brand", "zfoc")
            self.config_store.update_local_config(
                scale_port=self.scale_port_var.get().strip(),
                scale_baudrate=int(self.scale_baudrate_var.get()),
                scale_timeout=float(self.scale_timeout_var.get() or "1.2"),
                scale_inter_command_delay=float(self.scale_inter_command_delay_var.get() or "0.05"),
                scale_brand=brand_value,
            )
            self._append_log(f"电子秤配置已保存 (port={self.scale_port_var.get()}, brand={brand_value})")
            messagebox.showinfo("成功", "电子秤配置已保存!\n请重启服务使更改生效。")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def _on_test_scale(self) -> None:
        port = self.scale_port_var.get()
        baudrate = self.scale_baudrate_var.get()
        self._append_log(f"测试连接 {port} @ {baudrate}...")

        def do_test() -> None:
            try:
                import serial
                ser = serial.Serial(
                    port=port,
                    baudrate=baudrate,
                    timeout=2,
                )
                raw = ser.readline()
                ser.close()
                text = raw.decode("latin-1", errors="replace").strip()
                self.after(0, self._append_log, f"收到: {text}")
                self.after(0, messagebox.showinfo, "测试结果", f"连接成功!\n读取到数据:\n{text}")
            except Exception as e:
                self.after(0, self._append_log, f"测试失败: {e}")
                self.after(0, messagebox.showerror, "测试失败", str(e))

        threading.Thread(target=do_test, daemon=True).start()

    # ==================================================================
    # 关于 & 更新 Tab
    # ==================================================================

    def _build_about_tab(self) -> None:
        f = self.tab_about

        # 版本信息
        ver_frame = ttk.LabelFrame(f, text="版本信息", padding=15)
        ver_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(ver_frame, text="IoT Box Desktop", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(ver_frame, text=f"版本: {APP_VERSION}", font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 0))
        ttk.Label(ver_frame, text=f"Python: {platform.python_version()}", font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Label(ver_frame, text=f"系统: {platform.platform()}", font=("Segoe UI", 10)).pack(anchor="w")

        # 更新设置
        update_frame = ttk.LabelFrame(f, text="在线更新", padding=10)
        update_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(update_frame, text="更新源 URL:", font=("Segoe UI", 10)).pack(anchor="w")
        self.update_url_var = StringVar(value=DEFAULT_UPDATE_MANIFEST_URL)
        ttk.Entry(update_frame, textvariable=self.update_url_var, width=60).pack(fill="x", pady=(3, 5))
        ttk.Label(update_frame, text="填写自定义更新清单 URL（JSON）；留空则使用下方 GitHub Releases 模式",
                   foreground="gray").pack(anchor="w")

        # GitHub 模式
        gh_frame = ttk.Frame(update_frame)
        gh_frame.pack(fill="x", pady=(5, 0))
        ttk.Label(gh_frame, text="GitHub:", width=10).pack(side="left")
        self.gh_owner_var = StringVar(value="mikokeyu1986-arch")
        ttk.Entry(gh_frame, textvariable=self.gh_owner_var, width=15).pack(side="left")
        ttk.Label(gh_frame, text=" / ").pack(side="left")
        self.gh_repo_var = StringVar(value="iot_box_comercia")
        ttk.Entry(gh_frame, textvariable=self.gh_repo_var, width=15).pack(side="left")
        ttk.Label(gh_frame, text="  owner/repo", foreground="gray").pack(side="left", padx=5)

        self.gh_prerelease_var = BooleanVar(value=False)
        ttk.Checkbutton(update_frame, text="包含预发布版本", variable=self.gh_prerelease_var).pack(anchor="w", pady=(3, 0))
        self.auto_update_var = BooleanVar(value=True)
        ttk.Checkbutton(update_frame, text="自动检测并安装新版本（每10分钟）", variable=self.auto_update_var).pack(anchor="w", pady=(3, 0))

        # 按钮
        btn_frame = ttk.Frame(update_frame)
        btn_frame.pack(fill="x", pady=10)
        self.btn_check_update = ttk.Button(btn_frame, text="🔍  检查更新", command=self._on_check_update)
        self.btn_check_update.pack(side="left", padx=(0, 8))

        self.btn_download = ttk.Button(btn_frame, text="⬇  下载并安装", command=self._on_download_update,
                                        state="disabled")
        self.btn_download.pack(side="left", padx=(0, 8))

        # 进度条
        self.progress_var = IntVar(value=0)
        self.progress_bar = ttk.Progressbar(update_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=(5, 0))

        # 更新日志
        self.lbl_update_status = ttk.Label(update_frame, text="", foreground="blue")
        self.lbl_update_status.pack(anchor="w", pady=(5, 0))

        # 备份管理
        backup_frame = ttk.LabelFrame(f, text="备份管理", padding=10)
        backup_frame.pack(fill="x", padx=10, pady=10)

        btn_row = ttk.Frame(backup_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="📋  查看备份", command=self._on_list_backups).pack(side="left")
        ttk.Button(btn_row, text="🗑  删除最旧备份", command=self._on_delete_oldest_backup).pack(side="left", padx=5)
        self.lbl_backup_list = ttk.Label(backup_frame, text="", font=("Consolas", 9))
        self.lbl_backup_list.pack(anchor="w", pady=(5, 0))

    # ------------------------------------------------------------------

    def _get_update_manager(self) -> UpdateManager:
        """根据当前设置构建 UpdateManager（自定义 URL 优先于 GitHub 模式）"""
        custom_url = self.update_url_var.get().strip()
        gh_owner = self.gh_owner_var.get().strip()
        gh_repo = self.gh_repo_var.get().strip()

        # 自定义 URL 优先
        if custom_url:
            source = UpdateSource(manifest_url=custom_url)
        elif gh_owner and gh_repo:
            source = GitHubUpdateSource(
                owner=gh_owner,
                repo=gh_repo,
                use_prerelease=self.gh_prerelease_var.get(),
            )
        else:
            # 无任何更新源配置，使用默认 URL
            source = UpdateSource(manifest_url=DEFAULT_UPDATE_MANIFEST_URL)

        return UpdateManager(
            current_version=APP_VERSION,
            base_dir=BASE_DIR,
            source=source,
        )

    def _on_check_update(self) -> None:
        self.btn_check_update.config(state="disabled", text="检查中…")
        self.lbl_update_status.config(text="正在连接更新服务器…")
        self._latest_version: Any = None

        def do_check() -> None:
            try:
                mgr = self._get_update_manager()
                result = mgr.check_for_updates()
            except Exception as exc:
                from app.updater import UpdateResult
                result = UpdateResult(False, f"检查更新失败: {exc}", current_version=APP_VERSION)
            self.after(0, self._on_check_done, result)

        threading.Thread(target=do_check, daemon=True).start()

    def _auto_update_check(self) -> None:
        if self.auto_update_var.get() and not getattr(self, "_update_in_progress", False):
            self._auto_update_checking = True
            self._on_check_update()
        self.after(600000, self._auto_update_check)

    def _on_check_done(self, result: Any) -> None:
        self.btn_check_update.config(state="normal", text="🔍  检查更新")
        if not result.success:
            self.lbl_update_status.config(text=f"错误: {result.message}", foreground="red")
            return

        if result.can_update:
            self.lbl_update_status.config(
                text=f"发现新版本: {result.latest_version}\n{result.message}",
                foreground="green",
            )
            self.btn_download.config(state="normal")
            self._latest_version = result.details
            if getattr(self, "_auto_update_checking", False):
                self._auto_update_checking = False
                self._update_in_progress = True
                self._on_download_update()
        else:
            self.lbl_update_status.config(
                text=f"已是最新版本 ({result.current_version})",
                foreground="blue",
            )
            self.btn_download.config(state="disabled")
            self._auto_update_checking = False

    def _on_download_update(self) -> None:
        if not hasattr(self, "_latest_version") or not self._latest_version:
            return

        from app.updater import VersionInfo
        version_info = VersionInfo(self._latest_version)
        version = version_info.version
        self.btn_download.config(state="disabled", text="下载中…")
        self.progress_var.set(0)

        def do_download() -> None:
            mgr = self._get_update_manager()
            try:
                pkg_path = mgr.download_update(
                    version_info,
                    progress_callback=lambda pct: self.after(0, self.progress_var.set, pct),
                )
                self.after(0, self._on_download_done, pkg_path, mgr, version)
            except Exception as e:
                self.after(0, self._on_download_error, str(e))

        threading.Thread(target=do_download, daemon=True).start()

    def _on_download_done(self, pkg_path: Path, mgr: UpdateManager, version: str) -> None:
        """下载完成：在后台线程中停止服务 → 安装 → 通知主线程。"""
        self.lbl_update_status.config(text="下载完成，正在停止服务并安装…", foreground="blue")
        self.progress_var.set(0)

        def do_install() -> None:
            # 安装前停止服务进程（在子线程中操作，避免阻塞 GUI），
            # 否则 Windows 上可能因文件锁导致覆盖失败或运行中的服务加载半更新的模块。
            self._stop_service_process()
            self.after(0, self._update_service_stopped_ui)
            self.after(0, lambda: self._append_log("服务已停止，正在安装更新…"))
            # 等待端口释放
            time.sleep(0.5)
            try:
                result = mgr.install_update(pkg_path, version)
            except Exception as exc:
                from app.updater import UpdateResult
                result = UpdateResult(False, f"安装失败: {exc}")
            self.after(0, self._on_install_done, result)

        threading.Thread(target=do_install, daemon=True).start()

    def _on_install_done(self, result: Any) -> None:
        """安装完成（主线程）：提示用户并选择是否重启整个 GUI。"""
        self.btn_download.config(state="disabled", text="⬇  下载并安装")
        self.progress_var.set(0)
        if result.success:
            self.lbl_update_status.config(text=result.message, foreground="green")
            if getattr(self, "_update_in_progress", False):
                self._update_in_progress = False
                self.after(1000, self._restart_gui)
                return
            if messagebox.askyesno("更新完成", f"{result.message}\n\n需要重启程序使更新生效，是否现在重启？"):
                self._restart_gui()
            else:
                # 用户选择稍后重启：重新启动服务，让现有版本继续可用
                self.after(500, self._on_start)
        else:
            self.lbl_update_status.config(text=result.message, foreground="red")
            # 安装失败：重新启动旧版本服务
            self.after(500, self._on_start)

    @staticmethod
    def _resolve_pythonw(exe: str) -> str:
        """尝试把 python.exe 换成 pythonw.exe（不弹 CMD 黑框），找不到则回退原 exe。"""
        if not exe or not exe.lower().endswith("python.exe"):
            return exe
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        return candidate if os.path.isfile(candidate) else exe

    def _restart_gui(self) -> None:
        """重启整个 GUI 程序（不仅是服务），让更新的代码生效。"""
        self._stop_service_process()
        time.sleep(1.0)
        # 优先用 pythonw.exe 启动新 GUI，避免再弹一个 CMD 窗口
        gui_py = self._resolve_pythonw(sys.executable)
        subprocess.Popen(
            [gui_py, str(BASE_DIR / "gui_app.py")],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        # 退出当前 GUI（销毁根窗口，终止事件循环）
        try:
            master = self.master
            if master is not None:
                master.destroy()
        except Exception:
            os._exit(0)

    def _on_download_error(self, error: str) -> None:
        self.lbl_update_status.config(text=f"下载失败: {error}", foreground="red")
        self.btn_download.config(state="normal", text="⬇  下载并安装")
        self.progress_var.set(0)

    def _on_list_backups(self) -> None:
        mgr = UpdateManager(current_version=APP_VERSION, base_dir=BASE_DIR)
        backups = mgr.get_backups()
        if not backups:
            self.lbl_backup_list.config(text="(无备份)")
            return
        lines = []
        for b in backups[:10]:
            lines.append(f"  {b['created']}  {b['name']}  ({b['size_mb']} MB)")
        self.lbl_backup_list.config(text="备份列表:\n" + "\n".join(lines))

    def _on_delete_oldest_backup(self) -> None:
        mgr = UpdateManager(current_version=APP_VERSION, base_dir=BASE_DIR)
        backups = mgr.get_backups()
        if not backups:
            messagebox.showinfo("备份", "没有备份可删除")
            return
        oldest = backups[-1]
        path = Path(oldest["path"])
        if path.exists():
            path.unlink()
            self.lbl_backup_list.config(text=f"已删除: {oldest['name']}")
            self._on_list_backups()

    # ==================================================================
    # 配置加载/保存
    # ==================================================================

    def _load_config(self) -> None:
        """从 config_store 加载电子秤设置（与 runtime 读取同一份 local_config）"""
        local = self.config_store.get_local_config()
        self.scale_port_var.set(local.get("scale_port", DEFAULT_SCALE_PORT))
        self.scale_baudrate_var.set(local.get("scale_baudrate", DEFAULT_SCALE_BAUDRATE))
        self.scale_timeout_var.set(str(local.get("scale_timeout", 1.2)))
        self.scale_inter_command_delay_var.set(str(local.get("scale_inter_command_delay", 0.05)))
        saved_protocol = str(local.get("service_protocol") or "https").strip().lower()
        self.proto_var.set(saved_protocol if saved_protocol in {"http", "https"} else "https")
        self.auto_start_var.set(bool(local.get("auto_start_service", True)))

        # 根据已保存的 brand 选中对应预设
        saved_brand = str(local.get("scale_brand") or "zfoc").strip().lower()
        matched_label = None
        for label, preset in SCALE_PRESETS.items():
            if preset.get("brand") == saved_brand:
                matched_label = label
                break
        self.scale_brand_var.set(matched_label or list(SCALE_PRESETS.keys())[0])

        self._refresh_ports()


# ============================================================================
# 托盘应用
# ============================================================================


class TrayApplication:
    """系统托盘 + 设置窗口管理器"""

    def __init__(self) -> None:
        self.config_store = ConfigStore(CONFIG_FILE)
        self.settings_window: SettingsWindow | None = None
        self._tray_icon = None
        self.start_minimized = "--minimized" in sys.argv

    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动托盘（尝试 pystray，失败则降级为纯窗口模式）"""
        try:
            import pystray
            self._run_tray(pystray)
        except ImportError:
            _logger.warning("pystray 未安装，使用窗口模式")
            self._run_window_only()

    # ------------------------------------------------------------------

    def _run_tray(self, pystray) -> None:
        """带系统托盘的完整模式（托盘在后台线程，GUI 在主线程）"""
        import threading

        # 创建 tk 根窗口（必须在主线程）
        root = Tk()
        root.withdraw()
        self._root = root

        def on_quit(icon, item):
            icon.stop()
            # 在主线程销毁 tk 窗口
            root.after(0, root.destroy)

        def on_settings(icon, item):
            root.after(0, self._show_settings, root)

        def on_open_web(icon, item):
            webbrowser.open(f"http://127.0.0.1:{HTTP_PORT}")

        # 图标
        icon_image = self._load_icon(pystray)

        menu = pystray.Menu(
            pystray.MenuItem("打开设置", on_settings, default=True),
            pystray.MenuItem("打开网页界面", on_open_web),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )

        self._tray_icon = pystray.Icon(
            "iot_box",
            icon_image,
            APP_NAME,
            menu,
        )

        # 托盘放到后台线程运行
        tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        tray_thread.start()

        # 显示设置窗口
        if self.start_minimized:
            self.settings_window = SettingsWindow(root, self.config_store)
            self.settings_window.withdraw()
        else:
            self._show_settings(master=root)

        # 主线程运行 tkinter 事件循环
        root.mainloop()

    # ------------------------------------------------------------------

    def _run_window_only(self) -> None:
        """降级模式：无托盘，纯窗口"""
        root = Tk()
        root.withdraw()  # 隐藏根窗口
        if self.start_minimized:
            self.settings_window = SettingsWindow(root, self.config_store)
            self.settings_window.withdraw()
        else:
            self._show_settings(master=root)
        root.mainloop()

    # ------------------------------------------------------------------

    def _show_settings(self, master: Tk | None = None) -> None:
        if self.settings_window is None or not self.settings_window.winfo_exists():
            self.settings_window = SettingsWindow(master, self.config_store)
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_force()

    # ------------------------------------------------------------------

    def _load_icon(self, pystray):
        """加载托盘图标"""
        ico_path = BASE_DIR / "web" / "favicon.ico"
        if ico_path.exists():
            from PIL import Image
            img = Image.open(ico_path)
            return img.resize((64, 64), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC)

        # 创建一个简单的内置图标（蓝色方块）
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(67, 97, 238))
        draw.text((20, 18), "IoT", fill=(255, 255, 255))
        return img


# ============================================================================
# 入口
# ============================================================================


def _ensure_windows_startup_entry() -> None:
    """Start the GUI in the notification area when Windows logs in."""
    if platform.system() != "Windows":
        return
    try:
        import winreg

        launcher = BASE_DIR / "start_gui_hidden.vbs"
        command = f'wscript.exe "{launcher}" --minimized'
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "IoTBoxDesktop", 0, winreg.REG_SZ, command)
    except Exception:
        _logger.exception("Unable to register Windows startup entry")


def main() -> None:
    """GUI 模式入口"""
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _ensure_windows_startup_entry()
    app = TrayApplication()
    app.run()


if __name__ == "__main__":
    main()
