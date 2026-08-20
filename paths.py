"""目录规划 + 老版本文件的一次性迁移。

打包成 exe 之后 `__file__` 指向临时解包目录，数据文件必须放在 exe 旁边，
所以所有路径统一从这里取。目录长这样（都在程序/exe 同级）：

    data/                 所有"数据"都在这里
      accounts.json
      sites.json
      keys/<站点>.json        拿到的 API Key
      keys-meta/<站点>.json   key 的人工标记
      tokens/<站点>.json      站点访问令牌
      sessions/<站点>/        每个站点自己一份登录态（+ index.json 元信息）
    logs/<站点>/          注册 / 签到 / 改密 的结果日志
      results.log  checkin.log  password.log
      shots/              失败截图
    exports/              导出的 key 表格 / 纯 key / CSV
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_dir() -> Path:
    """程序（或 exe）所在目录，所有数据都挂在它下面。"""
    if getattr(sys, "frozen", False):  # PyInstaller 打出来的 exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DATA_DIR = APP_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
LOGS_DIR = APP_DIR / "logs"
EXPORTS_DIR = APP_DIR / "exports"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
LOG_KINDS = ("results", "checkin", "password")
# 第一次运行（尤其是打包出去的 exe）时自己建出来的空数据文件——**打包不带任何账号/站点信息**，
# 缺文件就建一个空的，别让程序起不来。
EMPTY_FILES = {
    "data/accounts.json": '{\n  "version": 1,\n  "accounts": []\n}\n',
    "data/sites.json": '{\n  "version": 1,\n  "sites": []\n}\n',
}


def ensure_dirs(base: Path | None = None) -> None:
    """把顶层目录建出来（缺哪个建哪个）。"""
    root = Path(base or APP_DIR)
    for d in ("data", "data/sessions", "logs", "exports"):
        (root / d).mkdir(parents=True, exist_ok=True)


def ensure_data_files(base: Path | None = None) -> list[str]:
    """缺账号库 / 站点登记表就建一个空的，返回建了哪些（已存在的一律不动）。"""
    root = Path(base or APP_DIR)
    made: list[str] = []
    for rel, text in EMPTY_FILES.items():
        p = root / rel
        if p.exists():
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            made.append(rel)
        except OSError:
            pass
    return made


def _move(src: Path, dst: Path, moved: list[str]) -> None:
    """搬一个文件/目录；目标已存在就不动，免得覆盖新数据。"""
    if not src.exists() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    moved.append(f"{src.name} → {dst}")


def _legacy_default_key(base: Path) -> str:
    """老布局里不带后缀的那些文件属于哪个站点（= 原来的默认站点）。"""
    import json

    for cfg in (base / "data" / "sites.json", base / "sites.json"):
        try:
            rows = json.loads(cfg.read_text(encoding="utf-8-sig")).get("sites", [])
        except (OSError, ValueError, AttributeError):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("default"):
                return str(row.get("key") or "site-a")
    return "site-a"


def _wrap_std_handle(which: int, mode: str):
    """把 Windows 的标准句柄包成 Python 文件对象。拿不到就返回 None。

    `--noconsole` 打出来的 exe 自己没有控制台，但从 cmd/PowerShell 启动、或者被重定向
    （`TabiTool.exe --sites > out.txt`、管道）时，父进程会把句柄传进来，这时直接用它，
    输出才能被重定向/被管道接住。
    """
    import ctypes
    import msvcrt

    try:
        handle = ctypes.windll.kernel32.GetStdHandle(which)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        flags = os.O_WRONLY if "w" in mode else os.O_RDONLY
        fd = msvcrt.open_osfhandle(handle, flags)
        return open(fd, mode, buffering=1, encoding="utf-8", errors="replace",
                    closefd=False)
    except (OSError, ValueError, AttributeError):
        return None


def migrate_layout(base: Path | None = None) -> list[str]:
    """把老版本的文件搬到现在的布局，返回搬了哪些。

    历史上搬过三次，这里三次都兜着（只在"源存在且目标不存在"时动手，跑几遍都安全）：

    1. 最早：`accounts.json`、`results.txt`、`results-<站点>.txt`、`sessions/`、
       `sessions-<站点>/`、`keys-meta*.json`、`shots/` 全平铺在程序根目录
    2. 之后：按站点分目录（`logs/<站点>/`、`sessions/<站点>/`）
    3. 现在：登录态挪进 `data/sessions/<站点>/`，日志后缀从 `.txt` 改成 `.log`
    """
    root = Path(base or APP_DIR)
    moved: list[str] = []
    data, logs = root / "data", root / "logs"
    old_sess, sess = root / "sessions", data / "sessions"
    main_key = _legacy_default_key(root)

    for name in ("accounts.json", "accounts.txt", "sites.json"):
        _move(root / name, data / name, moved)

    # 老的默认站点：不带后缀的那几个
    _move(root / "keys-meta.json", data / "keys-meta" / f"{main_key}.json", moved)
    for kind in LOG_KINDS:
        _move(root / f"{kind}.txt", logs / main_key / f"{kind}.log", moved)
        # 其它站点：<kind>-<站点>.txt
        for old in sorted(root.glob(f"{kind}-*.txt")):
            key = old.stem[len(kind) + 1:]
            if key:
                _move(old, logs / key / f"{kind}.log", moved)
    for old in sorted(root.glob("keys-meta-*.json")):
        key = old.stem[len("keys-meta-"):]
        if key:
            _move(old, data / "keys-meta" / f"{key}.json", moved)

    # 日志后缀 .txt → .log（按站点目录逐个改名，内容格式没变）
    if logs.exists():
        for site_dir in sorted(p for p in logs.iterdir() if p.is_dir()):
            for kind in LOG_KINDS:
                _move(site_dir / f"{kind}.txt", site_dir / f"{kind}.log", moved)

    # sessions/ 下原来直接放着老默认站点的登录态（含 index.json），要挪进 <站点>/ 子目录
    if old_sess.exists():
        loose = list(old_sess.glob("*.json"))
        if loose:
            (old_sess / main_key).mkdir(parents=True, exist_ok=True)
            for p in loose:
                _move(p, old_sess / main_key / p.name, moved)
    for old in sorted(root.glob("sessions-*")):
        if old.is_dir():
            _move(old, old_sess / old.name[len("sessions-"):], moved)
    # 整个 sessions/ 挪进 data/（登录态属于数据，和 keys/tokens 放一起）
    if old_sess.exists() and not sess.exists():
        _move(old_sess, sess, moved)
    elif old_sess.exists():          # data/sessions 已经有了：逐个站点搬，不覆盖
        for old in sorted(p for p in old_sess.iterdir()):
            _move(old, sess / old.name, moved)
        try:
            old_sess.rmdir()         # 空了才删得掉，非空就留着让人自己看
        except OSError:
            pass

    _move(root / "shots", logs / "shots", moved)
    for name in ("keys-table.txt", "keys.txt", "keys.csv",
                 "keys-all-table.txt", "keys-all.txt", "keys-all.csv"):
        _move(root / name, root / "exports" / name, moved)
    ensure_dirs(root)
    ensure_data_files(root)
    return moved


def attach_console() -> None:
    """让 exe 当命令行用时输出看得见。

    exe 是 `--noconsole` 打的（否则双击会多一个黑窗），这种进程默认没有控制台、
    `sys.stdout` 是 None。优先用父进程传进来的标准句柄（支持重定向和管道）；
    没有就把调用方那个控制台接过来，输出打到 CONOUT$。双击运行时两样都没有，直接跳过。
    """
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    handles = {"stdout": _wrap_std_handle(-11, "w"), "stderr": _wrap_std_handle(-12, "w")}
    if any(handles.values()):
        for name, stream in handles.items():
            if stream is not None:
                setattr(sys, name, stream)
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1) == 0:  # ATTACH_PARENT_PROCESS
            return
    except (OSError, AttributeError):
        return
    for name in ("stdout", "stderr"):
        try:
            setattr(sys, name, open("CONOUT$", "w", buffering=1,
                                    encoding="utf-8", errors="replace"))
        except OSError:
            pass
    try:
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        pass


def crash_log(exc: BaseException) -> Path:
    """把没接住的异常写到 exe 旁边的 error.log（窗口模式下没有控制台可看）。"""
    import traceback
    from datetime import datetime

    path = APP_DIR / "error.log"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except OSError:
        pass
    return path


def safe_console() -> None:
    """让 print 在 GBK 控制台/重定向到文件时也不会因为个别字符崩掉。

    Windows 上 stdout 重定向后用的是本地编码（cp936），遇到编不出来的字符会直接抛
    UnicodeEncodeError 把程序打断，这里统一降级成替换。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# 导入时就把老布局搬好：sites/keymeta 这些模块一加载就会读配置文件，必须先搬完再读；
# 顺手把缺的空数据文件建出来（打包出去的 exe 第一次跑就是这个情况，不该报错）。
# 跑几遍都安全（只在"源存在且目标不存在"时搬）。要跳过就设 INVITE_SKIP_MIGRATE=1。
if os.environ.get("INVITE_SKIP_MIGRATE") != "1":
    try:
        migrate_layout(APP_DIR)
    except OSError:  # 权限/占用之类的问题不该让程序起不来
        pass
else:
    try:
        ensure_dirs(APP_DIR)
        ensure_data_files(APP_DIR)
    except OSError:
        pass
