import os
import tkinter as tk
from tkinter import ttk, messagebox
from threading import Thread
from queue import Queue, Empty
import platform
from datetime import datetime
from pathlib import Path
import sys
from phone_agent import PhoneAgent
from phone_agent.model import ModelConfig
from phone_agent.agent import AgentConfig


# ==========================
# Windows: 用你那套 DPI awareness（system aware）
# ==========================
def enable_windows_dpi_awareness_like_yours():
    if platform.system() != "Windows":
        return
    try:
        from ctypes import windll
        try:
            windll.shcore.SetProcessDpiAwareness(1)  # 1 = System DPI aware
        except Exception:
            pass
    except Exception:
        pass


# ==========================
# .env 读写（不依赖 python-dotenv）
# ==========================
def _get_base_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd()

def get_app_dir() -> Path:
    """
    返回应用的“稳定目录”
    - 源码运行：当前 .py 文件所在目录
    - PyInstaller onefile/onedir：exe 所在目录
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    else:
        # 源码运行
        return Path(__file__).resolve().parent

#ENV_PATH = _get_base_dir() / ".env"
APP_DIR = get_app_dir()
ENV_PATH = APP_DIR / ".env"

ENV_KEYS = {
    "api_key": "PHONEAGENT_API_KEY",
    "base_url": "PHONEAGENT_BASE_URL",
    "model_name": "PHONEAGENT_MODEL_NAME",
}

DEFAULT_SETTINGS = {
    "base_url": "https://api-inference.modelscope.cn/v1/",
    "api_key": "",
    "model_name": "Qwen/Qwen3-VL-30B-A3B-Thinking",
}


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1]
    return v


def read_env_file(path: Path) -> dict:
    if not path.exists():
        return {}
    data = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except Exception:
            return {}

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = _strip_quotes(v.strip())
        data[k] = v
    return data


def _quote_env_value(v: str) -> str:
    # 统一写成双引号，避免特殊字符/空格/# 导致解析问题
    v = v.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{v}"'


def write_env_file_update(path: Path, updates: dict):
    """
    更新/写入指定 key，尽量保留原文件其它内容与注释（简单实现）。
    """
    existing_lines = []
    if path.exists():
        try:
            existing_lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            existing_lines = []

    # 构建 key->行号 映射（只匹配最简单的 KEY=...）
    key_to_idx = {}
    for i, raw in enumerate(existing_lines):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k = line.split("=", 1)[0].strip()
        if k:
            key_to_idx[k] = i

    # 应用更新
    for k, v in updates.items():
        new_line = f"{k}={_quote_env_value(v)}"
        if k in key_to_idx:
            existing_lines[key_to_idx[k]] = new_line
        else:
            existing_lines.append(new_line)

    # 若是新文件，给点注释头
    if not path.exists() and existing_lines:
        header = [
            "# AutoGLM / PhoneAgent settings",
            "# Values are quoted automatically.",
            "",
        ]
        existing_lines = header + existing_lines

    path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")


def load_settings_from_env() -> dict:
    env = read_env_file(ENV_PATH)
    s = dict(DEFAULT_SETTINGS)
    s["api_key"] = env.get(ENV_KEYS["api_key"], s["api_key"])
    s["base_url"] = env.get(ENV_KEYS["base_url"], s["base_url"])
    s["model_name"] = env.get(ENV_KEYS["model_name"], s["model_name"])
    return s


def save_settings_to_env(settings: dict):
    updates = {
        ENV_KEYS["api_key"]: settings.get("api_key", ""),
        ENV_KEYS["base_url"]: settings.get("base_url", ""),
        ENV_KEYS["model_name"]: settings.get("model_name", ""),
    }
    write_env_file_update(ENV_PATH, updates)

def resource_path(rel_path: str) -> str:
    """
    兼容源码运行 & PyInstaller onefile
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 解压目录
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return str(base / rel_path)
class ChatGUI:
    def __init__(self):
        self._closing = False
        self._after_id = None
        self._busy = False

        # 读取 .env
        self.settings = load_settings_from_env()

        # agent config（你原来的）
        self.agent_config = AgentConfig(
            max_steps=100,
            verbose=True,
            lang="cn",
        )

        # 构建 agent（可热更新）
        self._build_agent()

        self.window = tk.Tk()
        self.window.title("AutoGLM")
        icon_path = resource_path("logo.ico")
        self.window.iconbitmap(icon_path)
        self.window.geometry("1600x900")
        self.window.minsize(1100, 650)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

        # ==========================
        # 缩放：按“倍数”直接设置（100%=1.0, 200%=2.0）
        # ==========================
        self.zoom_var = tk.DoubleVar(value=2.0)  # 默认 200%
        self._apply_zoom()

        # ==========================
        # ttk 样式
        # ==========================
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            style.theme_use(style.theme_names()[0])

        style.configure(".", font=("微软雅黑", 12))
        style.configure("Header.TLabel", font=("微软雅黑", 13, "bold"))
        style.configure("Status.TLabel", font=("微软雅黑", 10))
        style.configure("TButton", padding=(10, 6))

        # 根布局
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)

        # 线程安全队列
        self.uiq = Queue()

        # 聊天/日志数据
        self.chat_items = []
        self.debug_lines = []
        self.debug_filter_var = tk.StringVar(value="")
        self.debug_ignore_case = tk.BooleanVar(value=True)
        self.debug_fragments = []  # 存储 (content: str, end: str)

        # 颜色
        self.chat_bg = "#FFFFFF"
        self.meta_fg = "#6B7280"
        self.user_bubble_bg = "#E8F0FE"
        self.ai_bubble_bg = "#F3F4F6"
        self.bubble_border = "#E5E7EB"

        # UI
        self._build_toolbar()
        self._build_panes()
        self._build_input()
        self._build_statusbar()

        # 队列轮询
        self._after_id = self.window.after(30, self._drain_ui_queue)

        # 初始状态
        self._post_debug("任务执行过程：Agent执行任务的过程")
        self._post_chat("ai", "已就绪。Enter 发送；Shift+Enter 换行；Ctrl+Enter 发送。")
        self._post_status(left="就绪", right=self._settings_brief())

        # 初次居中
        self.window.after_idle(self._center_sash)

    # --------------------------
    # agent 构建/重载
    # --------------------------
    def _build_agent(self):
        self.model_config = ModelConfig(
            base_url=self.settings["base_url"],
            api_key=self.settings["api_key"],
            model_name=self.settings["model_name"],
        )
        self.agent = PhoneAgent(model_config=self.model_config, agent_config=self.agent_config)

    def _reload_agent(self):
        # 忙的时候不允许热切（避免线程里用到旧对象造成混乱）
        if self._busy:
            messagebox.showwarning("提示", "当前正在运行任务，请结束后再修改并应用模型设置。")
            return
        self._build_agent()
        self._post_status(right=self._settings_brief())
        self._post_debug(f"已应用设置：base_url={self.settings['base_url']} | model={self.settings['model_name']}")

    def _settings_brief(self) -> str:
        k = self.settings.get("api_key", "")
        k_brief = (k[:6] + "…" + k[-4:]) if len(k) >= 12 else ("(空)" if not k else "(已设置)")
        return f"{self.settings.get('model_name','')} | {k_brief}"

    # --------------------------
    # 缩放控制
    # --------------------------
    def _apply_zoom(self):
        try:
            z = float(self.zoom_var.get())
            z = max(0.75, min(3.0, z))
            self.window.tk.call("tk", "scaling", z)
        except Exception:
            pass

    def _set_zoom_from_percent(self, s: str):
        try:
            pct = float(s.strip().replace("%", ""))
            self.zoom_var.set(pct / 100.0)
            self._apply_zoom()
            self.window.update_idletasks()
            self._refresh_bubbles()
            self._center_sash()
        except Exception:
            pass

    # --------------------------
    # UI 构建
    # --------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self.window, padding=(10, 8))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        ttk.Label(bar, text="AutoGLM", style="Header.TLabel").grid(row=0, column=0, sticky="w")

        zoom_box = ttk.Combobox(
            bar,
            width=8,
            state="readonly",
            values=["100%", "125%", "150%", "175%", "200%", "225%", "250%"],
        )
        zoom_box.grid(row=0, column=1, sticky="e", padx=(10, 10))
        zoom_box.set("200%")
        zoom_box.bind("<<ComboboxSelected>>", lambda e: self._set_zoom_from_percent(zoom_box.get()))

        btns = ttk.Frame(bar)
        btns.grid(row=0, column=2, sticky="e")

        ttk.Button(btns, text="清空聊天", command=self.clear_chat).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="清空日志", command=self.clear_debug).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="置顶窗口", command=self.toggle_topmost).pack(side="left", padx=(0, 6))

        self.settings_btn = ttk.Button(btns, text="模型设置", command=self.open_settings_dialog)
        self.settings_btn.pack(side="left")

    def _build_panes(self):
        container = ttk.Frame(self.window, padding=(10, 6))
        container.grid(row=1, column=0, sticky="nsew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.paned = ttk.Panedwindow(container, orient="horizontal")
        self.paned.grid(row=0, column=0, sticky="nsew")
        self.paned.bind("<Configure>", lambda e: self._center_sash())

        # 左：聊天
        left = ttk.Frame(self.paned, padding=(8, 8))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="聊天内容", style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))

        chat_wrap = ttk.Frame(left)
        chat_wrap.grid(row=1, column=0, sticky="nsew")
        chat_wrap.rowconfigure(0, weight=1)
        chat_wrap.columnconfigure(0, weight=1)

        self.chat_text = tk.Text(
            chat_wrap,
            wrap="word",
            font=("微软雅黑", 12),
            bg=self.chat_bg,
            fg="#111111",
            padx=10,
            pady=10,
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#E5E7EB",
        )
        self.chat_text.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ttk.Scrollbar(chat_wrap, orient="vertical", command=self.chat_text.yview)
        chat_scroll.grid(row=0, column=1, sticky="ns")
        self.chat_text.configure(yscrollcommand=chat_scroll.set)

        self.chat_text.config(state=tk.DISABLED)
        self.chat_text.bind("<Configure>", lambda e: self._refresh_bubbles())

        # 右：日志
        right = ttk.Frame(self.paned, padding=(8, 8))
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(right, text="Agent 执行过程", style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))

        filter_bar = ttk.Frame(right)
        filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        filter_bar.columnconfigure(1, weight=1)

        ttk.Label(filter_bar, text="搜索/过滤：", style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.debug_filter_entry = ttk.Entry(filter_bar, textvariable=self.debug_filter_var)
        self.debug_filter_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self.debug_filter_entry.bind("<Return>", lambda e: self.apply_debug_filter())

        ttk.Checkbutton(
            filter_bar,
            text="忽略大小写",
            variable=self.debug_ignore_case,
            command=self.apply_debug_filter,
        ).grid(row=0, column=2, sticky="e", padx=(0, 6))

        ttk.Button(filter_bar, text="应用", command=self.apply_debug_filter).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(filter_bar, text="清除", command=self.clear_debug_filter).grid(row=0, column=4)

        debug_wrap = ttk.Frame(right)
        debug_wrap.grid(row=2, column=0, sticky="nsew")
        debug_wrap.rowconfigure(0, weight=1)
        debug_wrap.columnconfigure(0, weight=1)

        self.debug_text = tk.Text(
            debug_wrap,
            wrap="none",
            font=("Consolas", 11),
            bg="#0B1220",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
            padx=10,
            pady=10,
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#111827",
        )
        self.debug_text.grid(row=0, column=0, sticky="nsew")

        debug_scroll = ttk.Scrollbar(debug_wrap, orient="vertical", command=self.debug_text.yview)
        debug_scroll.grid(row=0, column=1, sticky="ns")
        self.debug_text.configure(yscrollcommand=debug_scroll.set)

        self.debug_text.tag_configure("match", background="#F59E0B", foreground="#111827")
        self.debug_text.config(state=tk.DISABLED)

        self.paned.add(left, weight=1)
        self.paned.add(right, weight=1)

    def _build_input(self):
        box = ttk.Frame(self.window, padding=(10, 8))
        box.grid(row=2, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)

        input_wrap = ttk.Frame(box)
        input_wrap.grid(row=0, column=0, sticky="ew")
        input_wrap.columnconfigure(0, weight=1)

        self.input_text = tk.Text(
            input_wrap,
            height=3,
            wrap="word",
            font=("微软雅黑", 12),
            padx=10,
            pady=8,
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#E5E7EB",
        )
        self.input_text.grid(row=0, column=0, sticky="ew")

        self.send_btn = ttk.Button(box, text="发送", command=self.send_message)
        self.send_btn.grid(row=0, column=1, sticky="e", padx=(10, 0))

        self.input_text.bind("<Return>", self._on_enter_send)
        self.input_text.bind("<KP_Enter>", self._on_enter_send)
        self.input_text.bind("<Shift-Return>", self._on_shift_enter_newline)
        self.input_text.bind("<Shift-KP_Enter>", self._on_shift_enter_newline)
        self.input_text.bind("<Control-Return>", self._on_ctrl_enter_send)
        self.input_text.bind("<Control-KP_Enter>", self._on_ctrl_enter_send)

        hint = ttk.Label(
            self.window,
            text="快捷键：Enter 发送｜Shift+Enter 换行｜Ctrl+Enter 发送（日志区支持搜索/过滤）",
            style="Status.TLabel",
        )
        hint.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 4))

    def _build_statusbar(self):
        bar = ttk.Frame(self.window, padding=(10, 6))
        bar.grid(row=4, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="就绪")
        self.status_right_var = tk.StringVar(value="")

        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(bar, textvariable=self.status_right_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")

    # --------------------------
    # 模型设置弹窗（写入 .env + 立即应用）
    # --------------------------
    def open_settings_dialog(self):
        if self._busy:
            messagebox.showwarning("提示", "当前正在运行任务，请结束后再打开设置。")
            return

        win = tk.Toplevel(self.window)
        win.title("模型设置（保存到 .env）")
        win.transient(self.window)
        win.grab_set()
        win.geometry("720x320")
        win.minsize(650, 300)

        wrap = ttk.Frame(win, padding=14)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(1, weight=1)

        base_url_var = tk.StringVar(value=self.settings.get("base_url", DEFAULT_SETTINGS["base_url"]))
        model_var = tk.StringVar(value=self.settings.get("model_name", DEFAULT_SETTINGS["model_name"]))
        api_key_var = tk.StringVar(value=self.settings.get("api_key", ""))

        ttk.Label(wrap, text="Base URL：").grid(row=0, column=0, sticky="w", pady=(0, 10))
        base_entry = ttk.Entry(wrap, textvariable=base_url_var)
        base_entry.grid(row=0, column=1, sticky="ew", pady=(0, 10))

        ttk.Label(wrap, text="Model ID：").grid(row=1, column=0, sticky="w", pady=(0, 10))
        model_entry = ttk.Entry(wrap, textvariable=model_var)
        model_entry.grid(row=1, column=1, sticky="ew", pady=(0, 10))

        ttk.Label(wrap, text="API Key：").grid(row=2, column=0, sticky="w", pady=(0, 10))
        api_entry = ttk.Entry(wrap, textvariable=api_key_var, show="•")
        api_entry.grid(row=2, column=1, sticky="ew", pady=(0, 10))

        show_var = tk.BooleanVar(value=False)

        def toggle_show():
            api_entry.configure(show="" if show_var.get() else "•")

        ttk.Checkbutton(wrap, text="显示 API Key", variable=show_var, command=toggle_show).grid(
            row=3, column=1, sticky="w", pady=(0, 12)
        )

        tip = ttk.Label(
            wrap,
            text=f".env 路径：{ENV_PATH}",
            style="Status.TLabel",
        )
        tip.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 14))

        btns = ttk.Frame(wrap)
        btns.grid(row=5, column=0, columnspan=2, sticky="e")

        def save_and_apply():
            base_url = base_url_var.get().strip()
            model_name = model_var.get().strip()
            api_key = api_key_var.get().strip()

            if not base_url:
                messagebox.showerror("错误", "Base URL 不能为空。")
                return
            if not model_name:
                messagebox.showerror("错误", "Model ID 不能为空。")
                return

            self.settings["base_url"] = base_url
            self.settings["model_name"] = model_name
            self.settings["api_key"] = api_key

            try:
                save_settings_to_env(self.settings)
            except Exception as e:
                messagebox.showerror("错误", f"写入 .env 失败：{e}")
                return

            self._reload_agent()
            self._post_chat("ai", "✅ 已保存并应用模型设置。")
            win.destroy()

        ttk.Button(btns, text="保存并应用", command=save_and_apply).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left")

        base_entry.focus_set()

    # --------------------------
    # 聊天气泡
    # --------------------------
    def _append_chat_ui(self, role, text):
        ts = datetime.now().strftime("%H:%M:%S")
        who = "🧑‍💻 你" if role == "user" else "🤖 AI"
        bubble_bg = self.user_bubble_bg if role == "user" else self.ai_bubble_bg

        self.chat_text.config(state=tk.NORMAL)
        if self.chat_text.index("end-1c") != "1.0":
            self.chat_text.insert(tk.END, "\n")

        avail = max(420, self.chat_text.winfo_width() - 60)
        bubble_max = int(avail * 0.72)

        container = tk.Frame(self.chat_text, bg=self.chat_bg, width=avail)
        self.chat_text.window_create(tk.END, window=container, align="top", pady=2)

        meta = tk.Label(container, text=f"[{ts}] {who}", bg=self.chat_bg, fg=self.meta_fg, font=("微软雅黑", 10))
        meta.pack(anchor="w")

        row = tk.Frame(container, bg=self.chat_bg, width=avail)
        row.pack(fill="x")

        bubble = tk.Label(
            row,
            text=text,
            bg=bubble_bg,
            fg="#111111",
            font=("微软雅黑", 12),
            justify="left",
            wraplength=bubble_max,
            padx=12,
            pady=8,
            bd=0,
            highlightthickness=1,
            highlightbackground=self.bubble_border,
        )

        # 不论是谁，都左对齐
        bubble.pack(side="left", padx=(0, 90), pady=(4, 0), anchor="w")

        self.chat_text.insert(tk.END, "\n")

        self.chat_items.append({"container": container, "row": row, "bubble": bubble})
        self.chat_text.config(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    def _refresh_bubbles(self):
        avail = max(420, self.chat_text.winfo_width() - 60)
        bubble_max = int(avail * 0.72)
        for item in self.chat_items:
            try:
                item["container"].config(width=avail)
                item["row"].config(width=avail)
                item["bubble"].config(wraplength=bubble_max)
            except Exception:
                pass

    def clear_chat(self):
        for item in self.chat_items:
            try:
                item["container"].destroy()
            except Exception:
                pass
        self.chat_items.clear()

        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.config(state=tk.DISABLED)

    # --------------------------
    # 日志
    # --------------------------
    def _append_debug_ui(self, data):
        # 处理字符串（兼容原有逻辑）
        if isinstance(data, str):
            content = data
            end = "\n"
        # 处理元组 (content, end)
        elif isinstance(data, (list, tuple)) and len(data) >= 2:
            content, end = data[0], data[1]
        # 其他类型强制转字符串
        else:
            content = str(data)
            end = "\n"
        
        # 根据 end 判断是否换行
        if end == "\n":
            # 换行：拼接当前内容，作为完整一行
            if self.debug_fragments:
                last_content = "".join([f[0] for f in self.debug_fragments])
                self.debug_lines.append(last_content + content)
                self.debug_fragments = []
            else:
                self.debug_lines.append(content)
        else:
            # 不换行：追加到片段列表
            self.debug_fragments.append((content, end))
        
        self._render_debug()

    def _render_debug(self):
        flt = self.debug_filter_var.get().strip()
        ignore_case = self.debug_ignore_case.get()

        if flt and ignore_case:
            f = flt.lower()
            shown = [ln for ln in self.debug_lines if f in ln.lower()]
        elif flt:
            shown = [ln for ln in self.debug_lines if flt in ln]
        else:
            shown = self.debug_lines.copy()

        # 处理未换行的最后一段内容
        if self.debug_fragments:
            last_fragment = "".join([f[0] for f in self.debug_fragments])
            if flt == "" or (ignore_case and f in last_fragment.lower()) or (not ignore_case and flt in last_fragment):
                shown.append(last_fragment)

        self.debug_text.config(state=tk.NORMAL)
        self.debug_text.delete("1.0", tk.END)
        for ln in shown:
            # 确保 ln 是字符串后再拼接
            ln_str = str(ln)
            self.debug_text.insert(tk.END, ln_str + "\n")
        self.debug_text.config(state=tk.DISABLED)
        self.debug_text.see(tk.END)

        self._highlight_debug_matches(flt, ignore_case)
        self._post_status(right=f"日志：{len(shown)}/{len(self.debug_lines)} 行 | {self._settings_brief()}")
        
    def _highlight_debug_matches(self, needle: str, ignore_case: bool):
        self.debug_text.config(state=tk.NORMAL)
        self.debug_text.tag_remove("match", "1.0", tk.END)

        needle = needle.strip()
        if needle:
            start = "1.0"
            while True:
                idx = self.debug_text.search(needle, start, stopindex=tk.END, nocase=1 if ignore_case else 0)
                if not idx:
                    break
                end = f"{idx}+{len(needle)}c"
                self.debug_text.tag_add("match", idx, end)
                start = end

        self.debug_text.config(state=tk.DISABLED)

    def apply_debug_filter(self):
        self._render_debug()

    def clear_debug_filter(self):
        self.debug_filter_var.set("")
        self._render_debug()

    def clear_debug(self):
        self.debug_lines.clear()
        self.debug_text.config(state=tk.NORMAL)
        self.debug_text.delete("1.0", tk.END)
        self.debug_text.config(state=tk.DISABLED)
        self._post_status(right=f"日志：0 行 | {self._settings_brief()}")

    # --------------------------
    # 队列 UI 更新
    # --------------------------
    def _post_chat(self, role, text):
        if not self._closing:
            self.uiq.put(("chat", role, text))

    def _post_debug(self, data):
        if not self._closing:
            self.uiq.put(("debug", data))

    def _post_status(self, left=None, right=None):
        if not self._closing:
            self.uiq.put(("status", left, right))

    def _set_busy(self, is_busy: bool):
        if not self._closing:
            self.uiq.put(("busy", is_busy))

    def _drain_ui_queue(self):
        if self._closing:
            return
        try:
            while True:
                kind, *rest = self.uiq.get_nowait()
                if kind == "chat":
                    role, text = rest
                    self._append_chat_ui(role, text)
                elif kind == "debug":
                    (text,) = rest
                    self._append_debug_ui(text)
                elif kind == "status":
                    left, right = rest
                    if left is not None:
                        self.status_var.set(left)
                    if right is not None:
                        self.status_right_var.set(right)
                elif kind == "busy":
                    (is_busy,) = rest
                    self._busy = is_busy
                    state = tk.DISABLED if is_busy else tk.NORMAL
                    self.send_btn.config(state=state)
                    self.input_text.config(state=state)
                    if hasattr(self, "settings_btn"):
                        self.settings_btn.config(state=state)
        except Empty:
            pass
        finally:
            self._after_id = self.window.after(30, self._drain_ui_queue)

    # --------------------------
    # 分割条等宽
    # --------------------------
    def _center_sash(self):
        try:
            w = self.paned.winfo_width()
            if w > 50:
                self.paned.sashpos(0, w // 2)
        except Exception:
            pass

    # --------------------------
    # 置顶
    # --------------------------
    def toggle_topmost(self):
        try:
            cur = bool(self.window.attributes("-topmost"))
            self.window.attributes("-topmost", not cur)
            self._post_status(right=f"置顶：{'开' if not cur else '关'} | {self._settings_brief()}")
        except Exception:
            pass

    # --------------------------
    # 输入快捷键
    # --------------------------
    def _on_enter_send(self, event):
        self.send_message()
        return "break"

    def _on_ctrl_enter_send(self, event):
        self.send_message()
        return "break"

    def _on_shift_enter_newline(self, event):
        self.input_text.insert(tk.INSERT, "\n")
        return "break"

    def _get_input(self) -> str:
        return self.input_text.get("1.0", "end-1c").strip()

    def _clear_input(self):
        self.input_text.delete("1.0", tk.END)

    # --------------------------
    # 发送 / 运行 Agent
    # --------------------------
    def send_message(self):
        if self._busy or self._closing:
            return

        user_msg = self._get_input()
        if not user_msg:
            return

        self._clear_input()
        self._post_chat("user", user_msg)

        self._set_busy(True)
        self._post_status(left="运行中…", right=self._settings_brief())

        Thread(target=self.run_agent, args=(user_msg,), daemon=True).start()

    def run_agent(self, user_msg):
        import builtins
        original_print = builtins.print

        def gui_print(*args, sep=' ', end='\n', **kwargs):
            msg = sep.join(str(a) for a in args)
            self._post_debug((msg, end))  # 传递内容+结束符

        builtins.print = gui_print

        try:
            result = self.agent.run(user_msg)
            self._post_chat("ai", str(result))
            self._post_status(left="就绪", right=f"steps≤{self.agent_config.max_steps} | {self._settings_brief()}")
        except Exception as e:
            self._post_chat("ai", f"❌ 错误：{e}")
            self._post_status(left="出错", right=self._settings_brief())
        finally:
            builtins.print = original_print
            self._set_busy(False)

    # --------------------------
    # 关闭窗口：避免 after/队列导致 TclError
    # --------------------------
    def _on_close(self):
        self._closing = True
        try:
            if self._after_id is not None:
                self.window.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass

    def run(self):
        self.input_text.focus_set()
        self.window.mainloop()


if __name__ == "__main__":
    enable_windows_dpi_awareness_like_yours()
    ChatGUI().run()
