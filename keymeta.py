"""API Key 的人工标记：分组（待用/已用/签到累积/已作废）和使用备注。

跟账号库分开存在 `data/keys-meta/<站点>.json` 里，按账号名索引，重新生成 key 也不会丢。
调用方（export / gui）都会把站点对应的路径传进来，不传就用下面这个兜底路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from paths import DATA_DIR

GROUPS = ("待用", "已用", "签到累积", "已作废")
DEFAULT_GROUP = ""
META_PATH = DATA_DIR / "keys-meta" / "default.json"
VERSION = 1


def _path(path: str | Path | None = None) -> Path:
    return Path(path) if path else META_PATH


def load(path: str | Path | None = None) -> dict[str, dict]:
    """读全部标记，返回 {账号: {"group": ..., "note": ...}}。"""
    p = _path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    items = data.get("keys", data) if isinstance(data, dict) else {}
    out: dict[str, dict] = {}
    for user, meta in items.items() if isinstance(items, dict) else []:
        if isinstance(meta, dict):
            out[user] = {"group": str(meta.get("group", "")), "note": str(meta.get("note", ""))}
        elif isinstance(meta, str):  # 只写了分组的简写形式
            out[user] = {"group": meta, "note": ""}
    return out


def save(data: dict[str, dict], path: str | Path | None = None) -> None:
    """写回标记；空标记会被清掉，避免文件里堆垃圾。"""
    p = _path(path)
    clean = {
        user: {"group": meta.get("group", ""), "note": meta.get("note", "")}
        for user, meta in sorted(data.items())
        if meta.get("group") or meta.get("note")
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": VERSION, "keys": clean}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def get(user: str, path: str | Path | None = None) -> dict:
    return load(path).get(user, {"group": DEFAULT_GROUP, "note": ""})


def update(users: list[str], group: str | None = None, note: str | None = None,
           path: str | Path | None = None) -> int:
    """给一批账号设置分组/备注；``None`` 表示这一项不动。返回改了几条。"""
    data = load(path)
    for user in users:
        meta = data.setdefault(user, {"group": "", "note": ""})
        if group is not None:
            meta["group"] = group
        if note is not None:
            meta["note"] = note
    save(data, path)
    return len(users)
