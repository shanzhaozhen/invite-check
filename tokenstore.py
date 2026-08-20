"""站点访问令牌（access token）的存放处：`data/tokens/<站点>.json`。

new-api 每个用户可以生成一个**系统访问令牌**（个人资料页「访问令牌」那一栏，接口是
`GET /api/user/token`）。带上 `Authorization: <令牌>` + `New-Api-User: <uid>` 就能调
用户级接口，**完全不需要 cookie**。

为什么要存它：登录态（session cookie）会过期、还会被站点轮换，一失效就得重走一遍 GitHub
OAuth（开浏览器、填密码、算 2FA）。访问令牌是账号级的长期凭据，查签到状态、查/取 API Key
这些都能靠它免浏览器完成。

⚠ `GET /api/user/token` 是**重新生成**：调一次就把旧令牌作废。所以只在"还没存过"时调。
⚠ 令牌等同于账号凭据，这个文件和账号库一样要保管好。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

VERSION = 1


def load(path: str | os.PathLike) -> dict[str, dict]:
    """读某个站点的令牌表，返回 {账号: {"token":..., "uid":..., "created_at":...}}。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    items = data.get("tokens", {}) if isinstance(data, dict) else {}
    out: dict[str, dict] = {}
    for user, item in (items.items() if isinstance(items, dict) else []):
        if isinstance(item, dict) and item.get("token"):
            out[str(user)] = dict(item)
        elif isinstance(item, str) and item.strip():
            out[str(user)] = {"token": item.strip()}
    return out


def save(path: str | os.PathLike, tokens: dict[str, dict]) -> None:
    p = Path(path)
    payload = {"version": VERSION, "site": p.stem,
               "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "tokens": {u: tokens[u] for u in sorted(tokens)}}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get(path: str | os.PathLike, user: str) -> str:
    """这个账号的令牌，没有就返回空串。"""
    return str(load(path).get(user, {}).get("token", "") or "")


def put(path: str | os.PathLike, user: str, token: str, uid=None) -> None:
    token = (token or "").strip()          # 站点返回值尾部带空格，必须 strip
    if not token:
        return
    tokens = load(path)
    entry = tokens.get(user, {})
    entry["token"] = token
    entry["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if uid not in (None, ""):
        entry["uid"] = uid
    tokens[user] = entry
    save(path, tokens)


def drop(path: str | os.PathLike, users: list[str]) -> int:
    tokens = load(path)
    gone = [u for u in users if tokens.pop(u, None) is not None]
    if gone:
        save(path, tokens)
    return len(gone)


def count(path: str | os.PathLike) -> int:
    return len(load(path))
