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

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import Tk, Frame, Label, Button, Entry, StringVar, IntVar, BooleanVar
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
from typing import Any

# 将项目根目录加入 path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.config_store import ConfigStore
from app.updater import UpdateManager, UpdateSource, GitHubUpdateSource, DEFAULT_UPDATE_MANIFEST_URL

_logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

APP_NAME = "IoT Box Desktop"
APP_VERSION = "2026.07.31"
_CONFIG_HTTP = BASE_DIR / "runtime_config_http.json"
_CONFIG_DEFAULT = BASE_DIR / "runtime_config.json"
CONFIG_FILE = _CONFIG_HTTP if _CONFIG_HTTP.exists() else _CONFIG_DEFAULT

DEFAULT_ODOO_URL = "http://192.168.1.1:8069"
DEFAULT_TOKEN_URL = "http://192.168.1.1:8069"
DEFAULT_SCALE_PORT = "COM3"
DEFAULT_SCALE_BAUDRATE = 9600

# 电子秤品牌预设
SCALE_PRESETS: dict[str, dict[str, Any]] = {
    "Dibal": {
        "brand": "Dibal",
        "protocol": "serial_continuous",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1,
        "encoding": "utf-8",
        "weight_regex": r"[\d]+[.,][\d]{3}",
    },
    "CAS": {
        "brand": "CAS",
        "protocol": "serial_continuous",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1,
        "encoding": "ascii",
        "weight_regex": r"\d+\.\d+",
    },
    "Mettler Toledo": {
        "brand": "Mettler Toledo",
        "protocol": "serial_command",
        "command": "SI\r\n",
        "baudrate": 9600,
        "bytesize": 7,
        "parity": "E",
        "stopbits": 1,
        "timeout": 2,
        "encoding": "ascii",
        "weight_regex": r"\d+\.\d+",
    },
    "Generic (连续输出)": {
        "brand": "Generic",
        "protocol": "serial_continuous",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1,
        "encoding": "ascii",
        "weight_regex": r"\d+\.\d+",
    },
    "Custom": {
        "brand": "Custom",
        "protocol": "serial_continuous",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1,
        "encoding": "ascii",
        "weight_regex": r"\d+\.\d+",
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

        self.protocol("WM_DELETE_WINDOW", self.withdraw)  # 关闭 = 隐藏

        # 图标（如可用）
        self._set_icon()

        # 构建 UI
        self._build_notebook()
        self._load_config()

    # ------------------------------------------------------------------

    def _set_icon(self) -> None:
        try:
            ico = BASE_DIR / "web" / "favicon.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

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

        self.proto_var = StringVar(value="http")
        rb_http = ttk.Radiobutton(proto_frame, text="HTTP  (端口 8069)", variable=self.proto_var, value="http")
        rb_https = ttk.Radiobutton(proto_frame, text="HTTPS (端口 8443)", variable=self.proto_var, value="https")
        rb_http.pack(side="left", padx=(0, 20))
        rb_https.pack(side="left")

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
        self._append_log(f"正在以 {proto.upper()} 模式启动服务…")
        script = HTTP_SCRIPT if proto == "http" else HTTPS_SCRIPT
        threading.Thread(target=self._run_service, args=(script, proto), daemon=True).start()

    def _run_service(self, script: Path, proto: str) -> None:
        try:
            self._service_proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(BASE_DIR),
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

    def _on_service_started(self, proto: str) -> None:
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_restart.config(state="normal")
        self.lbl_status.config(text=f"● 服务状态: 运行中 ({proto.upper()})", foreground="green")
        self.lbl_protocol.config(text=f"协议: {proto.upper()}")
        self._start_time = time.time()
        self._update_uptime()

    def _on_stop(self) -> None:
        if hasattr(self, "_service_proc") and self._service_proc:
            self._service_proc.terminate()
            self._service_proc = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_restart.config(state="disabled")
        self.lbl_status.config(text="● 服务状态: 已停止", foreground="red")
        self.lbl_uptime.config(text="运行时间: --")
        self._append_log("服务已停止")

    def _on_restart(self) -> None:
        self._on_stop()
        self.after(1000, self._on_start)

    def _on_open_web(self) -> None:
        proto = self.proto_var.get()
        port = "8069" if proto == "http" else "8443"
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
        self.log_text.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {text}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ==================================================================
    # 服务器配置 Tab
    # ==================================================================

    def _build_server_tab(self) -> None:
        f = self.tab_server

        frame = ttk.LabelFrame(f, text="Odoo 服务器连接", padding=15)
        frame.pack(fill="x", padx=10, pady=10)

        # Odoo Server URL
        ttk.Label(frame, text="Odoo 服务器 URL", font=("Segoe UI", 10)).pack(anchor="w")
        self.odoourl_var = StringVar()
        ttk.Entry(frame, textvariable=self.odoourl_var, width=60).pack(fill="x", pady=(3, 10))
        ttk.Label(frame, text="例如: http://192.168.1.100:8069", foreground="gray").pack(anchor="w")

        # Token URL
        ttk.Label(frame, text="Token 获取 URL", font=("Segoe UI", 10)).pack(anchor="w", pady=(10, 0))
        self.token_url_var = StringVar()
        ttk.Entry(frame, textvariable=self.token_url_var, width=60).pack(fill="x", pady=(3, 10))
        ttk.Label(frame, text="与 Odoo URL 通常相同", foreground="gray").pack(anchor="w")

        # CGI URL
        ttk.Label(frame, text="CGI 服务器 URL（可选）", font=("Segoe UI", 10)).pack(anchor="w", pady=(10, 0))
        self.cgi_url_var = StringVar()
        ttk.Entry(frame, textvariable=self.cgi_url_var, width=60).pack(fill="x", pady=(3, 10))

        # 保存按钮
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="💾  保存服务器配置", command=self._on_save_server).pack(side="left")
        ttk.Label(btn_frame, text="保存后需重启服务生效", foreground="gray").pack(side="left", padx=10)

    def _on_save_server(self) -> None:
        try:
            self._save_config_to_file()
            self._append_log("服务器配置已保存")
            messagebox.showinfo("成功", "配置已保存!\n请重启服务使更改生效。")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

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
        ttk.Label(row1, text="串口:", width=12).pack(side="left")
        self.scale_port_var = StringVar(value=DEFAULT_SCALE_PORT)
        self.port_combo = ttk.Combobox(row1, textvariable=self.scale_port_var, width=20)
        self.port_combo.pack(side="left")
        ttk.Button(row1, text="刷新串口", command=self._refresh_ports).pack(side="left", padx=5)
        self.lbl_port_status = ttk.Label(row1, text="", foreground="gray")
        self.lbl_port_status.pack(side="left", padx=5)

        row2 = ttk.Frame(port_frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="波特率:", width=12).pack(side="left")
        self.scale_baudrate_var = IntVar(value=DEFAULT_SCALE_BAUDRATE)
        baud_cb = ttk.Combobox(row2, textvariable=self.scale_baudrate_var,
                                values=[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200],
                                width=18)
        baud_cb.pack(side="left")

        row3 = ttk.Frame(port_frame)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="数据位:", width=12).pack(side="left")
        self.scale_bytesize_var = IntVar(value=8)
        ttk.Combobox(row3, textvariable=self.scale_bytesize_var, values=[5, 6, 7, 8], width=18).pack(side="left")

        row4 = ttk.Frame(port_frame)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="校验位:", width=12).pack(side="left")
        self.scale_parity_var = StringVar(value="N")
        ttk.Combobox(row4, textvariable=self.scale_parity_var,
                      values=["N (None)", "E (Even)", "O (Odd)"], width=18).pack(side="left")

        row5 = ttk.Frame(port_frame)
        row5.pack(fill="x", pady=2)
        ttk.Label(row5, text="停止位:", width=12).pack(side="left")
        self.scale_stopbits_var = IntVar(value=1)
        ttk.Combobox(row5, textvariable=self.scale_stopbits_var, values=[1, 2], width=18).pack(side="left")

        row51 = ttk.Frame(port_frame)
        row51.pack(fill="x", pady=2)
        ttk.Label(row51, text="超时(秒):", width=12).pack(side="left")
        self.scale_timeout_var = StringVar(value="1")
        ttk.Entry(row51, textvariable=self.scale_timeout_var, width=20).pack(side="left")

        # 协议设置
        proto_frame = ttk.LabelFrame(f, text="数据协议（高级）", padding=10)
        proto_frame.pack(fill="x", padx=10, pady=10)

        r1 = ttk.Frame(proto_frame)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="协议:", width=12).pack(side="left")
        self.scale_protocol_var = StringVar(value="serial_continuous")
        ttk.Combobox(r1, textvariable=self.scale_protocol_var,
                      values=["serial_continuous", "serial_command", "tcp"], width=18).pack(side="left")

        r2 = ttk.Frame(proto_frame)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="正则表达式:", width=12).pack(side="left")
        self.scale_regex_var = StringVar(value=r"\d+\.\d+")
        ttk.Entry(r2, textvariable=self.scale_regex_var, width=40).pack(side="left")

        r3 = ttk.Frame(proto_frame)
        r3.pack(fill="x", pady=2)
        ttk.Label(r3, text="命令(指令模式):", width=12).pack(side="left")
        self.scale_command_var = StringVar(value="")
        ttk.Entry(r3, textvariable=self.scale_command_var, width=40).pack(side="left")
        ttk.Label(r3, text="如: SI\\r\\n", foreground="gray").pack(side="left", padx=5)

        r4 = ttk.Frame(proto_frame)
        r4.pack(fill="x", pady=2)
        ttk.Label(r4, text="编码:", width=12).pack(side="left")
        self.scale_encoding_var = StringVar(value="ascii")
        ttk.Combobox(r4, textvariable=self.scale_encoding_var,
                      values=["ascii", "utf-8", "latin-1", "gb2312"], width=18).pack(side="left")

        # 按钮
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="🧪  测试连接", command=self._on_test_scale).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="💾  保存电子秤配置", command=self._on_save_scale).pack(side="left")

    # ------------------------------------------------------------------

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
        """列出系统可用串口"""
        try:
            import serial.tools.list_ports
            return [p.device for p in serial.tools.list_ports.comports()]
        except ImportError:
            pass
        # 回退：列出常见 COM 口
        if platform.system() == "Windows":
            candidates = [f"COM{i}" for i in range(1, 33)]
            # 简单探测
            import serial
            available = []
            for port in candidates:
                try:
                    s = serial.Serial(port)
                    s.close()
                    available.append(port)
                except (OSError, serial.SerialException):
                    pass
            return available
        return []

    def _apply_preset(self) -> None:
        brand = self.scale_brand_var.get()
        preset = SCALE_PRESETS.get(brand)
        if not preset:
            return
        self.scale_port_var.set(DEFAULT_SCALE_PORT)
        self.scale_baudrate_var.set(preset.get("baudrate", 9600))
        self.scale_bytesize_var.set(preset.get("bytesize", 8))
        self.scale_parity_var.set(preset.get("parity", "N"))
        self.scale_stopbits_var.set(preset.get("stopbits", 1))
        self.scale_timeout_var.set(str(preset.get("timeout", 1)))
        self.scale_protocol_var.set(preset.get("protocol", "serial_continuous"))
        self.scale_regex_var.set(preset.get("weight_regex", r"\d+\.\d+"))
        self.scale_command_var.set(preset.get("command", ""))
        self.scale_encoding_var.set(preset.get("encoding", "ascii"))
        self._refresh_ports()
        self._append_log(f"已应用 {brand} 预设")

    def _on_save_scale(self) -> None:
        try:
            parity_map = {"N (None)": "N", "E (Even)": "E", "O (Odd)": "O"}
            scale_config = {
                "protocol": self.scale_protocol_var.get(),
                "port": self.scale_port_var.get(),
                "baudrate": self.scale_baudrate_var.get(),
                "bytesize": self.scale_bytesize_var.get(),
                "parity": parity_map.get(self.scale_parity_var.get(), self.scale_parity_var.get()),
                "stopbits": self.scale_stopbits_var.get(),
                "timeout": float(self.scale_timeout_var.get() or "1"),
                "encoding": self.scale_encoding_var.get(),
                "weight_regex": self.scale_regex_var.get(),
                "brand": self.scale_brand_var.get(),
                "command": self.scale_command_var.get(),
            }
            self._save_config_to_file(scale_override={"scale": scale_config})
            self._append_log("电子秤配置已保存")
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
                    bytesize=self.scale_bytesize_var.get(),
                    parity=self.scale_parity_var.get()[:1],
                    stopbits=self.scale_stopbits_var.get(),
                    timeout=2,
                )
                # 尝试读取一行
                raw = ser.readline()
                ser.close()
                text = raw.decode(self.scale_encoding_var.get(), errors="replace").strip()
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

        ttk.Label(update_frame, text="更新源:", font=("Segoe UI", 10)).pack(anchor="w")
        self.update_url_var = StringVar(value=DEFAULT_UPDATE_MANIFEST_URL or "")
        ttk.Entry(update_frame, textvariable=self.update_url_var, width=60).pack(fill="x", pady=(3, 5))
        ttk.Label(update_frame, text="填写你的更新清单 URL（JSON），留空使用 GitHub Releases 模式",
                   foreground="gray").pack(anchor="w")

        # GitHub 模式
        gh_frame = ttk.Frame(update_frame)
        gh_frame.pack(fill="x", pady=(5, 0))
        ttk.Label(gh_frame, text="GitHub:", width=10).pack(side="left")
        self.gh_owner_var = StringVar(value="")
        ttk.Entry(gh_frame, textvariable=self.gh_owner_var, width=15).pack(side="left")
        ttk.Label(gh_frame, text=" / ").pack(side="left")
        self.gh_repo_var = StringVar(value="")
        ttk.Entry(gh_frame, textvariable=self.gh_repo_var, width=15).pack(side="left")
        ttk.Label(gh_frame, text="  owner/repo", foreground="gray").pack(side="left", padx=5)

        self.gh_prerelease_var = BooleanVar(value=False)
        ttk.Checkbutton(update_frame, text="包含预发布版本", variable=self.gh_prerelease_var).pack(anchor="w", pady=(3, 0))

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
        """根据当前设置构建 UpdateManager"""
        custom_url = self.update_url_var.get().strip()
        gh_owner = self.gh_owner_var.get().strip()
        gh_repo = self.gh_repo_var.get().strip()

        if gh_owner and gh_repo:
            source = GitHubUpdateSource(
                owner=gh_owner,
                repo=gh_repo,
                use_prerelease=self.gh_prerelease_var.get(),
            )
        else:
            source = UpdateSource(manifest_url=custom_url)

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
            mgr = self._get_update_manager()
            result = mgr.check_for_updates()
            self.after(0, self._on_check_done, result)

        threading.Thread(target=do_check, daemon=True).start()

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
        else:
            self.lbl_update_status.config(
                text=f"已是最新版本 ({result.current_version})",
                foreground="blue",
            )
            self.btn_download.config(state="disabled")

    def _on_download_update(self) -> None:
        if not hasattr(self, "_latest_version") or not self._latest_version:
            return

        from app.updater import VersionInfo
        version_info = VersionInfo(self._latest_version)
        self.btn_download.config(state="disabled", text="下载中…")
        self.progress_var.set(0)

        def do_download() -> None:
            mgr = self._get_update_manager()
            try:
                pkg_path = mgr.download_update(
                    version_info,
                    progress_callback=lambda pct: self.after(0, self.progress_var.set, pct),
                )
                self.after(0, self._on_download_done, pkg_path, mgr)
            except Exception as e:
                self.after(0, self._on_download_error, str(e))

        threading.Thread(target=do_download, daemon=True).start()

    def _on_download_done(self, pkg_path: Path, mgr: UpdateManager) -> None:
        self.lbl_update_status.config(text="下载完成，正在安装…", foreground="blue")
        result = mgr.install_update(pkg_path, mgr.source.parse_latest_release(
            mgr.source.fetch_manifest()
        ).version if mgr.source.parse_latest_release(mgr.source.fetch_manifest()) else "unknown")

        if result.success:
            self.lbl_update_status.config(
                text=result.message + "\n请手动重启程序应用更新",
                foreground="green",
            )
            if messagebox.askyesno("更新完成", f"{result.message}\n\n是否现在重启?"):
                self._on_restart()
        else:
            self.lbl_update_status.config(text=result.message, foreground="red")

        self.btn_download.config(state="disabled", text="⬇  下载并安装")
        self.progress_var.set(0)

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
        """从 runtime_config_http.json 加载配置"""
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            else:
                cfg = {}
        except Exception:
            cfg = {}

        odoo = cfg.get("odoo", {})
        self.odoourl_var.set(odoo.get("url", DEFAULT_ODOO_URL))
        self.token_url_var.set(cfg.get("token_server_url", "") or DEFAULT_TOKEN_URL)
        self.cgi_url_var.set(cfg.get("cgi_server_url", ""))

        scale = cfg.get("scale", {})
        self.scale_port_var.set(scale.get("port", DEFAULT_SCALE_PORT))
        self.scale_baudrate_var.set(scale.get("baudrate", DEFAULT_SCALE_BAUDRATE))
        self.scale_bytesize_var.set(scale.get("bytesize", 8))
        self.scale_parity_var.set(scale.get("parity", "N"))
        self.scale_stopbits_var.set(scale.get("stopbits", 1))
        self.scale_timeout_var.set(str(scale.get("timeout", 1)))
        self.scale_protocol_var.set(scale.get("protocol", "serial_continuous"))
        self.scale_regex_var.set(scale.get("weight_regex", r"\d+\.\d+"))
        self.scale_command_var.set(scale.get("command", ""))
        self.scale_encoding_var.set(scale.get("encoding", "ascii"))
        self.scale_brand_var.set(scale.get("brand", "Generic (连续输出)"))

        self._refresh_ports()

    def _save_config_to_file(self, scale_override: dict[str, Any] | None = None) -> None:
        """将当前设置写回 runtime_config_http.json"""
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            else:
                cfg = {}

            # 服务器配置
            odoo = cfg.get("odoo", {})
            if isinstance(odoo, dict):
                odoo["url"] = self.odoourl_var.get()
            else:
                odoo = {"url": self.odoourl_var.get()}
            cfg["odoo"] = odoo

            if self.token_url_var.get():
                cfg["token_server_url"] = self.token_url_var.get()
            if self.cgi_url_var.get():
                cfg["cgi_server_url"] = self.cgi_url_var.get()

            # 电子秤配置
            if scale_override:
                cfg["scale"] = scale_override.get("scale", scale_override)
            else:
                parity_map = {"N (None)": "N", "E (Even)": "E", "O (Odd)": "O"}
                cfg["scale"] = {
                    "protocol": self.scale_protocol_var.get(),
                    "port": self.scale_port_var.get(),
                    "baudrate": self.scale_baudrate_var.get(),
                    "bytesize": self.scale_bytesize_var.get(),
                    "parity": parity_map.get(self.scale_parity_var.get(), self.scale_parity_var.get()),
                    "stopbits": self.scale_stopbits_var.get(),
                    "timeout": float(self.scale_timeout_var.get() or "1"),
                    "encoding": self.scale_encoding_var.get(),
                    "weight_regex": self.scale_regex_var.get(),
                    "brand": self.scale_brand_var.get(),
                    "command": self.scale_command_var.get(),
                }

            CONFIG_FILE.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            _logger.exception("保存配置失败")
            raise


