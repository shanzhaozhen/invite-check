"""不开浏览器的接口调用（urllib，标准库）。

站点的**查状态**和**签到**接口本来就只要 cookie + `New-Api-User` 头，不一定非得开浏览器。
所以先走这条快路：能查就查、能签就签；只有被 Cloudflare 挡住（Turnstile / 拦截页）或者
登录态过期时才回退到浏览器流程。

参考了 Newapi-checkin（github.com/Jasonliu-0/Newapi-checkin）的做法——它整个项目就是纯 HTTP
签到、跑在 GitHub Actions 上，不带浏览器；代价是遇到开了 Turnstile 的站点就没办法。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from session import get_meta, load_state
import tokenstore

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
TURNSTILE_HINT = "Turnstile"
# 站点说"这个访问令牌不认"时的原话（两个版本的站点措辞不一样）
TOKEN_DEAD_HINTS = ("invalid access token", "access token 无效", "access token无效")


def token_looks_dead(text: str) -> bool:
    """站点回话是不是"访问令牌无效"。实测令牌偶尔会被站点作废，得能认出来。"""
    low = (text or "").lower()
    return any(h in low for h in TOKEN_DEAD_HINTS)


def cookie_header(state: dict, host: str) -> str:
    """从 storage_state 里挑出这个域的 cookie，拼成 Cookie 头。"""
    parts = []
    for c in (state or {}).get("cookies", []):
        dom = str(c.get("domain", "")).lstrip(".")
        if dom and (dom in host or host in dom):
            parts.append(f"{c.get('name')}={c.get('value')}")
    return "; ".join(parts)


def _uid_from_state(state: dict, origin: str):
    """localStorage 里前端存的 uid（site-b 那套鉴权要它）。"""
    for o in (state or {}).get("origins", []):
        if o.get("origin") != origin:
            continue
        items = {i.get("name"): i.get("value") for i in o.get("localStorage", [])}
        if items.get("uid"):
            return items["uid"]
        try:
            return (json.loads(items.get("user") or "{}") or {}).get("id")
        except ValueError:
            return None
    return None


def call(origin: str, path: str, cookies: str, uid=None, method: str = "GET",
         timeout: int = 30, token: str = "") -> tuple[int, str]:
    """打一个接口，返回 (状态码, 响应文本)。网络层出错返回 (0, 原因)。

    ``token`` 给了就用访问令牌鉴权（`Authorization` + `New-Api-User`，不需要 cookie）。
    """
    req = urllib.request.Request(origin + path, method=method)
    if cookies:
        req.add_header("Cookie", cookies)
    if token:
        req.add_header("Authorization", token)
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    if uid not in (None, ""):
        req.add_header("New-Api-User", str(uid))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, str(exc)[:200]


class SiteClient:
    """一个站点 + 一个账号的免浏览器客户端。

    鉴权优先用**访问令牌**（长期有效，`tokens_path` 里存着的），没有才退回 session cookie。
    ``ready`` 为假说明两样都没有。
    """

    def __init__(self, origin: str, username: str, session_dir: Path | None,
                 tokens_path: Path | str | None = None):
        self.origin = origin.rstrip("/")
        self.host = self.origin.split("//", 1)[-1]
        self.username = username
        state = load_state(username, session_dir) or {}
        self.cookies = cookie_header(state, self.host)
        self.uid = _uid_from_state(state, self.origin) or get_meta(username,
                                                                  session_dir).get("uid")
        self.tokens_path = str(tokens_path) if tokens_path else ""
        self.token = tokenstore.get(self.tokens_path, username) if self.tokens_path else ""
        if self.token and not self.uid:
            self.uid = tokenstore.load(self.tokens_path).get(username, {}).get("uid")
        self.ready = bool(self.token and self.uid) or bool(self.cookies)

    @property
    def auth(self) -> str:
        return "访问令牌" if (self.token and self.uid) else "session cookie"

    def _json(self, path: str, method: str = "GET") -> tuple[int, dict | None, str]:
        # 有令牌就只用令牌（不带 cookie，免得 session 过期反而干扰）
        if self.token and self.uid:
            code, text = call(self.origin, path, "", self.uid, method, token=self.token)
            # 令牌被站点作废了（实测会发生）：扔掉它，用 cookie 换一张新的再重发
            if token_looks_dead(text):
                self.retire_token()
                if self.mint_token():
                    code, text = call(self.origin, path, "", self.uid, method,
                                      token=self.token)
                elif self.cookies:
                    code, text = call(self.origin, path, self.cookies, self.uid, method)
        else:
            code, text = call(self.origin, path, self.cookies, self.uid, method)
        try:
            return code, json.loads(text), text
        except ValueError:
            return code, None, text

    def retire_token(self) -> None:
        """把失效的令牌从存档里删掉（下次就会重新生成，不会一直拿着废票打）。"""
        if self.tokens_path and self.token:
            tokenstore.drop(self.tokens_path, [self.username])
        self.token = ""

    def self_info(self) -> dict:
        """`GET /api/user/self`，主要为了拿 `quota`（剩余额度）和 `used_quota`。"""
        code, data, _text = self._json("/api/user/self")
        if code != 200 or not isinstance(data, dict):
            return {}
        return data.get("data") or {}

    def mint_token(self) -> str:
        """用 session cookie 换一个长期访问令牌并存起来（会作废旧令牌，所以只在没有时调）。"""
        if not self.cookies:
            return ""
        code, text = call(self.origin, "/api/user/token", self.cookies, self.uid)
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        token = str((data or {}).get("data") or "").strip()
        if code != 200 or not token:
            return ""
        self.token = token
        if self.tokens_path:
            tokenstore.put(self.tokens_path, self.username, token, uid=self.uid)
        return token

    def checkin_status(self, month: str = "") -> dict:
        """和 runner.checkin_status 返回同样的形状；拿不到就返回 {}。

        ``month`` 给 ``YYYY-MM`` 时按站点前端的用法带上月份（拿那个月的签到日历）。
        """
        path = "/api/user/checkin" + (f"?month={month}" if month else "")
        code, data, _text = self._json(path)
        if code != 200 or not data or not data.get("data"):
            return {}
        d = data["data"]
        s = d.get("stats") or {}
        day = datetime.now().strftime("%Y-%m-%d")
        rec = next((r for r in (s.get("records") or [])
                    if r.get("checkin_date") == day), None)
        return {"http": code, "ok": True, "enabled": bool(d.get("enabled")),
                "checked_in_today": bool(s.get("checked_in_today")),
                "count": s.get("checkin_count") or 0,
                "award": (rec or {}).get("quota_awarded", 0), "day": day}

    def checkin(self, token: str = "") -> tuple[bool, str]:
        """POST 签到。返回 (成功没有, 站点原话)。

        ``token`` 是 Turnstile token，按站点前端的用法放在 **query 参数** 里
        （抓包确认：`POST /api/user/checkin?turnstile=<token>`，body 是空的）。
        没有 token 就不带——开了 turnstile_check 的站点会回「Turnstile token 为空」。
        """
        path = "/api/user/checkin"
        if token:
            path += "?turnstile=" + urllib.parse.quote(token, safe="")
        code, data, text = self._json(path, method="POST")
        if data is not None:
            return bool(data.get("success")), str(data.get("message") or "")[:160]
        return False, f"HTTP {code}: {text[:120]}"
