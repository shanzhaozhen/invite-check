"""支持的邀请站点登记表。

多个站点用的是同一套后端（new-api + 自研前端），流程完全一样，区别只有域名和邀请码。
所以这里只登记「站点 = origin + aff」，再按站点分目录存数据：

- 账号库 `data/accounts.json` 是**共用**的（同一批 GitHub 账号可以在多个站点各注册一次）
- 每个站点自己一份：`logs/<站点>/`（注册·签到·改密日志 + 失败截图）、
  `sessions/<站点>/`（登录态）、`data/keys-meta/<站点>.json`（key 的人工标记）

登记表存在 `data/sites.json` 里，可以在界面「站点」页增删改，不用改代码；
文件不存在时用下面 `BUILTIN` 里内置的两个站点。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from paths import APP_DIR

CONFIG_NAME = "sites.json"
VERSION = 1


def origin_of(url: str) -> str:
    """取 URL 的 scheme://host[:port] 部分。"""
    m = re.match(r"^(https?://[^/]+)", url.strip())
    return m.group(1) if m else url.strip()


def host_of(url: str) -> str:
    return origin_of(url).split("//", 1)[-1]


def slug(host_or_url: str) -> str:
    """从域名取一个短标识：site-b.example → site-b，site-a.example → site-a。"""
    host = host_of(host_or_url) if "//" in host_or_url else host_or_url
    labels = [x for x in host.split(":")[0].split(".") if x and x != "www"]
    key = (labels[0] if labels else host) or "site"
    return re.sub(r"[^0-9A-Za-z_-]", "-", key)


@dataclass(frozen=True)
class Site:
    """一个邀请站点。``key`` 用来给数据目录/文件分组，从域名推出来。

    ``default`` 只表示"界面和命令行默认选它"，不再影响文件名（每个站点都有自己的目录）。
    ``checkin_method`` 是这个站点的签到方式（见 `METHODS`）：``auto`` 表示按
    接口直签 → 取令牌 → 走界面 的顺序试，试成一次就记到 ``last_ok_method``，以后直接用那条。
    """

    key: str
    name: str
    signup_url: str
    default: bool = False
    enabled: bool = True
    checkin_method: str = "auto"
    last_ok_method: str = ""

    @property
    def origin(self) -> str:
        return origin_of(self.signup_url)

    @property
    def host(self) -> str:
        return host_of(self.signup_url)

    @property
    def aff(self) -> str:
        m = re.search(r"[?&]aff(?:_code)?=([^&#]+)", self.signup_url)
        return m.group(1) if m else ""

    def logs_dir(self, base: Path | None = None) -> Path:
        return Path(base or APP_DIR) / "logs" / self.key

    def results_path(self, base: Path | None = None) -> Path:
        return self.logs_dir(base) / "results.log"

    def checkin_path(self, base: Path | None = None) -> Path:
        return self.logs_dir(base) / "checkin.log"

    def password_log_path(self, base: Path | None = None) -> Path:
        """改站点密码的结果日志。"""
        return self.logs_dir(base) / "password.log"

    def shot_dir(self, base: Path | None = None) -> Path:
        """失败截图，按站点分开放。"""
        return self.logs_dir(base) / "shots"

    def sessions_dir(self, base: Path | None = None) -> Path:
        return Path(base or APP_DIR) / "data" / "sessions" / self.key

    def keymeta_path(self, base: Path | None = None) -> Path:
        return Path(base or APP_DIR) / "data" / "keys-meta" / f"{self.key}.json"

    def keys_path(self, base: Path | None = None) -> Path:
        """这个站点拿到的 API Key（正式数据，不是日志）。"""
        return Path(base or APP_DIR) / "data" / "keys" / f"{self.key}.json"

    def tokens_path(self, base: Path | None = None) -> Path:
        """站点访问令牌（长期认证凭据，替代会过期的 session）。"""
        return Path(base or APP_DIR) / "data" / "tokens" / f"{self.key}.json"

    def to_dict(self) -> dict:
        d = {"key": self.key, "name": self.name, "signup_url": self.signup_url,
             "enabled": self.enabled}
        if self.default:
            d["default"] = True
        if self.checkin_method and self.checkin_method != "auto":
            d["checkin_method"] = self.checkin_method
        if self.last_ok_method:
            d["last_ok_method"] = self.last_ok_method
        return d


