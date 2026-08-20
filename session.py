"""站点登录态的存取。

站点会话放在 HttpOnly cookie 里，Playwright 的 storage_state 能整份存下来，之后带着它开
浏览器就是已登录状态，不用再走 GitHub OAuth。按站点分目录：`sessions/<站点>/<账号>.json`，
同目录下一份 `index.json` 记保存时间、uid、最近签到日期等元信息；整套会话可以打包成一个
json 导入导出。

注意：站点的 refresh cookie 是一次性的，每换一次 access_token 就换发新的，所以拿到新
token 后要立刻把状态存回来（见 runner.persist_session）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from paths import SESSIONS_DIR

SESSION_DIR = SESSIONS_DIR  # 不带站点时的兜底目录（正常都会带站点子目录）
INDEX_NAME = "index.json"
BUNDLE_VERSION = 1


@dataclass
class SessionInfo:
    username: str
    path: Path
    saved_at: float
    meta: dict = field(default_factory=dict)

    @property
    def saved_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.saved_at))

    @property
    def uid(self) -> str:
        return str(self.meta.get("uid", "") or "")

    @property
    def last_checkin(self) -> str:
        return str(self.meta.get("last_checkin", "") or "")


def _base(base: Path | None) -> Path:
    return Path(base) if base else SESSION_DIR


def session_path(username: str, base: Path | None = None) -> Path:
    """会话文件路径。账号名里的特殊字符统一换成下划线。"""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in username)
    return _base(base) / f"{safe}.json"


def has_session(username: str, base: Path | None = None) -> bool:
    p = session_path(username, base)
    return p.exists() and p.stat().st_size > 2


def index_path(base: Path | None = None) -> Path:
    return _base(base) / INDEX_NAME


def read_index(base: Path | None = None) -> dict:
    p = index_path(base)
    if not p.exists():
        return {"version": BUNDLE_VERSION, "sessions": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("sessions", {})
        return data
    except (ValueError, OSError):
        return {"version": BUNDLE_VERSION, "sessions": {}}


def write_index(data: dict, base: Path | None = None) -> None:
    p = index_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_meta(username: str, base: Path | None = None, **meta) -> None:
    """更新某个账号的元信息（uid、最近签到日期等）。"""
    data = read_index(base)
    entry = data["sessions"].setdefault(username, {})
    entry.update({k: v for k, v in meta.items() if v is not None})
    entry["saved_at"] = entry.get("saved_at") or time.time()
    write_index(data, base)


def get_meta(username: str, base: Path | None = None) -> dict:
    return read_index(base)["sessions"].get(username, {})


def save_session(ctx, username: str, base: Path | None = None, **meta) -> Path:
    """把当前浏览器上下文的 cookie/localStorage 存下来，并更新索引。"""
    path = session_path(username, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ctx.storage_state(), ensure_ascii=False), encoding="utf-8")
    data = read_index(base)
    entry = data["sessions"].setdefault(username, {})
    entry.update({k: v for k, v in meta.items() if v is not None})
    entry["saved_at"] = time.time()
    entry["file"] = path.name
    write_index(data, base)
    return path


def load_state(username: str, base: Path | None = None) -> dict | None:
    """读回 storage_state，文件坏了就当没有。"""
    path = session_path(username, base)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def list_sessions(base: Path | None = None) -> list[SessionInfo]:
    """列出已保存的登录态，按保存时间从新到旧。"""
    root = _base(base)
    if not root.exists():
        return []
    index = read_index(base)["sessions"]
    out = []
    for p in root.glob("*.json"):
        if p.name == INDEX_NAME:
            continue
        meta = index.get(p.stem, {})
        out.append(
            SessionInfo(username=p.stem, path=p,
                        saved_at=float(meta.get("saved_at") or p.stat().st_mtime), meta=meta)
        )
    return sorted(out, key=lambda s: s.saved_at, reverse=True)


def drop_session(username: str, base: Path | None = None) -> bool:
    path = session_path(username, base)
    existed = path.exists()
    if existed:
        path.unlink()
    data = read_index(base)
    if data["sessions"].pop(username, None) is not None:
        write_index(data, base)
    return existed


def export_bundle(dst: str | Path, users: list[str] | None = None,
                  base: Path | None = None) -> int:
    """把（指定或全部）登录态打包成一个 json，方便备份/换机器。返回条数。"""
    index = read_index(base)["sessions"]
    picked = users or [s.username for s in list_sessions(base)]
    bundle = {"version": BUNDLE_VERSION, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "sessions": {}}
    for user in picked:
        state = load_state(user, base)
        if state is None:
            continue
        bundle["sessions"][user] = {"state": state, "meta": index.get(user, {})}
    Path(dst).write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(bundle["sessions"])


def import_bundle(src: str | Path, base: Path | None = None,
                  overwrite: bool = True) -> list[str]:
    """导入打包文件，返回实际写入的账号名。"""
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    sessions = data.get("sessions", data)
    if not isinstance(sessions, dict):
        raise ValueError("不是合法的会话打包文件")
    index = read_index(base)
    written: list[str] = []
    for user, item in sessions.items():
        state = item.get("state") if isinstance(item, dict) else None
        if state is None and isinstance(item, dict) and "cookies" in item:
            state = item  # 直接给一份 storage_state 也认
        if state is None:
            continue
        path = session_path(user, base)
        if path.exists() and not overwrite:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        meta = dict(item.get("meta", {})) if isinstance(item, dict) else {}
        meta.setdefault("saved_at", time.time())
        meta["file"] = path.name
        index["sessions"][user] = meta
        written.append(user)
    write_index(index, base)
    return written
