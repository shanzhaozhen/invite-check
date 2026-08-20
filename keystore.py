"""API Key 的正式存放处：`data/keys/<站点>.json`。

`logs/<站点>/results.log` 是**流水账**——一直追加、含失败原因和历史 key，适合追溯，不适合
当"现在手上有哪些 key"的数据源。所以取到 key 就立刻在这里按账号存一份最新状态，
界面和导出都读这里；日志照旧写，两边各司其职。

文件长这样：

    {
      "version": 1,
      "site": "site-b",
      "updated_at": "2026-08-17 03:10:00",
      "keys": {
        "user-a": {"key": "sk-...", "uid": 10001,
                        "created_at": "2026-08-16 20:20:17",
                        "note": "复用 分组=default 过期=永不 配额=无限",
                        "quota": 4067374, "used_quota": 38250000,
                        "quota_at": "2026-08-17 22:40:11"}
      }
    }

`quota` 是站点上的**剩余额度**（原始单位，500000 = $1），注册取 key / 签到成功之后顺手记一笔，
界面「运行」页和导出都显示它。`quota_at` 是这个数字什么时候拿的——额度会随着用 key 变化，
不去重新问站点就只能当"上次看到的值"。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from store import read_rows

VERSION = 1
QUOTA_PER_USD = 500_000  # 站点用额度单位计价，500000 = $1


def fmt_quota(quota) -> str:
    """把额度原始值写成 `$12.34`；拿不到就空字符串。"""
    try:
        return f"${float(quota) / QUOTA_PER_USD:.2f}"
    except (TypeError, ValueError):
        return ""


def load(path: str | os.PathLike) -> dict[str, dict]:
    """读某个站点的 key 表，返回 {账号: {"key":..., "uid":..., "created_at":..., "note":...}}。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    keys = data.get("keys", {}) if isinstance(data, dict) else {}
    out: dict[str, dict] = {}
    for user, item in (keys.items() if isinstance(keys, dict) else []):
        # 有 key 的当然要；只记了额度（还没取到 key）的也留着，界面要显示
        if isinstance(item, dict) and (item.get("key") or item.get("quota") is not None):
            out[str(user)] = dict(item)
        elif isinstance(item, str):  # 只存了 key 的简写形式
            out[str(user)] = {"key": item}
    return out


def save(path: str | os.PathLike, keys: dict[str, dict]) -> None:
    """写回（先写临时文件再替换，避免写坏）。"""
    p = Path(path)
    payload = {
        "version": VERSION,
        "site": p.stem,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keys": {u: keys[u] for u in sorted(keys)},
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def put(path: str | os.PathLike, user: str, key: str, uid=None, note: str = "",
        when: str = "") -> None:
    """记一个账号的 key（同一个账号重新取 key 就覆盖，历史在日志里）。"""
    if not key:
        return
    keys = load(path)
    entry = keys.get(user, {})
    entry["key"] = key
    entry["created_at"] = when or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if uid not in (None, ""):
        entry["uid"] = uid
    if note:
        entry["note"] = note
    keys[user] = entry
    save(path, keys)


def get(path: str | os.PathLike, user: str) -> dict:
    return load(path).get(user, {})


def set_quota(path: str | os.PathLike, user: str, quota, used=None, uid=None) -> None:
    """记一笔站点上的剩余额度（注册取 key / 签到之后顺手调，没有 key 的账号也能记）。"""
    if quota in (None, ""):
        return
    keys = load(path)
    entry = keys.get(user, {})
    entry["quota"] = quota
    entry["quota_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if used not in (None, ""):
        entry["used_quota"] = used
    if uid not in (None, ""):
        entry["uid"] = uid
    keys[user] = entry
    save(path, keys)


def quota_map(path: str | os.PathLike) -> dict[str, str]:
    """{账号: "$12.34"}，界面直接拿去填表。"""
    return {u: fmt_quota(v.get("quota")) for u, v in load(path).items()
            if v.get("quota") is not None}


def drop(path: str | os.PathLike, users: list[str]) -> int:
    """删掉几个账号的记录（比如 key 已作废），返回删了几条。"""
    keys = load(path)
    gone = [u for u in users if keys.pop(u, None) is not None]
    if gone:
        save(path, keys)
    return len(gone)


def _uid_from_note(note: str):
    """日志备注里形如 `uid=10001 inviter_id=0 ...`，把 uid 抠出来。"""
    for part in note.split():
        if part.startswith("uid="):
            val = part[4:]
            return int(val) if val.isdigit() else val
    return None


def sync_from_log(results_path: str | os.PathLike, keys_path: str | os.PathLike) -> int:
    """拿注册日志补齐 key 表：日志里每个账号最新的一条 ok+key，表里没有或不一样就写进去。

    老版本只往日志里写，所以第一次跑会把历史 key 全部收进来；之后每次启动跑一遍也不亏
    （纯读文本），能兜住"手工改了日志"这种情况。返回新增/更新了几条。
    """
    latest: dict[str, dict] = {}
    for row in read_rows(results_path):
        if row["status"] == "ok" and row["key"]:
            latest[row["user"]] = {"key": row["key"], "created_at": row["time"],
                                   "note": row["note"], "uid": _uid_from_note(row["note"])}
    if not latest:
        return 0
    keys = load(keys_path)
    changed = 0
    for user, item in latest.items():
        old = keys.get(user)
        if old and old.get("key") == item["key"]:
            continue
        entry = dict(old or {})
        entry.update({k: v for k, v in item.items() if v not in (None, "")})
        keys[user] = entry
        changed += 1
    if changed:
        save(keys_path, keys)
    return changed


def sync_all(site_objs, base=None) -> dict[str, int]:
    """给所有登记的站点跑一遍 sync_from_log，返回 {站点: 变动条数}。"""
    out: dict[str, int] = {}
    for s in site_objs:
        try:
            n = sync_from_log(s.results_path(base), s.keys_path(base))
        except OSError:
            n = 0
        if n:
            out[s.key] = n
    return out


def rows(path: str | os.PathLike,
         order: list[str] | None = None) -> list[tuple[str, str, str, str]]:
    """[(账号, key, 时间, 剩余额度文本)]，``order`` 给账号库顺序时按它排。"""
    keys = load(path)
    picked = [u for u in (order or keys) if u in keys]
    return [(u, keys[u].get("key", ""), keys[u].get("created_at", ""),
             fmt_quota(keys[u].get("quota"))) for u in picked]