# 签到方式：从便宜到贵。auto = 按这个顺序试，成了就记住
METHODS: dict[str, str] = {
    "auto": "自动（试探并记住）",
    "api": "接口直签（站点没开人机验证时最快，1 秒）",
    "token": "CloakBrowser 取令牌 + 接口签到（不碰站点页面，约 14 秒）",
    "ui": "CloakBrowser 走站点界面（头像→个人资料→立即签到，约 25 秒）",
}
METHOD_ORDER: list[str] = ["api", "token", "ui"]      # auto 模式的试探顺序


def normalize_method(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in METHODS else "auto"



# 登记表为空时的占位站点：让界面/命令行不至于崩，真要跑会提示"先加一个站点"。
# **代码里不内置任何真实站点**——打包出去的 exe 第一次跑就是一张空表，站点自己在界面里加。
PLACEHOLDER = Site(key="site", name="（还没有站点）", signup_url="", enabled=False)
BUILTIN: list[Site] = []


def config_path(base: Path | None = None) -> Path:
    return Path(base or APP_DIR) / "data" / CONFIG_NAME


def _from_dict(d: dict) -> Site | None:
    url = str(d.get("signup_url", "")).strip()
    if not url:
        return None
    key = str(d.get("key") or slug(url)).strip()
    last_ok = str(d.get("last_ok_method", "")).strip().lower()
    return Site(
        key=key,
        name=str(d.get("name") or key).strip(),
        signup_url=url,
        default=bool(d.get("default", False)),
        enabled=bool(d.get("enabled", True)),
        checkin_method=normalize_method(str(d.get("checkin_method", "auto"))),
        last_ok_method=last_ok if last_ok in METHOD_ORDER else "",
    )


def load(base: Path | None = None) -> list[Site]:
    """读登记表；文件不存在 / 坏了 / 是空表都返回空列表（调用方按"还没配站点"处理）。"""
    p = config_path(base)
    if not p.exists():
        return list(BUILTIN)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return list(BUILTIN)
    rows = data.get("sites", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return list(BUILTIN)
    return [s for s in (_from_dict(r) for r in rows if isinstance(r, dict)) if s]


def save(site_list: list[Site], base: Path | None = None) -> None:
    """写回登记表（原子替换）。"""
    p = config_path(base)
    payload = {"version": VERSION, "sites": [s.to_dict() for s in site_list]}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    reload(base)


SITES: list[Site] = load()


def _pick_default(site_list: list[Site]) -> Site:
    """默认站点：标了 default 的 → 第一个 → 一个都没有就用占位站点。"""
    return next((s for s in site_list if s.default),
                site_list[0] if site_list else PLACEHOLDER)


def reload(base: Path | None = None) -> list[Site]:
    """重新读一遍登记表，刷新模块级的 SITES / DEFAULT。"""
    global SITES, DEFAULT, DEFAULT_SIGNUP_URL
    SITES = load(base)
    DEFAULT = _pick_default(SITES)
    DEFAULT_SIGNUP_URL = DEFAULT.signup_url
    return SITES


DEFAULT: Site = _pick_default(SITES)
DEFAULT_SIGNUP_URL: str = DEFAULT.signup_url


def configured() -> bool:
    """登记表里到底有没有站点（界面/命令行在真要跑之前先问这个）。"""
    return bool(SITES)


def enabled() -> list[Site]:
    """启用的站点；有站点但一个都没启用时退回全部，免得什么都跑不了。空表就返回空列表。"""
    if not SITES:
        return []
    on = [s for s in SITES if s.enabled]
    return on or list(SITES)


def keys() -> list[str]:
    return [s.key for s in SITES]


def names() -> list[str]:
    return [s.name for s in SITES]


def enabled_names() -> list[str]:
    """启用站点的显示名（界面「运行」页的下拉框只列这些）。"""
    return [s.name for s in enabled()]


def first_enabled() -> Site:
    """默认选中的站点：默认站点启用就用它，否则第一个启用的；一个站点都没有就是占位站点。"""
    if DEFAULT.enabled:
        return DEFAULT
    return (enabled() or [DEFAULT])[0]


def by_key(key: str) -> Site:
    """按 key 或显示名找登记的站点，找不到抛 KeyError。"""
    for s in SITES:
        if key in (s.key, s.name):
            return s
    raise KeyError(key)


def for_url(url: str) -> Site:
    """按 origin 匹配登记的站点；没登记过就临时造一个（带后缀，不占用默认扁平文件）。"""
    origin = origin_of(url)
    for s in SITES:
        if s.origin == origin:
            return s
    return Site(key=slug(url), name=host_of(url), signup_url=url, default=False)


def resolve(site: str = "", url: str = "") -> Site:
    """定站点：给了 url 就按 url 的 origin 认（最具体）；否则按 site 名；都没给用默认。"""
    if url:
        return for_url(url)
    if site:
        return by_key(site)
    return DEFAULT


def make(name: str, signup_url: str, key: str = "", enabled_flag: bool = True) -> Site:
    """按输入造一个站点（key 不给就从域名推），供界面「站点」页新增用。"""
    k = (key or slug(signup_url)).strip()
    return Site(key=k, name=(name or k).strip(), signup_url=signup_url.strip(),
                default=False, enabled=enabled_flag)


def with_enabled(site: Site, flag: bool) -> Site:
    return replace(site, enabled=flag)


def with_method(site: Site, method: str) -> Site:
    return replace(site, checkin_method=normalize_method(method))


def safe_key(raw: str) -> str:
    """把人填的 key 洗成能当目录名的样子（字母数字 - _），洗完是空就返回空串。"""
    return re.sub(r"[^0-9A-Za-z_-]", "", (raw or "").strip())


def data_moves(site: Site, new_key: str, base: Path | None = None) -> list[tuple[Path, Path]]:
    """改 key 时要跟着搬的东西：[(源, 目标)]，只列**当前确实存在**的。

    这个站点的数据全按 key 命名：`logs/<key>/`（含日志和截图）、`data/sessions/<key>/`
    （登录态 + index.json）、`data/keys/<key>.json`、`data/keys-meta/<key>.json`、
    `data/tokens/<key>.json`。不搬的话新 key 下面什么都没有，等于这个站点从头开始。
    """
    new = replace(site, key=safe_key(new_key))
    pairs = [
        (site.logs_dir(base), new.logs_dir(base)),
        (site.sessions_dir(base), new.sessions_dir(base)),
        (site.keys_path(base), new.keys_path(base)),
        (site.keymeta_path(base), new.keymeta_path(base)),
        (site.tokens_path(base), new.tokens_path(base)),
    ]
    return [(src, dst) for src, dst in pairs if src.exists() and src != dst]


def move_data(site: Site, new_key: str, base: Path | None = None) -> tuple[list[str], list[str]]:
    """真去搬（调用方应先用 `data_moves` 检查目标存不存在）。返回 (搬成的, 失败的)。"""
    import shutil

    moved: list[str] = []
    failed: list[str] = []
    for src, dst in data_moves(site, new_key, base):
        if dst.exists():
            failed.append(f"{dst} 已存在，没动 {src}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(f"{src.name} → {dst}")
        except OSError as exc:
            failed.append(f"{src} → {dst}: {exc}")
    return moved, failed


def first_method(site: Site) -> str:
    """这个站点该先用哪种签到方式。

    界面/配置里指定了就用它；配成 auto 的话用上次成功的那种（`last_ok_method`）；
    两样都没有才返回空串，表示"从 `METHOD_ORDER` 从头试"。
    """
    if site.checkin_method in METHOD_ORDER:
        return site.checkin_method
    return site.last_ok_method if site.last_ok_method in METHOD_ORDER else ""


def remember_ok_method(key: str, method: str, base: Path | None = None) -> None:
    """把"这个站点用哪种方式签成了"记回 sites.json（同一种就不重复写盘）。"""
    if method not in METHOD_ORDER:
        return
    site_list = load(base)
    changed = False
    out = []
    for s in site_list:
        if s.key == key and s.last_ok_method != method:
            s = replace(s, last_ok_method=method)
            changed = True
        out.append(s)
    if changed:
        save(out, base)