# ============================================================================
# 托盘应用
# ============================================================================


class TrayApplication:
    """系统托盘 + 设置窗口管理器"""

    def __init__(self) -> None:
        self.config_store = ConfigStore(CONFIG_FILE)
        self.settings_window: SettingsWindow | None = None
        self._tray_icon = None

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
        """带系统托盘的完整模式"""

        def on_quit(icon, item):
            icon.stop()
            sys.exit(0)

        def on_settings(icon, item):
            self._show_settings()

        def on_open_web(icon, item):
            webbrowser.open("http://127.0.0.1:8069")

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

        # 启动时显示窗口
        self.after_idle(self._show_settings)

        self._tray_icon.run()

    # ------------------------------------------------------------------

    def _run_window_only(self) -> None:
        """降级模式：无托盘，纯窗口"""
        root = Tk()
        root.withdraw()  # 隐藏根窗口
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

    # ------------------------------------------------------------------

    @staticmethod
    def after_idle(callback) -> None:
        """延迟执行（等 GUI 初始化后）"""
        try:
            root = Tk()
            root.withdraw()
            root.after(500, lambda: [callback(), root.destroy()])
        except Exception:
            callback()


# ============================================================================
# 入口
# ============================================================================


def main() -> None:
    """GUI 模式入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = TrayApplication()
    app.run()


if __name__ == "__main__":
    main()
