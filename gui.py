"""桌面界面（Tkinter，标准库自带）：运行 / 账号池 / 会话 / 导出 四个页签。"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import export
import keymeta
import keystore
import session as sess
import sites
from paths import ACCOUNTS_PATH, APP_DIR, EXPORTS_DIR
from runner import SIGNUP_URL, Settings, open_account, run_batch, run_checkin
from store import (
    Account,
    ResultStore,
    detect_format,
    dump_accounts,
    load_accounts,
    merge_accounts,
    read_rows,
    save_accounts,
)

BASE = APP_DIR
PAD = {"padx": 6, "pady": 4}
CHECKIN_HEADER = "# 时间\t账号\t状态\t保留列\t备注"
ACCOUNT_FILETYPES = [("账号库 json", "*.json"), ("CSV", "*.csv"), ("文本", "*.txt"),
                     ("全部", "*.*")]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("InviteTool · 邀请站点自动注册 / API Key / 每日签到")
        self.geometry("1060x740")
        self.minsize(920, 640)

        self.q: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.accounts: list[Account] = []
        self.rows: dict[str, str] = {}
        self.site: sites.Site = sites.first_enabled()

        self.var_site = tk.StringVar(value=sites.first_enabled().name)
        # 这三个不再显示在界面上（路径固定按站点算），只当内部状态用：
        # 账号库固定 data/accounts.json，日志固定 logs/<站点>/results.log · checkin.log
        self.var_accounts = tk.StringVar(value=str(ACCOUNTS_PATH))
        self.var_out = tk.StringVar(value=str(sites.first_enabled().results_path(BASE)))
        self.var_checkin_out = tk.StringVar(value=str(sites.first_enabled().checkin_path(BASE)))
        self.var_url = tk.StringVar(value=SIGNUP_URL)
        self.var_background = tk.BooleanVar(value=True)
        self.var_skip = tk.BooleanVar(value=True)
        self.var_session = tk.BooleanVar(value=True)
        self.var_reuse_key = tk.BooleanVar(value=True)
        self.var_only_selected = tk.BooleanVar(value=False)
        # 跑哪些站点：当前站点 / 所有启用站点 / 自选（self.pick_keys 记住选了哪几个）
        self.var_scope = tk.StringVar(value=self.SCOPE_ONE)
        self.pick_keys: list[str] = []
        self.var_key_group = tk.StringVar(value="")
        self.var_key_note = tk.StringVar(value="")
        self.var_filter_group = tk.StringVar(value="")
        self.var_retries = tk.IntVar(value=2)
        self.var_delay = tk.DoubleVar(value=5.0)
        self.var_limit = tk.IntVar(value=0)
        # 同时跑几个账号：签到实测 3~6 都稳（每个 CloakBrowser 约 500MB 内存）；
        # 注册是真开 Chrome + 走 GitHub，并发高了容易被 GitHub 风控盯上，所以默认 1
        self.var_concurrency = tk.IntVar(value=1)
        self.var_proxy = tk.StringVar(value="")
        self.var_channel = tk.StringVar(value="auto")
        self.var_progress = tk.StringVar(value="就绪")
        self.var_fmt = tk.StringVar(value="table")
        self.var_export_scope = tk.StringVar(value="全部")
        self.var_ov_summary = tk.StringVar(value="")
        self.var_site_name = tk.StringVar(value="")
        self.var_site_key = tk.StringVar(value="")
        self.var_site_url = tk.StringVar(value="")
        self.var_site_method = tk.StringVar(value="auto")
        self.site_list: list = list(sites.SITES)
        self.ov_sites: list = []
        self._ov_keys: dict[str, dict[str, str]] = {}
        self.pool_vars = [tk.StringVar() for _ in range(5)]
        self.var_pool_enabled = tk.BooleanVar(value=True)

        keystore.sync_all(sites.SITES, BASE)  # 把日志里的历史 key 收进 data/keys/
        self._build()
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, **PAD)
        tabs = {}
        for key, text in (("run", " 运行 "), ("overview", " 总览 "), ("pool", " 账号池 "),
                          ("sites", " 站点 "), ("sess", " 会话 "), ("out", " 导出 ")):
            tabs[key] = ttk.Frame(nb)
            nb.add(tabs[key], text=text)
        self._build_run(tabs["run"])
        self._build_overview(tabs["overview"])
        self._build_pool(tabs["pool"])
        self._build_sites(tabs["sites"])
        self._build_sessions(tabs["sess"])
        self._build_export(tabs["out"])
        ttk.Label(self, textvariable=self.var_progress, anchor="w").pack(
            fill="x", padx=8, pady=(0, 6))
        self._ready = True
        self.load()

    # ---------------- 表格通用：点表头排序 + 悬浮看全文 ----------------

    SCOPE_ONE = "当前站点"
    SCOPE_ALL = "所有启用站点"
    SCOPE_PICK = "自选站点…"
    RUN_COLS = ("分组", "账号", "状态", "API Key", "剩余额度", "备注", "运行结果")
    EXPORT_COLS = ("序号", "站点", "API Key", "账号", "剩余额度", "分组", "备注/时间")

    @staticmethod
    def _sort_key(text) -> tuple:
        """排序用的键：数字/金额（`$12.34`）按数值比，其它按文本比，空的一律排最后。"""
        s = str(text or "").strip()
        if not s:
            return (2, 0.0, "")
        try:
            return (0, float(s.lstrip("$+").replace(",", "")), "")
        except ValueError:
            return (1, 0.0, s)

    def _sortable(self, tree: ttk.Treeview, cols) -> None:
        """给每个列头挂排序：点一下升序，再点一下降序（表头上带箭头）。空值永远排最后。"""
        desc: dict[str, bool] = {}

        def sort_by(col: str) -> None:
            rev = desc.get(col, False)        # 第一次点是升序，之后来回切
            desc[col] = not rev
            idx = list(cols).index(col)

            def cell(iid: str) -> str:
                return str((list(tree.item(iid, "values")) + [""] * len(cols))[idx] or "")

            items = list(tree.get_children(""))
            filled = sorted((i for i in items if cell(i).strip()),
                            key=lambda i: self._sort_key(cell(i)), reverse=rev)
            empty = [i for i in items if not cell(i).strip()]
            for pos, iid in enumerate(filled + empty):
                tree.move(iid, "", pos)
            for c in cols:                     # 只在当前排序列上显示箭头
                tree.heading(c, text=f"{c}{'↓' if rev else '↑'}" if c == col else c)

        for c in cols:
            tree.heading(c, text=c, command=lambda cc=c: sort_by(cc))

    def _tip_bind(self, tree: ttk.Treeview, cols) -> None:
        """鼠标停在格子上就弹一个小黄条显示整格内容（列窄看不全时用）。"""
        tip: dict = {"win": None, "cell": None}

        def hide(_evt=None) -> None:
            if tip["win"] is not None:
                tip["win"].destroy()
            tip["win"], tip["cell"] = None, None

        def show(evt) -> None:
            iid, col = tree.identify_row(evt.y), tree.identify_column(evt.x)
            if not iid or not col.startswith("#"):
                hide()
                return
            n = int(col[1:]) - 1
            if not 0 <= n < len(cols):
                hide()
                return
            if tip["cell"] == (iid, n):
                return
            hide()
            vals = list(tree.item(iid, "values")) + [""] * len(cols)
            text = str(vals[n] or "")
            if len(text) < 6:                  # 短内容一眼就看全了，不打扰
                return
            tip["cell"] = (iid, n)
            win = tk.Toplevel(tree)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            tk.Label(win, text=f"{cols[n]}：{text}", justify="left", background="#ffffe0",
                     relief="solid", borderwidth=1, wraplength=560,
                     padx=4, pady=2).pack()
            win.geometry(f"+{evt.x_root + 14}+{evt.y_root + 18}")
            tip["win"] = win

        tree.bind("<Motion>", show, add="+")
        tree.bind("<Leave>", hide, add="+")
        tree.bind("<Button-1>", hide, add="+")
        tree.bind("<MouseWheel>", hide, add="+")

    def _build_run(self, root: ttk.Frame) -> None:
        site_bar = ttk.Frame(root)
        site_bar.pack(fill="x", **PAD)
        ttk.Label(site_bar, text="站点").pack(side="left")
        self.cb_site = ttk.Combobox(site_bar, textvariable=self.var_site, width=14,
                                    state="readonly",
                                    values=sites.enabled_names())
        self.cb_site.pack(side="left", padx=(2, 10))
        ttk.Label(site_bar,
                  text="切站点 = 换它的邀请链接 / 注册·签到日志 / 登录态 / Key 标记（账号库共用）；"
                       "这里只列启用的站点，增删改和启停在「站点」页"
                  ).pack(side="left")
        ttk.Button(site_bar, text="签到日志目录", width=12,
                   command=lambda: self._open_dir(self.var_checkin_out)).pack(side="right")
        ttk.Button(site_bar, text="注册日志目录", width=12,
                   command=lambda: self._open_dir(self.var_out)).pack(side="right", padx=6)
        ttk.Button(site_bar, text="重新加载账号库", width=14,
                   command=self.load).pack(side="right", padx=6)
        self.var_site.trace_add("write", lambda *_: self._on_site_change())

        top = ttk.Frame(root)
        top.pack(fill="x", **PAD)
        ttk.Label(top, text="邀请链接").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.var_url).grid(row=0, column=1, sticky="ew")
        top.columnconfigure(1, weight=1)

        opt = ttk.Frame(root)
        opt.pack(fill="x", **PAD)
        ttk.Checkbutton(opt, text="后台运行", variable=self.var_background).pack(side="left")
        ttk.Checkbutton(opt, text="跳过已完成", variable=self.var_skip).pack(side="left", padx=6)
        ttk.Checkbutton(opt, text="复用登录态", variable=self.var_session).pack(side="left")
        ttk.Checkbutton(opt, text="复用已有 Key", variable=self.var_reuse_key).pack(side="left",
                                                                                padx=6)
        ttk.Label(opt, text="重试").pack(side="left", padx=(8, 0))
        ttk.Spinbox(opt, from_=1, to=5, width=3, textvariable=self.var_retries).pack(side="left")
        ttk.Label(opt, text="间隔").pack(side="left", padx=(6, 0))
        ttk.Spinbox(opt, from_=0, to=120, width=4, textvariable=self.var_delay).pack(side="left")
        ttk.Label(opt, text="只跑前").pack(side="left", padx=(6, 0))
        ttk.Spinbox(opt, from_=0, to=999, width=4, textvariable=self.var_limit).pack(side="left")
        ttk.Label(opt, text="个(0=全部)").pack(side="left")
        ttk.Label(opt, text="并发").pack(side="left", padx=(8, 0))
        ttk.Spinbox(opt, from_=1, to=8, width=3,
                    textvariable=self.var_concurrency).pack(side="left")
        ttk.Label(opt, text="个账号(1=顺序)").pack(side="left")
        ttk.Label(opt, text="浏览器").pack(side="left", padx=(8, 0))
        ttk.Combobox(opt, textvariable=self.var_channel, width=8, state="readonly",
                     values=("auto", "chrome", "msedge", "chromium")).pack(side="left")
        ttk.Label(opt, text="代理").pack(side="left", padx=(8, 0))
        ttk.Entry(opt, textvariable=self.var_proxy, width=16).pack(side="left")
        self._build_run_rest(root)

    def _build_run_rest(self, root: ttk.Frame) -> None:
        btns = ttk.Frame(root)
        btns.pack(fill="x", **PAD)
        self.btn_start = ttk.Button(btns, text="开始：注册 / 取 Key", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_checkin = ttk.Button(btns, text="自动签到", command=self.start_checkin)
        self.btn_checkin.pack(side="left", padx=6)
        self.btn_assist = ttk.Button(btns, text="协助签到", command=self.start_checkin_assist)
        self.btn_assist.pack(side="left")
        ttk.Checkbutton(btns, text="只跑选中", variable=self.var_only_selected).pack(side="left")
        ttk.Label(btns, text="站点范围").pack(side="left", padx=(10, 2))
        self.cb_scope = ttk.Combobox(btns, textvariable=self.var_scope, width=13,
                                     state="readonly",
                                     values=(self.SCOPE_ONE, self.SCOPE_ALL, self.SCOPE_PICK))
        self.cb_scope.pack(side="left")
        self.cb_scope.bind("<<ComboboxSelected>>", self._on_scope_change)
        self.btn_pause = ttk.Button(btns, text="暂停", command=self.toggle_pause,
                                    state="disabled")
        self.btn_pause.pack(side="left", padx=(10, 0))
        self.btn_stop = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="登录选中账号", command=self.open_selected_account).pack(side="left",
                                                                                 padx=(14, 0))
        ttk.Button(btns, text="复制 Key", command=self._copy_key).pack(side="left", padx=6)

        cols = self.RUN_COLS
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=10,
                                 selectmode="extended")
        for c, w in zip(cols, (70, 120, 55, 300, 70, 160, 200)):
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, **PAD)
        self.tree.bind("<Double-1>", self._copy_key)
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<<TreeviewSelect>>", self._fill_meta_form)
        self._sortable(self.tree, cols)
        self._tip_bind(self.tree, cols)

        mark = ttk.Frame(root)
        mark.pack(fill="x", **PAD)
        ttk.Label(mark, text="分组").pack(side="left")
        ttk.Combobox(mark, textvariable=self.var_key_group, width=10, state="readonly",
                     values=("",) + keymeta.GROUPS).pack(side="left", padx=(2, 8))
        ttk.Label(mark, text="备注").pack(side="left")
        ttk.Entry(mark, textvariable=self.var_key_note).pack(side="left", fill="x", expand=True,
                                                            padx=2)
        ttk.Button(mark, text="应用到选中", command=self.apply_meta).pack(side="left", padx=6)
        ttk.Label(mark, text="（分组留空=清除标记）").pack(side="left")

        ttk.Label(root, text="表格支持多选：Ctrl 点选加减、Shift 点选连续、Ctrl+A 全选；"
                            "右键可以跑选中账号、快速改分组。点表头排序，鼠标停在格子上看全文。"
                  ).pack(anchor="w", padx=8)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="复制 API Key", command=self._copy_key)
        self.menu.add_separator()
        self.menu.add_command(label="跑选中：注册 / 取 Key",
                              command=lambda: self._start(checkin=False, only_selected=True))
        self.menu.add_command(label="跑选中：自动签到",
                              command=lambda: self._start(checkin=True, only_selected=True))
        self.menu.add_separator()
        group_menu = tk.Menu(self.menu, tearoff=0)
        for name in keymeta.GROUPS:
            group_menu.add_command(label=name,
                                   command=lambda g=name: self.set_group(g))
        group_menu.add_separator()
        group_menu.add_command(label="清除分组", command=lambda: self.set_group(""))
        self.menu.add_cascade(label="设为分组", menu=group_menu)
        self.menu.add_separator()
        self.menu.add_command(label="登录这个账号", command=self.open_selected_account)

        self.log_box = tk.Text(root, height=10, wrap="none")
        self.log_box.pack(fill="both", expand=True, **PAD)

    def _popup_menu(self, event) -> None:
        """右键菜单。点在没选中的行上时先把那一行选上，已在选区里就保留多选。"""
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection():
            self.menu.tk_popup(event.x_root, event.y_root)

    # ---------------- 运行页动作 ----------------

    def _on_scope_change(self, _evt=None) -> None:
        """选到「自选站点…」就弹勾选框；选完把当前选择显示在下拉框上。"""
        if self.var_scope.get() != self.SCOPE_PICK:
            return
        picked = self._pick_sites_dialog()
        if not picked:                      # 取消 / 一个都没勾：退回当前站点
            self.var_scope.set(self.SCOPE_ONE)
            return
        self.pick_keys = picked
        names = "、".join(s.name for s in self._sites_by_keys(picked))
        self.var_scope.set(f"自选 {len(picked)} 个")
        self.log(f"站点范围：自选 {names}（按这个顺序依次跑）")

    def _sites_by_keys(self, keys) -> list:
        """按 key 取站点对象，顺序跟着 keys；登记表里没了的自动忽略。"""
        out = []
        for key in keys:
            try:
                out.append(sites.by_key(key))
            except KeyError:
                continue
        return out

    def _pick_sites_dialog(self) -> list[str]:
        """勾选要跑哪几个站点（按登记表顺序依次跑）。返回选中的 key 列表，取消返回空。"""
        win = tk.Toplevel(self)
        win.title("自选站点")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text="勾上要跑的站点，会按登记表里的顺序依次跑：").pack(
            anchor="w", padx=10, pady=(10, 4))
        chosen = set(self.pick_keys) or {s.key for s in sites.enabled()}
        vars_: dict[str, tk.BooleanVar] = {}
        for s in sites.SITES:
            v = tk.BooleanVar(value=s.key in chosen)
            vars_[s.key] = v
            text = f"{s.name}（{s.key}）" + ("" if s.enabled else "  [已停用]")
            ttk.Checkbutton(win, text=text, variable=v).pack(anchor="w", padx=16)
        result: list[str] = []

        def ok() -> None:
            result.extend(s.key for s in sites.SITES if vars_[s.key].get())
            win.destroy()

        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=10)
        ttk.Button(row, text="确定", command=ok).pack(side="right")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right", padx=6)
        win.grab_set()
        self.wait_window(win)
        return result

    def _run_targets(self) -> list:
        """这次要跑哪些站点（顺序执行）。登记表是空的就返回空列表，调用方负责提示。"""
        if not sites.configured():
            return []
        scope = self.var_scope.get()
        if scope == self.SCOPE_ALL:
            return sites.enabled()
        if scope.startswith("自选"):
            return self._sites_by_keys(self.pick_keys) or [self.site]
        return [self.site]

    def _open_dir(self, var: tk.StringVar) -> None:
        """打开日志所在的文件夹（文件已经有了就顺便在资源管理器里选中它）。"""
        path = Path(var.get())
        folder = path.parent
        if path.exists():
            # explorer 的 /select 语法：逗号后面直接跟路径，不能有空格；成功也会返回 1，别检查返回码
            subprocess.run(["explorer", f"/select,{path}"], check=False)
            return
        if folder.exists():
            os.startfile(str(folder))  # noqa: S606 - Windows 上用默认程序打开
            self.var_progress.set(f"{path.name} 还没生成，先打开了目录 {folder}")
            return
        messagebox.showinfo("提示", f"{folder} 还没生成（这个站点还没跑过）")

    def _copy_key(self, _evt=None) -> None:
        sel = self.tree.selection()
        if not sel:
            self.var_progress.set("先在表格里选一行")
            return
        vals = list(self.tree.item(sel[0], "values")) + [""] * 7
        user, key = vals[1], vals[3]
        if not key:
            self.log(f"{user} 还没有 API Key，先跑一次「开始：注册 / 取 Key」")
            self.var_progress.set(f"{user} 还没有 API Key")
            return
        self.clipboard_clear()
        self.clipboard_append(key)
        self.update()  # 让剪贴板内容真正生效
        self.log(f"已复制 {user} 的 API Key: {key}")
        self.var_progress.set(f"已复制 {user} 的 API Key")

    def log(self, msg: str) -> None:
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")

    def _meta_path(self) -> str:
        """当前站点的 key 标记文件（keys-meta*.json）。"""
        return str(self.site.keymeta_path(BASE))

    def _sess_dir(self) -> Path:
        """当前站点的登录态目录。"""
        return self.site.sessions_dir(BASE)

    def _on_site_change(self) -> None:
        """切站点：换掉邀请链接和各数据文件路径，然后重新加载列表 / 会话 / 导出预览。"""
        if not getattr(self, "_ready", False):  # 界面还没搭完，别去动还不存在的控件
            return
        try:
            self.site = sites.by_key(self.var_site.get())
        except KeyError:
            self.site = sites.first_enabled()
        self.var_url.set(self.site.signup_url)
        self.var_out.set(str(self.site.results_path(BASE)))
        self.var_checkin_out.set(str(self.site.checkin_path(BASE)))
        self.log(f"切到站点 {self.site.name}（{self.site.origin}）")
        self.load()
        self.refresh_sessions()
        self.export_preview()

    def load(self) -> None:
        """加载账号库，填上本站点已拿到的 key、注册状态，以及人工标记的分组/备注。"""
        try:
            self.accounts = load_accounts(self.var_accounts.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("账号库读取失败", str(exc))
            return
        known = {
            user: (key, when, quota)
            for user, key, when, _g, _n, _s, quota in export.collect_scoped(
                [self.site], BASE, self.var_accounts.get())
        }
        meta = keymeta.load(self._meta_path())
        _keys, registered, failed, signed = self._ov_state(self.site)
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for a in self.accounts:
            key, when, quota = known.get(a.username, ("", "", ""))
            m = meta.get(a.username, {})
            if not a.enabled:
                state = "停用"
            elif key:
                state = "已有"
            elif a.username in registered:
                state = "已注册"          # 有登录态但没取到 key
            elif a.username in failed:
                state = "注册失败"
            else:
                state = "未注册"
            result = a.note or (f"上次 {when}" if when else "")
            if a.username in signed:
                result = (result + " 今天已签到").strip()
            self.rows[a.username] = self.tree.insert(
                "", "end",
                values=(m.get("group", ""), a.username, state, key, quota,
                        m.get("note", ""), result),
            )
        on = sum(1 for a in self.accounts if a.enabled)
        self.log(f"[{self.site.name}] 已加载 {len(self.accounts)} 个账号"
                 f"（启用 {on}，已有 key {len(known)}，注册过 {len(registered)}）")
        self.var_progress.set(
            f"[{self.site.name}] {len(self.accounts)} 个账号：{len(known)} 个已有 Key，"
            f"{len(registered)} 个注册过；未注册的不会签到（用「登录选中账号」可以补注册）"
        )

    # ---------------- 分组 / 备注 ----------------

    def _fill_meta_form(self, _evt=None) -> None:
        """选中单行时把它的分组/备注回填到下面的输入框。"""
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        vals = list(self.tree.item(sel[0], "values")) + [""] * 7
        self.var_key_group.set(vals[0])
        self.var_key_note.set(vals[5])

    def _write_meta(self, users: list[str], group: str | None, note: str | None) -> None:
        keymeta.update(users, group=group, note=note, path=self._meta_path())
        for user in users:
            iid = self.rows.get(user)
            if not iid:
                continue
            vals = list(self.tree.item(iid, "values")) + [""] * 7
            if group is not None:
                vals[0] = group
            if note is not None:
                vals[5] = note
            self.tree.item(iid, values=tuple(vals[:7]))
        self.export_preview()

    def apply_meta(self) -> None:
        """把输入框里的分组 + 备注写到所有选中行。"""
        users = self._selected_users()
        if not users:
            messagebox.showinfo("提示", "先在表格里选中要标记的账号")
            return
        group, note = self.var_key_group.get().strip(), self.var_key_note.get().strip()
        self._write_meta(users, group, note)
        self.var_progress.set(f"已标记 {len(users)} 个账号：分组={group or '(空)'} 备注={note or '(空)'}")

    def set_group(self, group: str) -> None:
        """右键菜单里的快速改分组，不动备注。"""
        users = self._selected_users()
        if not users:
            return
        self._write_meta(users, group, None)
        self.var_key_group.set(group)
        self.var_progress.set(f"已把 {len(users)} 个账号设为 {group or '(无分组)'}")

    def _log_paths(self, site, multi: bool) -> tuple[str, str]:
        """(注册日志, 签到日志)。只跑当前站点时用输入框里的路径（你可能手动改过）。"""
        if not multi and site.key == self.site.key:
            return self.var_out.get(), self.var_checkin_out.get()
        return str(site.results_path(BASE)), str(site.checkin_path(BASE))

    def _settings(self, for_checkin: bool = False, site=None) -> Settings:
        """跑任务用的参数。``site`` 不给就是运行页当前选的站点。"""
        site = site or self.site
        # 当前站点用输入框里的链接（你可能手动改过），其它站点用登记表里的
        url = self.var_url.get().strip() if site.key == self.site.key else site.signup_url
        return Settings(
            signup_url=url,
            retries=max(1, int(self.var_retries.get())),
            delay_between=float(self.var_delay.get()),
            skip_done=self.var_skip.get(),
            shot_dir=site.shot_dir(BASE),
            proxy=self.var_proxy.get().strip(),
            channel=self.var_channel.get(),
            use_session=self.var_session.get(),
            reuse_key=self.var_reuse_key.get(),
            background=self.var_background.get(),
            session_dir=site.sessions_dir(BASE),
            keys_path=site.keys_path(BASE),
            tokens_path=site.tokens_path(BASE),
            site_key=site.key,
            checkin_method=sites.first_method(site),
            concurrency=max(1, int(self.var_concurrency.get() or 1)),
            stop_flag=self.stop_evt.is_set,
            pause_flag=self.pause_evt.is_set,
        )

    def _selected_users(self) -> list[str]:
        """运行页表格里选中的账号名（支持 Ctrl / Shift 多选）。账号在第 2 列。"""
        return [self.tree.item(iid, "values")[1] for iid in self.tree.selection()]

    def _scope_accounts(self, only_selected: bool) -> tuple[list, str]:
        """算出这次要跑哪些账号，返回 (账号列表, 说明文字)。"""
        if only_selected:
            wanted = set(self._selected_users())
            picked = [a for a in self.accounts if a.username in wanted]
            off = [a.username for a in picked if not a.enabled]
            if off:
                self.log(f"注意：{', '.join(off)} 在账号池里是停用状态，因为手动选中了所以照样跑")
            return picked, f"选中 {len(picked)} 个"
        picked = [a for a in self.accounts if a.enabled]
        limit = max(0, int(self.var_limit.get()))
        if limit:
            picked = picked[:limit]
            return picked, f"前 {len(picked)} 个"
        return picked, f"全部 {len(picked)} 个"

    def start(self) -> None:
        self._start(checkin=False)

    def start_checkin(self) -> None:
        self._start(checkin=True)

    def start_checkin_assist(self) -> None:
        """协助签到：开一个能看见的窗口，先自动试，实在不成才轮到你点。"""
        if not messagebox.askokcancel(
                "协助签到",
                "接下来会用 **CloakBrowser**（反检测 Chromium）逐个打开已登录好的窗口：\n\n"
                "工具先自己走一遍（个人资料 → 立即签到 → 有人机验证就勾一下）；\n"
                "自动没成的才轮到你——你在窗口里点「每日签到」，签上工具会自动关窗口继续下一个。\n\n"
                "跟以前不同的是：这个窗口里的人机验证**是能点过的**，所以多数账号你不用动手。\n"
                "今天已签到的、没在这个站点注册成功的会自动跳过。"):
            return
        self._start(checkin=True, assist=True)

    def _start(self, checkin: bool, only_selected: bool | None = None,
               assist: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已经有任务在跑了")
            return
        if not sites.configured():
            messagebox.showinfo("先加一个站点", "站点登记表是空的：到「站点」页填显示名 + "
                                                "邀请链接 →「添加为新站点」→「保存」，再回来跑。")
            return
        if not self.accounts:
            self.load()
        if not self.accounts:
            messagebox.showinfo("账号库是空的", "到「账号池」页导入或手工加几个账号"
                                                "（账号 / 密码 / TOTP 密钥），再回来跑。")
            return
        use_sel = self.var_only_selected.get() if only_selected is None else only_selected
        accounts, scope = self._scope_accounts(use_sel)
        if not accounts:
            messagebox.showinfo("提示", "先在表格里选中账号" if use_sel else "账号库里没有启用的账号")
            return

        self.stop_evt.clear()
        self.pause_evt.clear()
        for b in (self.btn_start, self.btn_checkin, self.btn_assist):
            b.config(state="disabled")
        self.btn_pause.config(state="normal", text="暂停")
        self.btn_stop.config(state="normal")

        targets = self._run_targets()
        if not targets:
            messagebox.showinfo("没有可跑的站点", "站点范围选的是「所有启用站点」但一个都没启用，"
                                                  "或者登记表是空的——到「站点」页看看。")
            for b in (self.btn_start, self.btn_checkin, self.btn_assist):
                b.config(state="normal")
            self.btn_pause.config(state="disabled")
            self.btn_stop.config(state="disabled")
            return
        multi = len(targets) > 1
        if checkin:
            fn, what = run_checkin, ("协助签到" if assist else "自动签到")
        else:
            fn, what = run_batch, "注册取 Key"
        where = "、".join(s.name for s in targets)
        self.var_progress.set(f"{what}：准备在 {where} 跑{scope}账号…")
        self.log(f"=== {what} @ {where}：{scope}"
                 f"（{', '.join(a.username for a in accounts[:6])}"
                 f"{' …' if len(accounts) > 6 else ''}）===")

        def work() -> None:
            try:
                for site in targets:
                    if self.stop_evt.is_set():
                        break
                    if multi:
                        self.q.put(("log", f"—— 站点 {site.name}（{site.origin}）——"))
                    st = self._settings(for_checkin=checkin, site=site)
                    reg_log, chk_log = self._log_paths(site, multi)
                    if checkin:
                        store = ResultStore(chk_log, CHECKIN_HEADER)
                        # 没在这个站点注册成功的账号不签到
                        extra = {"register_results": reg_log, "assist": assist}
                    else:
                        store = ResultStore(reg_log)
                        extra = {}
                    label = f"{site.name} · {what}" if multi else what
                    fn(accounts, st, store,
                       log=lambda m: self.q.put(("log", m)),
                       # 多站点一起跑时不动表格（一个账号会有多条结果），跑完刷「总览」
                       on_result=None if multi else
                       (lambda u, s, k, n: self.q.put(("row", u, s, k, n))),
                       on_progress=lambda i, t, u: self.q.put(("prog", label, i, t, u)),
                       **extra)
            except Exception as exc:  # noqa: BLE001 - 兜住线程内异常并显示
                self.q.put(("log", f"运行异常: {exc}"))
            finally:
                self.q.put(("end",))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def toggle_pause(self) -> None:
        if self.pause_evt.is_set():
            self.pause_evt.clear()
            self.btn_pause.config(text="暂停")
            self.log("[继续] 已请求继续")
        else:
            self.pause_evt.set()
            self.btn_pause.config(text="继续")
            self.log("[暂停] 已请求暂停，当前账号跑完就停住")

    def stop(self) -> None:
        self.stop_evt.set()
        self.pause_evt.clear()
        self.log("[停止] 已请求停止：正在跑的账号会在最近的一个检查点中断（一般 1 秒内），"
                 "浏览器随即关掉。已完成的账号下次会自动跳过")
        self.var_progress.set("正在停止…")
        self.btn_stop.config(state="disabled")
        self.btn_pause.config(state="disabled")

    # ---------------- 总览页（账号 × 站点） ----------------

    OV_LEGEND = ("图例：✓ 有 Key    ○ 注册过但没取到 Key    × 上次失败    空 没跑过；"
                 "后面带「签」= 今天已签到")

    def _build_overview(self, root: ttk.Frame) -> None:
        bar = ttk.Frame(root)
        bar.pack(fill="x", **PAD)
        ttk.Button(bar, text="刷新", command=self.overview_refresh).pack(side="left")
        ttk.Label(bar, text="一屏看完每个账号在各站点的进度；双击站点那一格 = 复制该站点的 API Key"
                  ).pack(side="left", padx=8)
        ttk.Label(root, textvariable=self.var_ov_summary, anchor="w").pack(fill="x", padx=8)
        self.ov_tree = ttk.Treeview(root, show="headings", height=20, selectmode="browse")
        self.ov_tree.pack(fill="both", expand=True, **PAD)
        self.ov_tree.bind("<Double-1>", self._ov_copy_cell)
        ttk.Label(root, text=self.OV_LEGEND).pack(anchor="w", padx=8, pady=(0, 6))
        self.overview_refresh()

    def _ov_state(self, site) -> tuple[dict, set, set, set]:
        """某站点的进度：{账号: key}、注册过的、上次失败的、今天签到过的。

        key 以 `data/keys/<站点>.json` 为准（正式数据），日志只用来找"上次失败"。
        """
        keys = {u: v["key"] for u, v in keystore.load(site.keys_path(BASE)).items()}
        fails: set[str] = set()
        for row in read_rows(site.results_path(BASE)):
            if row["status"] == "ok" and row["key"]:
                fails.discard(row["user"])
            elif row["status"] == "fail" and row["user"] not in keys:
                fails.add(row["user"])
        reg = {s.username for s in sess.list_sessions(site.sessions_dir(BASE))} | set(keys)
        today = datetime.now().strftime("%Y-%m-%d")
        signed = {row["user"] for row in read_rows(site.checkin_path(BASE))
                  if row["time"].startswith(today) and row["status"] in ("ok", "done")}
        return keys, reg, fails, signed

    def overview_refresh(self) -> None:
        """重画矩阵。站点多了以后靠这一页看全局，不用来回切站点。"""
        self.ov_sites = list(sites.SITES)
        cols = ("账号", *[s.name for s in self.ov_sites])
        self.ov_tree.config(columns=cols)
        for i, c in enumerate(cols):
            self.ov_tree.heading(c, text=c)
            self.ov_tree.column(c, width=150 if i == 0 else 96, anchor="w")
        self.ov_tree.delete(*self.ov_tree.get_children())
        try:
            accounts = load_accounts(self.var_accounts.get())
        except (OSError, ValueError):
            accounts = self.accounts
        state = {s.key: self._ov_state(s) for s in self.ov_sites}
        self._ov_keys = {k: v[0] for k, v in state.items()}
        for a in accounts:
            cells = []
            for s in self.ov_sites:
                keys, reg, fails, signed = state[s.key]
                mark = ("✓" if a.username in keys else
                        "○" if a.username in reg else
                        "×" if a.username in fails else "")
                cells.append(mark + ("签" if a.username in signed else ""))
            self.ov_tree.insert("", "end", values=(a.username, *cells))
        total = len(accounts)
        parts = [f"{s.name} {len(state[s.key][0])}/{total}" for s in self.ov_sites]
        self.var_ov_summary.set(f"共 {total} 个账号　|　有 Key：" + "　·　".join(parts))

    def _ov_copy_cell(self, evt) -> None:
        """双击矩阵里的格子，复制那个站点下这个账号的 API Key。"""
        iid = self.ov_tree.identify_row(evt.y)
        col = self.ov_tree.identify_column(evt.x)
        if not iid or not col:
            return
        idx = int(col[1:]) - 1
        user = self.ov_tree.item(iid, "values")[0]
        if idx <= 0 or idx > len(self.ov_sites):
            self.var_progress.set(f"{user}：双击右边站点那一格才会复制 Key")
            return
        site = self.ov_sites[idx - 1]
        key = self._ov_keys.get(site.key, {}).get(user, "")
        if not key:
            self.var_progress.set(f"{user} 在 {site.name} 还没有 Key")
            return
        self.clipboard_clear()
        self.clipboard_append(key)
        self.update()
        self.var_progress.set(f"已复制 {user} 在 {site.name} 的 API Key")

    # ---------------- 站点页 ----------------

    SITE_COLS = ("显示名", "key", "邀请码", "启用", "签到方式", "邀请链接", "数据文件")

    def _build_sites(self, root: ttk.Frame) -> None:
        bar = ttk.Frame(root)
        bar.pack(fill="x", **PAD)
        ttk.Label(bar, text="站点登记表存在 sites.json，改完点「保存」。顺序 = 下拉框和"
                            "「所有站点」的运行顺序；删站点不会删已经拿到的 key 和登录态。"
                  ).pack(side="left")
        ttk.Button(bar, text="保存", command=self.sites_save).pack(side="right")
        ttk.Button(bar, text="重新载入", command=self.sites_load).pack(side="right", padx=6)

        self.site_tree = ttk.Treeview(root, columns=self.SITE_COLS, show="headings", height=12)
        for c, w in zip(self.SITE_COLS, (120, 110, 70, 50, 110, 300, 200)):
            self.site_tree.heading(c, text=c)
            self.site_tree.column(c, width=w, anchor="w")
        self.site_tree.pack(fill="both", expand=True, **PAD)
        self.site_tree.bind("<<TreeviewSelect>>", self._site_fill_form)

        form = ttk.Frame(root)
        form.pack(fill="x", **PAD)
        ttk.Label(form, text="显示名").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.var_site_name, width=18).grid(row=1, column=0, padx=2)
        ttk.Label(form, text="key（数据目录名）").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Entry(form, textvariable=self.var_site_key, width=14).grid(row=1, column=1,
                                                                      padx=(8, 2))
        ttk.Label(form, text="邀请链接（带 aff 的完整注册地址）").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.var_site_url).grid(row=1, column=2, sticky="ew", padx=2)
        ttk.Label(form, text="签到方式").grid(row=0, column=3, sticky="w", padx=(8, 0))
        ttk.Combobox(form, textvariable=self.var_site_method, width=8, state="readonly",
                     values=tuple(sites.METHODS)).grid(row=1, column=3, padx=(8, 2))
        form.columnconfigure(2, weight=1)
        ttk.Label(form, text="key 决定这个站点所有数据的目录/文件名（logs/<key>/、"
                            "data/sessions/<key>/、data/keys/<key>.json …）：留空就从域名推；"
                            "改了它「更新选中」会问你要不要把已有数据一起搬过去。"
                            "签到方式 auto = 按「接口直签 → 取令牌 → 走界面」试一遍并记住成功的那种"
                  ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))

        ops = ttk.Frame(root)
        ops.pack(fill="x", **PAD)
        ttk.Button(ops, text="添加为新站点", command=self.site_add).pack(side="left")
        ttk.Button(ops, text="更新选中", command=self.site_update).pack(side="left", padx=6)
        ttk.Button(ops, text="删除选中", command=self.site_delete).pack(side="left")
        ttk.Button(ops, text="停用/启用", command=self.site_toggle).pack(side="left", padx=6)
        ttk.Button(ops, text="上移", width=6,
                   command=lambda: self.site_move(-1)).pack(side="left", padx=(18, 2))
        ttk.Button(ops, text="下移", width=6, command=lambda: self.site_move(1)).pack(side="left")
        self.sites_load()

    def _site_rows(self) -> None:
        """按 self.site_list 重画表格。"""
        self.site_tree.delete(*self.site_tree.get_children())
        for s in self.site_list:
            files = (f"{s.results_path(BASE).name} / {s.sessions_dir(BASE).name}"
                     + ("（默认站点）" if s.default else ""))
            method = s.checkin_method + (f"（上次 {s.last_ok_method}）"
                                         if s.checkin_method == "auto" and s.last_ok_method
                                         else "")
            self.site_tree.insert("", "end", values=(s.name, s.key, s.aff or "-",
                                                     "是" if s.enabled else "否", method,
                                                     s.signup_url, files))

    def sites_load(self) -> None:
        self.site_list = sites.load(BASE)
        self._site_rows()
        self.var_progress.set(f"站点登记表载入 {len(self.site_list)} 个站点")

    def _site_fill_form(self, _evt=None) -> None:
        sel = self.site_tree.selection()
        if not sel:
            return
        i = self.site_tree.index(sel[0])
        site = self.site_list[i]
        self.var_site_name.set(site.name)
        self.var_site_key.set(site.key)
        self.var_site_url.set(site.signup_url)
        self.var_site_method.set(site.checkin_method)

    def _site_form(self):
        """按表单造一个 Site。key 留空就从域名推，填了就用你填的（会清掉不能做文件名的字符）。"""
        name, url = self.var_site_name.get().strip(), self.var_site_url.get().strip()
        if not url.startswith("http"):
            messagebox.showwarning("缺字段", "邀请链接要填完整地址，比如 https://xxx.com/sign-up?aff=abc")
            return None
        key = sites.safe_key(self.var_site_key.get())
        if self.var_site_key.get().strip() and not key:
            messagebox.showwarning("key 不合法", "key 只能用字母、数字、- 和 _（它要当目录名）")
            return None
        return sites.with_method(sites.make(name, url, key), self.var_site_method.get())

    def site_add(self) -> None:
        site = self._site_form()
        if site is None:
            return
        if any(s.key == site.key for s in self.site_list):
            messagebox.showinfo("已存在", f"已经有 key={site.key} 的站点了，改用「更新选中」")
            return
        self.site_list.append(site)
        self._site_rows()
        self.var_progress.set(f"已添加 {site.name}，记得点「保存」")

    def site_update(self) -> None:
        sel = self.site_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选一行")
            return
        new = self._site_form()
        if new is None:
            return
        i = self.site_tree.index(sel[0])
        old = self.site_list[i]
        if new.key != old.key and not self._move_site_data(old, new.key):
            return
        # 保留默认标记、启用状态和"上次成功的签到方式"
        self.site_list[i] = sites.Site(key=new.key, name=new.name, signup_url=new.signup_url,
                                       default=old.default, enabled=old.enabled,
                                       checkin_method=new.checkin_method,
                                       last_ok_method=old.last_ok_method)
        self._site_rows()
        self.var_progress.set("已更新选中站点，记得点「保存」")

    def _move_site_data(self, old, new_key: str) -> bool:
        """key 改了：问一声，然后把这个站点已有的数据一起搬到新 key 下。返回能不能继续。

        搬的是 `logs/<key>/`、`data/sessions/<key>/` 和 keys / keys-meta / tokens 三个 json。
        只要有一个目标已存在就整体不动手——宁可让你先手工处理，也不要搬一半。
        """
        pending = sites.data_moves(old, new_key, BASE)
        if not pending:
            self.log(f"{old.name} 的 key 改成 {new_key}（这个站点还没有数据文件，不用搬）")
            return True
        conflicts = [f"{dst.name} 已存在" for _src, dst in pending if dst.exists()]
        if conflicts:
            messagebox.showwarning(
                "有冲突，没动手",
                f"要搬到 {new_key} 下面的这些东西已经存在：\n  " + "\n  ".join(conflicts)
                + "\n\n先手工处理掉再改 key（避免覆盖数据）。")
            return False
        listing = "\n  ".join(f"{src.name} → {dst}" for src, dst in pending)
        if not messagebox.askokcancel(
                "把数据一起搬过去？",
                f"key 从 {old.key} 改成 {new_key}。这个站点的数据都是按 key 命名的，"
                f"要一起搬过去才认得出来：\n\n  {listing}\n\n"
                "（登录态、API Key、令牌、日志都在里面；不搬的话这个站点会当成全新的。）"):
            return False
        moved, failed = sites.move_data(old, new_key, BASE)
        for line in moved:
            self.log(f"  已搬 {line}")
        for line in failed:
            self.log(f"  [失败] {line}")
        if failed:
            messagebox.showwarning("有几项没搬成", "\n".join(failed)
                                   + "\n\n登记表已按新 key 更新，请手工核对上面这几项。")
        self.var_progress.set(f"已把 {len(moved)} 项数据搬到 {new_key}")
        return True

    def site_delete(self) -> None:
        sel = self.site_tree.selection()
        if not sel:
            return
        i = self.site_tree.index(sel[0])
        site = self.site_list[i]
        if site.default:
            messagebox.showinfo("不能删", "这是默认站点（界面和命令行默认选它，老数据迁移也认它）。"
                                          "不想跑它就设成「停用」，或者先把别的站点设成默认。")
            return
        if not messagebox.askokcancel("删除", f"从登记表里删掉 {site.name}？\n"
                                              f"已经拿到的 key 和登录态文件不会被删。"):
            return
        del self.site_list[i]
        self._site_rows()
        self.var_progress.set(f"已删除 {site.name}，记得点「保存」")

    def site_toggle(self) -> None:
        sel = self.site_tree.selection()
        if not sel:
            return
        i = self.site_tree.index(sel[0])
        self.site_list[i] = sites.with_enabled(self.site_list[i], not self.site_list[i].enabled)
        self._site_rows()
        self.var_progress.set("已切换启用状态，记得点「保存」")

    def site_move(self, step: int) -> None:
        sel = self.site_tree.selection()
        if not sel:
            return
        i = self.site_tree.index(sel[0])
        j = i + step
        if 0 <= j < len(self.site_list):
            self.site_list[i], self.site_list[j] = self.site_list[j], self.site_list[i]
            self._site_rows()
            self.site_tree.selection_set(self.site_tree.get_children()[j])

    def sites_save(self) -> None:
        if not self.site_list:
            messagebox.showwarning("空登记表", "一个站点都没有，不写文件")
            return
        try:
            sites.save(self.site_list, BASE)
        except OSError as exc:
            messagebox.showerror("写入失败", str(exc))
            return
        self._refresh_site_widgets()
        self.log(f"已保存站点登记表（{len(self.site_list)} 个）→ {sites.CONFIG_NAME}")
        self.var_progress.set(f"已保存 {len(self.site_list)} 个站点")

    def _refresh_site_widgets(self) -> None:
        """站点表变了：刷新各处下拉框和总览。运行页只列**启用**的站点。"""
        names = sites.enabled_names()
        if hasattr(self, "cb_site"):
            self.cb_site.config(values=names)
            if self.var_site.get() not in names:
                self.var_site.set(names[0] if names else sites.DEFAULT.name)
        if hasattr(self, "cb_export_scope"):     # 导出页仍然列全部（停用站点的 key 也要能导）
            all_names = sites.names()
            self.cb_export_scope.config(values=("全部", *all_names))
            if self.var_export_scope.get() not in ("全部", *all_names):
                self.var_export_scope.set("全部")
        if hasattr(self, "ov_tree"):
            self.overview_refresh()
        # 名字没变但 key 可能改了（数据路径全跟着 key 走），所以强制按登记表重算一遍
        self._on_site_change()

    # ---------------- 会话页 ----------------

    def _build_sessions(self, root: ttk.Frame) -> None:
        ttk.Label(root, text="登录态按站点分目录保存（复用它就不用再走 GitHub 登录）；"
                            "这里显示的是「运行」页当前选中站点的登录态。"
                            "打包文件等同于登录凭证，注意保管。").pack(anchor="w", **PAD)
        cols = ("账号", "保存时间", "uid", "最近签到")
        self.sess_tree = ttk.Treeview(root, columns=cols, show="headings", height=14)
        for c, w in zip(cols, (220, 170, 90, 120)):
            self.sess_tree.heading(c, text=c)
            self.sess_tree.column(c, width=w, anchor="w")
        self.sess_tree.pack(fill="both", expand=True, **PAD)

        ops = ttk.Frame(root)
        ops.pack(fill="x", **PAD)
        ttk.Button(ops, text="打开已登录窗口", command=self.open_session).pack(side="left")
        ttk.Button(ops, text="刷新", width=6, command=self.refresh_sessions).pack(side="left",
                                                                                 padx=6)
        ttk.Button(ops, text="删除选中", command=self.drop_session).pack(side="left")
        ttk.Button(ops, text="导出打包…", command=self.export_sessions).pack(side="right")
        ttk.Button(ops, text="导入打包…", command=self.import_sessions).pack(side="right", padx=6)
        self.refresh_sessions()

    def refresh_sessions(self) -> None:
        self.sess_tree.delete(*self.sess_tree.get_children())
        for s in sess.list_sessions(self._sess_dir()):
            self.sess_tree.insert("", "end",
                                  values=(s.username, s.saved_text, s.uid, s.last_checkin))

    def _sess_selected(self) -> str:
        sel = self.sess_tree.selection()
        return self.sess_tree.item(sel[0], "values")[0] if sel else ""

    def open_session(self) -> None:
        self.open_account_window(self._sess_selected())

    def open_selected_account(self) -> None:
        """运行页表格里选中的账号：有登录态就直接进，没有就用 GitHub 登一次。"""
        sel = self.tree.selection()
        user = self.tree.item(sel[0], "values")[1] if sel else ""
        self.open_account_window(user)

    def open_account_window(self, user: str) -> None:
        if not user:
            messagebox.showinfo("提示", "先在列表里选一个账号")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "任务正在跑，先停下再开窗口")
            return
        st = self._settings()
        acct = next((a for a in self.accounts if a.username == user), None)
        if acct is None and not sess.has_session(user, self._sess_dir()):
            messagebox.showwarning("没法登录", f"账号库里没有 {user}，也没有它的登录态")
            return
        self.log(f"正在打开 {user} 的浏览器窗口…")

        def work() -> None:
            try:
                open_account(user, st, log=lambda m: self.q.put(("log", m)), acct=acct)
            except Exception as exc:  # noqa: BLE001
                self.q.put(("log", f"打开失败: {exc}"))
            finally:
                self.q.put(("sess",))

        threading.Thread(target=work, daemon=True).start()

    def drop_session(self) -> None:
        user = self._sess_selected()
        if user and messagebox.askokcancel("删除", f"删掉 {user} 的登录态？下次要重新登录。"):
            sess.drop_session(user, self._sess_dir())
            self.refresh_sessions()

    def export_sessions(self) -> None:
        path = filedialog.asksaveasfilename(initialfile=f"sessions-{self.site.key}.json",
                                            defaultextension=".json")
        if path:
            n = sess.export_bundle(path, base=self._sess_dir())
            self.var_progress.set(f"已打包 {n} 个登录态 → {path}")

    def import_sessions(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("打包文件", "*.json"), ("全部", "*.*")])
        if not path:
            return
        mode = self._ask_mode(
            "怎么导入登录态",
            f"从 {Path(path).name} 导入到站点「{self.site.name}」的登录态。\n\n"
            "新增合并：只导入本地没有的账号，已有的保持不动\n"
            "覆盖：同名账号的登录态用文件里的替换掉",
        )
        if not mode:
            return
        try:
            users = sess.import_bundle(path, base=self._sess_dir(),
                                       overwrite=(mode == "replace"))
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self.refresh_sessions()
        self.var_progress.set(f"已导入 {len(users)} 个登录态（{'覆盖' if mode == 'replace' else '新增'}）")

    # ---------------- 账号池页 ----------------

    POOL_COLS = ("账号", "密码", "密钥", "邮箱", "邮密", "启用")

    def _build_pool(self, root: ttk.Frame) -> None:
        bar = ttk.Frame(root)
        bar.pack(fill="x", **PAD)
        ttk.Label(bar, text="改完记得点「保存」。主格式是 accounts.json，也能导入导出 csv / txt。"
                  ).pack(side="left")
        ttk.Button(bar, text="保存", command=self.pool_save).pack(side="right")
        ttk.Button(bar, text="重新载入", command=self.pool_load).pack(side="right", padx=6)
        ttk.Button(bar, text="导出…", command=self.pool_export).pack(side="right")
        ttk.Button(bar, text="导入…", command=self.pool_import).pack(side="right", padx=6)

        self.pool = ttk.Treeview(root, columns=self.POOL_COLS, show="headings", height=13)
        for c, w in zip(self.POOL_COLS, (150, 140, 180, 170, 130, 50)):
            self.pool.heading(c, text=c)
            self.pool.column(c, width=w, anchor="w")
        self.pool.pack(fill="both", expand=True, **PAD)
        self.pool.bind("<<TreeviewSelect>>", self._pool_fill_form)

        form = ttk.Frame(root)
        form.pack(fill="x", **PAD)
        for i, (label, var) in enumerate(zip(self.POOL_COLS[:5], self.pool_vars)):
            ttk.Label(form, text=label).grid(row=0, column=i, sticky="w")
            ttk.Entry(form, textvariable=var, width=20).grid(row=1, column=i, sticky="ew", padx=2)
            form.columnconfigure(i, weight=1)
        ttk.Checkbutton(form, text="启用", variable=self.var_pool_enabled).grid(
            row=1, column=5, padx=6)

        ops = ttk.Frame(root)
        ops.pack(fill="x", **PAD)
        ttk.Button(ops, text="添加为新行", command=self.pool_add).pack(side="left")
        ttk.Button(ops, text="更新选中行", command=self.pool_update).pack(side="left", padx=6)
        ttk.Button(ops, text="删除选中行", command=self.pool_delete).pack(side="left")
        ttk.Button(ops, text="停用/启用", command=self.pool_toggle).pack(side="left", padx=6)
        ttk.Button(ops, text="上移", width=6,
                   command=lambda: self.pool_move(-1)).pack(side="left", padx=(18, 2))
        ttk.Button(ops, text="下移", width=6, command=lambda: self.pool_move(1)).pack(side="left")
        self.pool_load()

    def pool_load(self) -> None:
        try:
            accounts = load_accounts(self.var_accounts.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("账号库读取失败", str(exc))
            return
        self.pool.delete(*self.pool.get_children())
        for a in accounts:
            self.pool.insert("", "end", values=(a.username, a.password, a.totp_secret,
                                                a.email, a.email_password,
                                                "是" if a.enabled else "否"))
        self.var_progress.set(f"账号池载入 {len(accounts)} 行")

    def _pool_fill_form(self, _evt=None) -> None:
        sel = self.pool.selection()
        if not sel:
            return
        vals = self.pool.item(sel[0], "values")
        for var, val in zip(self.pool_vars, vals[:5]):
            var.set(val)
        self.var_pool_enabled.set(vals[5] != "否" if len(vals) > 5 else True)

    def _pool_form_values(self) -> tuple[str, ...] | None:
        vals = tuple(v.get().strip() for v in self.pool_vars)
        if not (vals[0] and vals[1] and vals[2]):
            messagebox.showwarning("缺字段", "账号、密码、密钥都要填")
            return None
        return vals + ("是" if self.var_pool_enabled.get() else "否",)

    def pool_add(self) -> None:
        vals = self._pool_form_values()
        if vals:
            self.pool.selection_set(self.pool.insert("", "end", values=vals))

    def pool_update(self) -> None:
        sel = self.pool.selection()
        if not sel:
            messagebox.showinfo("提示", "先选一行")
            return
        vals = self._pool_form_values()
        if vals:
            self.pool.item(sel[0], values=vals)

    def pool_delete(self) -> None:
        sel = self.pool.selection()
        if sel and messagebox.askokcancel("删除", f"删除 {len(sel)} 行？"):
            self.pool.delete(*sel)

    def pool_toggle(self) -> None:
        for iid in self.pool.selection():
            vals = list(self.pool.item(iid, "values"))
            vals[5] = "否" if vals[5] == "是" else "是"
            self.pool.item(iid, values=vals)

    def pool_move(self, step: int) -> None:
        sel = self.pool.selection()
        if not sel:
            return
        idx = self.pool.index(sel[0]) + step
        if 0 <= idx < len(self.pool.get_children()):
            self.pool.move(sel[0], "", idx)

    def _pool_accounts(self) -> list[Account]:
        out = []
        for iid in self.pool.get_children():
            v = list(self.pool.item(iid, "values")) + [""] * 6
            out.append(Account(username=v[0], password=v[1], totp_secret=v[2],
                               email=v[3], email_password=v[4], enabled=v[5] != "否"))
        return out

    def pool_save(self) -> None:
        accounts = self._pool_accounts()
        if not accounts:
            messagebox.showwarning("空账号池", "一行都没有，不写文件")
            return
        path = self.var_accounts.get()
        try:
            save_accounts(path, accounts)
        except OSError as exc:
            messagebox.showerror("写入失败", str(exc))
            return
        self.var_progress.set(f"已写入 {len(accounts)} 行到 {Path(path).name}")
        self.load()

    def _ask_mode(self, title: str, message: str) -> str:
        """问用户是新增合并还是覆盖，返回 'merge' / 'replace' / ''（取消）。"""
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.resizable(False, False)
        picked = {"v": ""}

        def pick(value: str) -> None:
            picked["v"] = value
            win.destroy()

        ttk.Label(win, text=message, justify="left", wraplength=400).pack(padx=16, pady=14)
        row = ttk.Frame(win)
        row.pack(pady=(0, 14))
        ttk.Button(row, text="新增合并", command=lambda: pick("merge")).pack(side="left", padx=6)
        ttk.Button(row, text="覆盖", command=lambda: pick("replace")).pack(side="left", padx=6)
        ttk.Button(row, text="取消", command=lambda: pick("")).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: pick(""))
        win.grab_set()
        self.wait_window(win)
        return picked["v"]

    def pool_import(self) -> None:
        path = filedialog.askopenfilename(filetypes=ACCOUNT_FILETYPES)
        if not path:
            return
        try:
            incoming = load_accounts(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        current = self._pool_accounts()
        mode = self._ask_mode(
            "怎么导入",
            f"从 {Path(path).name} 读到 {len(incoming)} 个账号，当前列表里有 {len(current)} 个。\n\n"
            "新增合并：保留现在的，只把没见过的账号加到后面（重名的跳过）\n"
            "覆盖：清空现在的，只留导入的这份",
        )
        if not mode:
            return
        merged, stats = merge_accounts(current, incoming, mode)
        self.pool.delete(*self.pool.get_children())
        for a in merged:
            self.pool.insert("", "end", values=(a.username, a.password, a.totp_secret,
                                                a.email, a.email_password,
                                                "是" if a.enabled else "否"))
        detail = (f"新增 {stats['added']} 个，重名跳过 {stats['skipped']} 个"
                  if mode == "merge" else f"覆盖为 {stats['added']} 个")
        self.log(f"导入 {Path(path).name}：{detail}，现在共 {len(merged)} 行，记得点「保存」")
        self.var_progress.set(f"{detail}，共 {len(merged)} 行，记得点「保存」")

    def pool_export(self) -> None:
        path = filedialog.asksaveasfilename(initialfile="accounts-export.json",
                                            defaultextension=".json",
                                            filetypes=ACCOUNT_FILETYPES)
        if not path:
            return
        fmt = detect_format(path)
        Path(path).write_text(dump_accounts(self._pool_accounts(), fmt) + "\n",
                              encoding="utf-8", newline="\n")
        self.var_progress.set(f"已导出 {fmt} 格式 → {path}")

    # ---------------- 导出页 ----------------

    def _build_export(self, root: ttk.Frame) -> None:
        bar = ttk.Frame(root)
        bar.pack(fill="x", **PAD)
        ttk.Label(bar, text="站点").pack(side="left")
        self.cb_export_scope = ttk.Combobox(bar, textvariable=self.var_export_scope, width=12,
                                            state="readonly", values=("全部", *sites.names()))
        self.cb_export_scope.pack(side="left", padx=(2, 10))
        self.var_export_scope.trace_add("write", lambda *_: self.export_preview())
        ttk.Label(bar, text="输出方式").pack(side="left")
        for text, val in (("表格（账号 + key）", "table"), ("纯 API Key", "keys"),
                          ("CSV（可进 Excel）", "csv")):
            ttk.Radiobutton(bar, text=text, value=val, variable=self.var_fmt,
                            command=self.export_preview).pack(side="left", padx=6)
        ttk.Button(bar, text="刷新预览", command=self.export_preview).pack(side="left",
                                                                       padx=(12, 0))
        ttk.Label(bar, text="分组筛选").pack(side="left", padx=(14, 2))
        ttk.Combobox(bar, textvariable=self.var_filter_group, width=10, state="readonly",
                     values=("", *keymeta.GROUPS)).pack(side="left")
        self.var_filter_group.trace_add("write", lambda *_: self.export_preview())

        ops = ttk.Frame(root)
        ops.pack(fill="x", **PAD)
        ttk.Button(ops, text="复制到剪贴板", command=self.export_copy).pack(side="left")
        ttk.Button(ops, text="另存为文件…", command=self.export_save).pack(side="left", padx=6)
        ttk.Label(ops, text="表格点表头可排序（再点一次反向）；「输出方式」只影响复制/另存的格式"
                  ).pack(side="left", padx=8)

        wrap = ttk.Frame(root)
        wrap.pack(fill="both", expand=True, **PAD)
        self.out_tree = ttk.Treeview(wrap, columns=self.EXPORT_COLS, show="headings")
        for c, w in zip(self.EXPORT_COLS, (60, 130, 300, 150, 70, 70, 160)):
            self.out_tree.column(c, width=w, anchor="w")
        bar_y = ttk.Scrollbar(wrap, orient="vertical", command=self.out_tree.yview)
        self.out_tree.configure(yscrollcommand=bar_y.set)
        bar_y.pack(side="right", fill="y")
        self.out_tree.pack(side="left", fill="both", expand=True)
        self._sortable(self.out_tree, self.EXPORT_COLS)
        self._tip_bind(self.out_tree, self.EXPORT_COLS)
        self.out_tree.bind("<Double-1>", self._export_copy_cell)
        self.export_preview()

    def _export_sites(self) -> list:
        scope = self.var_export_scope.get()
        if scope != "全部":
            try:
                return [sites.by_key(scope)]
            except KeyError:
                pass
        return list(sites.SITES)

    def _export_text(self) -> str:
        return export.render(self._export_rows(), self.var_fmt.get(), with_site=True)

    def _export_rows(self) -> list[tuple[str, ...]]:
        return export.collect_scoped(self._export_sites(), BASE, self.var_accounts.get(),
                                     self.var_filter_group.get())

    def export_preview(self) -> None:
        """把收集到的行填进表格（列顺序按 EXPORT_COLS）。"""
        self.out_tree.delete(*self.out_tree.get_children())
        # collect_scoped 给的是 (账号, key, 时间, 分组, 备注, 站点, 剩余额度)
        for i, r in enumerate(self._export_rows(), start=1):
            user, key, when, group, note, site, quota = (list(r) + [""] * 7)[:7]
            self.out_tree.insert("", "end", values=(i, site, key, user, quota, group,
                                                    note or when))
        self.var_progress.set(f"导出预览：{len(self.out_tree.get_children())} 条")

    def _export_copy_cell(self, _evt=None) -> None:
        """双击一行就把它的 API Key 复制走（表格里最常要的就是这个）。"""
        sel = self.out_tree.selection()
        if not sel:
            return
        key = self.out_tree.item(sel[0], "values")[2]
        self.clipboard_clear()
        self.clipboard_append(key)
        self.update()
        self.var_progress.set(f"已复制 {key[:14]}…")

    def export_copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._export_text())
        self.update()  # 让剪贴板内容真正生效
        self.var_progress.set("已复制到剪贴板")

    def export_save(self) -> None:
        fmt = self.var_fmt.get()
        scope = self.var_export_scope.get()
        tag = "all" if scope == "全部" else scope
        default = {"table": f"keys-{tag}.txt", "keys": f"keys-{tag}.txt",
                   "csv": f"keys-{tag}.csv"}[fmt]
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(initialfile=default,
                                            initialdir=str(EXPORTS_DIR),
                                            defaultextension=Path(default).suffix)
        if not path:
            return
        Path(path).write_text(self._export_text() + "\n", encoding="utf-8", newline="\n")
        self.var_progress.set(f"已导出到 {path}")

    # ---------------- 线程消息 ----------------

    STATUS_TEXT = {"ok": "成功", "done": "已签到", "fail": "失败", "skip": "跳过"}

    def _drain(self) -> None:
        """主线程里消费工作线程的消息，Tk 控件只在这里改。"""
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log(msg[1])
                elif kind == "prog":
                    _, what, idx, total, user = msg
                    self.var_progress.set(f"{what} {idx}/{total}：{user}")
                elif kind == "row":
                    _, user, status, key, note = msg
                    iid = self.rows.get(user)
                    old = list(self.tree.item(iid, "values")) + [""] * 7 if iid else [""] * 7
                    if not key:  # 签到之类的任务不带 key，别把已有的擦掉
                        key = old[3]
                    # 额度是 runner 记进 key 表的，这里读回来刷新（读不到就保留旧值）
                    quota = keystore.fmt_quota(
                        keystore.get(self.site.keys_path(BASE), user).get("quota")) or old[4]
                    # 分组（第 1 列）和备注是人工填的，保留
                    values = (old[0], user, self.STATUS_TEXT.get(status, status), key,
                              quota, old[5], note)
                    if iid:
                        self.tree.item(iid, values=values)
                        self.tree.see(iid)
                    else:
                        self.rows[user] = self.tree.insert("", "end", values=values)
                elif kind == "sess":
                    self.refresh_sessions()
                elif kind == "end":
                    for b in (self.btn_start, self.btn_checkin, self.btn_assist):
                        b.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_pause.config(state="disabled", text="暂停")
                    self.var_progress.set("已结束")
                    self.refresh_sessions()
                    self.overview_refresh()
                    self.export_preview()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("退出", "任务还在跑，确定退出？"):
                return
            self.stop_evt.set()
        self.destroy()


if __name__ == "__main__":
    # exe 是 --noconsole 打的：双击是开界面（不会有黑窗）；带参数时当命令行用，
    # 这时把调用方的控制台接过来，输出才看得见（TabiTool.exe --checkin）
    if len(sys.argv) > 1:
        import cli
        from paths import attach_console

        attach_console()
        raise SystemExit(cli.main(sys.argv[1:]))
    try:
        App().mainloop()
    except Exception as exc:  # noqa: BLE001 - 窗口模式没有控制台，崩了要留痕
        from paths import crash_log

        path = crash_log(exc)
        try:
            messagebox.showerror("程序异常退出", f"{exc}\n\n详细信息写到了 {path}")
        except Exception:  # noqa: BLE001 - Tk 都起不来就只能靠日志了
            pass
        raise
