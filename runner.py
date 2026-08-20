"""邀请注册流程：GitHub 账号登录 → 授权 → 生成 API Key → 记录结果。

支持多个同构站点（new-api + 自研前端，见 sites.py），流程与站点无关：origin 和邀请码都从
``Settings.signup_url`` 推出来，登录态目录由 ``Settings.session_dir`` 指定。

选择器都写成"候选列表"的形式：站点或 GitHub 改版时，往下加一条候选即可，
不用改流程代码。
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from playwright.sync_api import Error as PWError
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout, sync_playwright

import fingerprint
import httpapi
import keystore
import tokenstore
from paths import APP_DIR
from store import Account, ResultStore, RunStats, read_rows
from session import (get_meta, has_session, list_sessions, load_state, record_meta,
                     save_session)
from sites import DEFAULT_SIGNUP_URL
from totp_util import fresh_totp

SIGNUP_URL = DEFAULT_SIGNUP_URL
LOGIN_PATH = "/sign-in"      # 注册过的账号走登录页，不用再带邀请码

Logger = Callable[[str], None]


@dataclass
class Settings:
    """一次运行的全部可调参数。"""

    signup_url: str = SIGNUP_URL
    timeout_ms: int = 30_000
    slow_mo_ms: int = 0
    key_name: str = "auto"
    reuse_key: bool = True  # 已有满足条件的 key 就直接取，不重复创建
    settle_ms: int = 1500  # 按钮可见之后再静置多久才点（等 React 挂事件），重试会逐轮加长
    retries: int = 1
    delay_between: float = 5.0
    skip_done: bool = True
    shot_dir: Path = field(default_factory=lambda: Path("shots"))
    proxy: str = ""
    channel: str = "auto"  # auto / chrome / msedge / chromium
    use_session: bool = True  # 有保存的登录态就直接用，跳过 GitHub 登录
    spoof_device: bool = True  # 每个账号一套固定但不同的设备指纹（分辨率/核数/内存/显卡）
    background: bool = True  # 后台跑：真实浏览器照旧，只是窗口挪到屏幕外，不占屏幕
    site_password: str = ""  # 要给站点账号设的登录密码（--set-password）
    checkin_token: str = ""  # 现成的 Turnstile token（--checkin-token，从可信浏览器里抓的）
    current_password: str = ""  # 站点上现有的密码（改密码时必须带；OAuth 号没有）
    verify_password: bool = False  # 设完密码后试着用密码登一次（站点登录页有 Turnstile，多半过不了）
    session_dir: Path | None = None
    keys_path: Path | None = None  # 取到的 key 存哪（data/keys/<站点>.json）
    tokens_path: Path | None = None  # 站点访问令牌存哪（data/tokens/<站点>.json）
    site_key: str = ""  # 站点登记表里的 key（签到方式记回 sites.json 时要用）
    checkin_method: str = ""  # 先用哪种签到方式（api/token/ui；空 = 按顺序试探）
    concurrency: int = 1  # 同时跑几个账号（1 = 顺序；每个账号一个独立浏览器，上限 8）
    stop_flag: Callable[[], bool] = lambda: False
    pause_flag: Callable[[], bool] = lambda: False


# --------------------------------------------------------------------------
# 选择器候选表
# --------------------------------------------------------------------------

GITHUB_OAUTH_BUTTON = [
    "button:has-text('使用 GitHub 继续')",
    "button:has-text('Continue with GitHub')",
    "a[href*='oauth/github']",
    "a[href*='github.com/login/oauth']",
    "button:has-text('GitHub')",
    "[role='button']:has-text('GitHub')",
]

# 有些站点（site-c 那种新前端）在注册/登录页放了「我已阅读并同意 用户协议」，**不勾上 GitHub
# 按钮就是 disabled**，点了毫无反应。这类组件（base-ui / shadcn 那套）真正的
# `<input type=checkbox>` 常常是 1×1 隐藏的，看得见能点的是旁边那个 16×16 的
# `span[role=checkbox][aria-checked]`，所以顺序是"先点看得见的控件，不成再去点隐藏的 input"。
# ⚠ 别去点「我已阅读并同意」那行文字——里面嵌着「用户协议」的链接，点上去会跳走。
AGREE_CLICK = [
    "[role='checkbox'][aria-checked='false']",
    "span[aria-checked='false']",
    "button[aria-checked='false']",
    "[class*='checkbox' i][aria-checked='false']",
    "input[type='checkbox'][id*='consent' i]",
    "input[type='checkbox'][id*='agree' i]",
]

GH_USERNAME = ["#login_field", "input[name='login']"]
GH_PASSWORD = ["#password", "input[name='password']"]
GH_SUBMIT = ["input[type='submit'][name='commit']", "button[type='submit']"]
GH_TOTP = ["#app_totp", "#totp", "input[name='app_otp']", "input[name='otp']"]
GH_TOTP_SUBMIT = ["button[type='submit']", "input[type='submit']"]
GH_AUTHORIZE = [
    "button[name='authorize'][value='1']",
    "button:has-text('Authorize')",
    "input[name='authorize'][value='1']",
]
GH_USE_TOTP_LINK = [
    "a:has-text('Use your authenticator app')",
    "a:has-text('authenticator app')",
    "button:has-text('authenticator app')",
]

# GitHub 反滥用系统把账号 flag 了之后，密码和 2FA 都能过，但授权页会直接拒绝：
# 「This account is flagged, and therefore cannot authorize a third party application.」
# 这种只能人工去 GitHub 申诉，重试纯属浪费（还会在 GitHub 那边多留失败记录）。
GH_FLAGGED_RE = re.compile(
    r"account is flagged|cannot authorize a third[- ]party|账[号戶]被标记", re.I
)

# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------


def _first_visible(page: Page, selectors: Sequence[str], timeout: float = 8.0) -> Locator | None:
    """轮询候选选择器，返回第一个真正可见的元素。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    return loc
            except PWError:
                continue
        page.wait_for_timeout(250)
    return None


def click_first(page: Page, selectors: Sequence[str], timeout: float = 8.0) -> bool:
    """点第一个可见的候选元素；找不到就返回 False 交给调用方决定。"""
    loc = _first_visible(page, selectors, timeout)
    if loc is None:
        return False
    try:
        loc.click()
        return True
    except PWError:
        try:  # 被遮挡时强制点击兜底
            loc.click(force=True)
            return True
        except PWError:
            return False


def fill_first(page: Page, selectors: Sequence[str], value: str, timeout: float = 8.0) -> bool:
    loc = _first_visible(page, selectors, timeout)
    if loc is None:
        return False
    loc.click()
    loc.fill("")
    loc.press_sequentially(value, delay=40)  # 逐字输入，规避简单的粘贴检测
    return True


class FlowError(RuntimeError):
    """流程里可预期的失败，带上人类可读的原因。"""


class FatalFlowError(FlowError):
    """这个账号本身有问题，重试也没用（比如 GitHub 账号被 flag），直接记失败不再重试。"""


class Stopped(FlowError):
    """用户点了「停止」。不算失败、不重试，这个账号记一条 skip 就完事。

    各处的等待循环（拦截页、等令牌、等人点、账号之间的间隔…）都会隔几百毫秒查一次
    `st.stop_flag()`，所以按下停止之后一般一秒内就抛出来，不用等当前账号跑完。
    """


def check_stop(st: Settings) -> None:
    """在每个"等一会儿"的地方调一下：按了停止就抛 `Stopped`。"""
    if st.stop_flag():
        raise Stopped("已停止")


def sleep_unless_stopped(st: Settings, secs: float, step: float = 0.3) -> bool:
    """睡 `secs` 秒，但每 `step` 秒看一眼停止标志。返回"是不是被停止了"。"""
    deadline = time.monotonic() + max(0.0, secs)
    while time.monotonic() < deadline:
        if st.stop_flag():
            return True
        time.sleep(min(step, max(0.0, deadline - time.monotonic())))
    return st.stop_flag()


# --------------------------------------------------------------------------
# GitHub 登录
# --------------------------------------------------------------------------


def _github_error_text(page: Page) -> str:
    """抓取 GitHub 登录页的红色报错条。"""
    for sel in [".flash-error", "[role='alert']", ".js-flash-alert"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                return " ".join(loc.inner_text().split())[:200]
        except PWError:
            continue
    return ""


def github_login(page: Page, acct: Account, log: Logger) -> None:
    """账号密码直登，随后用本地算出的 TOTP 过两步验证。"""
    t0 = time.time()
    if "github.com/login" not in page.url:
        raise FlowError(f"不是 GitHub 登录页: {page.url[:120]}")
    if not fill_first(page, GH_USERNAME, acct.username, timeout=15):
        raise FlowError("GitHub 登录页没有出现用户名输入框")
    if not fill_first(page, GH_PASSWORD, acct.password):
        raise FlowError("GitHub 登录页没有出现密码输入框")
    if not click_first(page, GH_SUBMIT):
        raise FlowError("找不到 GitHub 登录提交按钮")
    log(f"  已提交 GitHub 账号密码（用了 {time.time() - t0:.1f} 秒）")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(800)

    err = _github_error_text(page)
    if err and re.search(r"incorrect|invalid|unable to (log|sign)", err, re.I):
        raise FlowError(f"GitHub 拒绝登录: {err}")

    url = page.url
    if "/sessions/verified-device" in url or "verify" in url.lower():
        raise FlowError("GitHub 要求邮箱设备验证码，需人工处理该账号")

    if "two-factor" in url or _first_visible(page, GH_TOTP, timeout=3) is not None:
        _submit_totp(page, acct, log)
    else:
        log("  未出现两步验证页，直接继续")
    log(f"  GitHub 这一段共 {time.time() - t0:.1f} 秒")


_LAST_TOTP: dict[str, str] = {}   # 账号 → 上次提交过的动态码


def _submit_totp(page: Page, acct: Account, log: Logger) -> None:
    """填写 6 位动态码；GitHub 现在多数是填完自动提交。

    ⚠ 同一个码**不能重复提交**（GitHub 回 "already been used or is too old"，实测重试时踩过）。
    所以记住上次用的那个，重复了就等到下一个 30 秒窗口再算一个。
    """
    click_first(page, GH_USE_TOTP_LINK, timeout=2)
    last = _LAST_TOTP.get(acct.username, "")
    code = fresh_totp(acct.totp_secret, min_validity=5, avoid=last)
    if last and code != last:
        log("  上次那个动态码已经用过了，换了一个新的")
    _LAST_TOTP[acct.username] = code
    log(f"  两步验证码 {code}")
    if not fill_first(page, GH_TOTP, code, timeout=10):
        raise FlowError("找不到两步验证输入框")
    page.wait_for_timeout(600)
    if _first_visible(page, GH_TOTP, timeout=1) is not None:
        click_first(page, GH_TOTP_SUBMIT, timeout=3)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(800)
    err = _github_error_text(page)
    if err and re.search(r"two-factor|otp|code", err, re.I):
        raise FlowError(f"两步验证失败: {err}")


# --------------------------------------------------------------------------
# 站点侧：进入邀请页 → 走 GitHub OAuth
# 站点是 new-api（响应头 x-new-api-version），所以登录态和令牌都能直接问它的接口
# --------------------------------------------------------------------------

_JS_SET_AFF = """
(aff) => {
  if (aff) { try { localStorage.setItem('aff', aff); } catch (e) {} }
  return localStorage.getItem('aff');
}
"""

# 登录态存在 HttpOnly cookie 里，JS 看不到。两种站点两套鉴权：
#   site-a：POST /api/user/auth/refresh 换 access_token，之后走 Authorization: Bearer
#   site-b（较新的 new-api）：没有 refresh 接口，直接用会话 cookie，但每个请求要带
#                               New-Api-User: <uid> 头（uid 存在 localStorage 里）
# 下面这个 refresh 在 site-b 上会 404，返回空串，改由 New-Api-User 兜底。
_JS_BEARER = """
async () => {
  try {
    const r = await fetch('/api/user/auth/refresh', {method: 'POST', credentials: 'include'});
    if (!r.ok) return '';
    const j = await r.json();
    return (j && j.data && j.data.access_token) || '';
  } catch (e) { return ''; }
}
"""

# 统一构造接口请求头：有 bearer 就带 Bearer；能从 localStorage 取到 uid 就带 New-Api-User。
# 两个都带最稳——各站点各取所需，多带的那个对方会忽略。
_JS_HEADERS_FN = """
  const _authHeaders = (bearer, json) => {
    const h = json ? {'Content-Type': 'application/json'} : {};
    if (bearer) h['Authorization'] = 'Bearer ' + bearer;
    try {
      let uid = localStorage.getItem('uid');
      if (!uid) { const u = JSON.parse(localStorage.getItem('user') || '{}'); uid = u && u.id; }
      if (uid) h['New-Api-User'] = String(uid);
    } catch (e) {}
    return h;
  };
"""

_JS_SELF = """
async (bearer) => {
""" + _JS_HEADERS_FN + """
  try {
    const r = await fetch('/api/user/self',
                          {credentials: 'include', headers: _authHeaders(bearer, false)});
    if (!r.ok) return null;
    const j = await r.json();
    return j && j.success ? j.data : null;
  } catch (e) { return null; }
}
"""


CF_WAIT_TITLES = ("稍候", "just a moment", "请稍等", "attention required")


def wait_cf_challenge(page: Page, st: Settings, log: Logger, tries: int = 3) -> None:
    """等 Cloudflare 托管拦截页放行；卡住就刷新重试。

    有头的真实 Chrome 基本秒过；无头很容易一直卡在这一页。
    等的过程中每秒看一眼「停止」有没有被按（按了就立刻抛 `Stopped`，别干等满 25 秒）。
    """
    for attempt in range(1, tries + 1):
        deadline = time.time() + 25
        while time.time() < deadline:
            check_stop(st)
            try:
                title = (page.title() or "").lower()
                has_ui = page.locator("button").count() > 0
            except PWError:
                title, has_ui = "", False
            blocked = any(t in title for t in CF_WAIT_TITLES)
            if not blocked and has_ui:
                if attempt > 1:
                    log("  Cloudflare 拦截页已通过")
                return
            page.wait_for_timeout(1000)
        log(f"  仍卡在 Cloudflare 拦截页，第 {attempt} 次刷新")
        try:
            page.reload(wait_until="domcontentloaded", timeout=st.timeout_ms)
        except PWError:
            pass
    raise FlowError("Cloudflare 拦截页没过去（本机 Chrome 那条路必须有头，别用无头）")


def _aff_code(url: str) -> str:
    m = re.search(r"[?&]aff(?:_code)?=([^&#]+)", url)
    return m.group(1) if m else ""


_JS_TURNSTILE = """
() => {
  try {
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
      const t = window.turnstile.getResponse();
      if (t) return t;
    }
  } catch (e) { /* 组件还没初始化 */ }
  const el = document.querySelector("[name='cf-turnstile-response']");
  return (el && el.value) || '';
}
"""

# 页面上有没有挂 Turnstile。注意这个站的 widget iframe 的 src 是空的（运行时才填），
# 所以不能只按 iframe[src*=challenges.cloudflare.com] 判断，否则会误判成"没有 Turnstile"。
_JS_HAS_TURNSTILE = """
() => {
  if (document.querySelector("[name='cf-turnstile-response'], .cf-turnstile, #cf-turnstile")) {
    return true;
  }
  if (document.querySelector("iframe[src*='challenges.cloudflare.com']")) return true;
  return !!(window.turnstile);
}
"""


def _click_turnstile_checkbox(page: Page, log: Logger) -> bool:
    """托管模式的 Turnstile 会要求点一下复选框，它在跨域 iframe 里。"""
    for fr in page.frames:
        if "challenges.cloudflare.com" not in (fr.url or ""):
            continue
        for sel in ("input[type='checkbox']", "#challenge-stage input", "label"):
            try:
                loc = fr.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=3000)
                    log("  点了 Turnstile 复选框")
                    return True
            except PWError:
                continue
    return False


def _wait_turnstile(page: Page, log: Logger, timeout: float = 45.0) -> bool:
    """等 Turnstile 出 token（前端拿它当放行条件）。页面没挂 Turnstile 就直接算就绪。"""
    try:
        if not page.evaluate(_JS_HAS_TURNSTILE):
            return True
    except PWError:
        return True
    deadline = time.time() + timeout
    logged = clicked = False
    while time.time() < deadline:
        try:
            if page.evaluate(_JS_TURNSTILE):
                log("  Turnstile 已通过")
                return True
        except PWError:
            pass
        if not logged:
            log("  等待 Turnstile 校验…")
            logged = True
        if not clicked and time.time() > deadline - timeout + 8:
            clicked = _click_turnstile_checkbox(page, log) or clicked
        page.wait_for_timeout(800)
    log("  Turnstile 超时未完成，仍然试着点按钮")
    return False


def _toast_text(page: Page) -> str:
    """抓一下页面上的提示条，失败原因经常写在这里。"""
    for sel in ("[data-sonner-toast]", "[role='status']", "[role='alert']", ".toast"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return " ".join(loc.inner_text().split())[:160]
        except PWError:
            continue
    return ""


# __SITE_PART_2__


_JS_OAUTH_URL = """
async (aff) => {
  const s = await fetch('/api/status', {credentials: 'include'})
                  .then(r => r.json()).catch(() => null);
  const cid = s && s.data && s.data.github_client_id;
  if (!cid) return {url: '', why: '拿不到 github_client_id'};
  const r = await fetch('/api/oauth/state', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({provider: 'github', intent: 'login', aff: aff || ''}),
  });
  const text = await r.text();
  let j = null;
  try { j = JSON.parse(text); } catch (e) { /* 不是 JSON */ }
  const state = (j && j.data && (j.data.state || j.data)) || (j && j.state);
  if (!state || typeof state !== 'string') {
    return {url: '', why: `state ${r.status}: ${text.slice(0, 120)}`};
  }
  return {url: 'https://github.com/login/oauth/authorize?client_id=' + cid +
               '&state=' + encodeURIComponent(state) + '&scope=user:email', why: ''};
}
"""


def start_oauth_directly(page: Page, st: Settings, log: Logger,
                         why_out: list | None = None) -> bool:
    """兜底：自己跟 /api/oauth/state 换个 state，拼出 GitHub 授权地址跳过去。

    和前端点按钮走的是同一条路（aff 也一起带上），只是不依赖按钮的事件绑定。
    ``why_out`` 给一个 list 的话，失败原因会塞进去（调用方拿它拼更准的报错，比如 429 限流）。
    """
    try:
        res = page.evaluate(_JS_OAUTH_URL, _aff_code(st.signup_url))
    except PWError as exc:
        log(f"  取 OAuth state 失败: {str(exc).splitlines()[0][:100]}")
        return False
    if not res or not res.get("url"):
        why = str((res or {}).get("why", "未知"))
        if why_out is not None:
            why_out.append(why)
        log(f"  直连 OAuth 不可用（{why}）")
        return False
    log("  改成直接跳 GitHub 授权地址")
    try:
        page.goto(res["url"], wait_until="domcontentloaded", timeout=st.timeout_ms)
        return True
    except PWError as exc:
        log(f"  跳转失败: {str(exc).splitlines()[0][:100]}")
        return False


def _github_or_logged_in(ctx, page: Page, timeout: float = 20.0) -> str:
    """点完 GitHub 按钮之后等结果：返回 "github"（跳过去了）/ "login"（站点自己就登进去了）/ ""。

    以前这里只等 GitHub 页面、死等 20 秒；可是站点常常在页面加载时用保存的 cookie 自己就
    登上了（site-a 的 refresh 尤其如此），那 20 秒纯属白等。所以两个条件一起盯。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if find_github_page(ctx, timeout=1) is not None:
            return "github"
        if check_login(page)[0]:
            return "login"
        page.wait_for_timeout(600)
    return ""


_JS_TERMS = """() => {
  const want = /已阅读|同意|agree|terms|条款|隐私/i;
  const near = (el) => { let t = '', p = el;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) t += ' ' + (p.innerText || '');
    return t; };
  const nodes = [...document.querySelectorAll(
      "input[type=checkbox],[role=checkbox],[aria-checked]")];
  let seen = 0, ticked = 0;
  for (const el of nodes) {
    if (nodes.length > 1 && !want.test(near(el))) continue;
    seen++;
    if (el.checked === true || el.getAttribute('aria-checked') === 'true') ticked++;
  }
  const btn = [...document.querySelectorAll('button,[role=button]')]
      .find(e => /github/i.test(e.textContent || ''));
  return {has: seen, ticked: ticked, disabled: btn ? !!btn.disabled : null};
}"""

# 兜底：直接点那个（可能 1×1 隐藏的）真 input。React / base-ui 监听的都是它，点它状态才同步
_JS_TICK_TERMS = """() => {
  const want = /已阅读|同意|agree|terms|条款|隐私/i;
  const near = (el) => { let t = '', p = el;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) t += ' ' + (p.innerText || '');
    return t; };
  const boxes = [...document.querySelectorAll('input[type=checkbox]')];
  for (const el of boxes) {
    if (boxes.length > 1 && !want.test(near(el))) continue;
    if (!el.checked) { el.click(); return true; }
  }
  return false;
}"""


def terms_state(page: Page) -> dict:
    """页面上的「我已阅读并同意」现状：`has` 几个、`ticked` 勾了几个、GitHub 按钮 `disabled`。"""
    try:
        return page.evaluate(_JS_TERMS) or {}
    except PWError:
        return {}


def accept_terms(page: Page, log: Logger) -> bool:
    """有「我已阅读并同意」就勾上——不勾的话 GitHub 按钮是 disabled，点了毫无反应。

    先用真实点击点看得见的那个控件（16×16 的 `span[role=checkbox]` 之类），不成再退回
    直接点隐藏的 `<input type=checkbox>`。返回"这次有没有勾上"（本来就勾着返回 False）。
    """
    state = terms_state(page)
    if not state.get("has") or state.get("ticked"):
        return False
    for sel in AGREE_CLICK:
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible():
                continue
            loc.click(timeout=3000)
        except PWError:
            continue
        if terms_state(page).get("ticked"):
            log("  勾上了「我已阅读并同意」")
            return True
    try:
        if page.evaluate(_JS_TICK_TERMS) and terms_state(page).get("ticked"):
            log("  勾上了「我已阅读并同意」（点的是隐藏的那个勾选框）")
            return True
    except PWError:
        pass
    log("  页面上有「我已阅读并同意」但没能勾上，GitHub 按钮可能是禁用的")
    return False


def open_invite_and_start_oauth(page: Page, st: Settings, log: Logger,
                                login_page: bool = False) -> str:
    """打开入口页，确保 aff 落进 localStorage，然后点进 GitHub 登录。

    返回 `"github"`（跳到 GitHub 了，调用方接着填账号密码）或 `"login"`（站点已经是登录态，
    不用碰 GitHub）——调用方靠它决定还要不要等 GitHub 页面，省掉一次 20 秒的空等。

    ``login_page=True`` 走 `/sign-in`：这个账号在本站点已经注册过了（keystore 里有 key 或者
    存过登录态），登录页更直接，也不用再带邀请码。

    前端是 React：按钮渲染出来不等于事件已挂上，点太早会毫无反应。所以等按钮**可见**之后
    静置 `settle_ms` 再点；点了没反应就原地多等几秒重试（每轮多等一点），别急着刷新。
    有「我已阅读并同意」的站点会先 `accept_terms` 勾上它（不勾按钮是 disabled，点了没用）。

    ⚠ 这里**故意不等**两样东西（实测各白等 15 / 20 秒，见 docs/anti-detection.md）：
    `networkidle`（站点有长连接，永远等到超时）和 Turnstile（CDP 驱动的浏览器拿不到 token，
    而 GitHub OAuth 这条路本来也不需要它）。
    """
    origin = site_origin(st.signup_url)
    url = origin + LOGIN_PATH if login_page else st.signup_url
    marker = LOGIN_PATH.strip("/") if login_page else "sign-up"
    t0 = time.time()
    log(f"  打开{'登录页' if login_page else '邀请页'} {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=st.timeout_ms)
    wait_cf_challenge(page, st, log)
    if not login_page:
        aff = _aff_code(st.signup_url)
        try:
            stored = page.evaluate(_JS_SET_AFF, aff)
            log(f"  邀请码 aff={stored or '(无)'}")
        except PWError:
            pass

    ctx = page.context
    host = origin.split("//", 1)[-1]
    oauth_why: list[str] = []          # 直连 OAuth 的失败原因，用来拼更准的报错
    # 页面加载时站点常常用保存的 cookie 自己就登上了（site-a 的 refresh 尤其如此），
    # 先问一句，省掉后面整套点击 + 等跳转
    if check_login(page)[0]:
        log(f"  入口页加载后已经是登录态，不用走 GitHub（共 {time.time() - t0:.1f} 秒）")
        return "login"
    for attempt in range(1, 5):
        if find_github_page(ctx, timeout=1) is not None:  # 上一次点击其实生效了，只是慢
            return "github"
        btn = _first_visible(page, GITHUB_OAUTH_BUTTON, timeout=12)
        if btn is None:
            if find_github_page(ctx, timeout=8) is not None:
                return "github"
            raise FlowError("入口页上没找到 GitHub 登录入口（站点可能改版）")
        page.wait_for_timeout(st.settle_ms * attempt)     # 给 React 挂事件的时间
        accept_terms(page, log)      # 有「我已阅读并同意」就勾上，不然按钮是 disabled
        try:
            btn.click(timeout=8000)
        except PWError:
            page.wait_for_timeout(1500)
            continue
        got = _github_or_logged_in(ctx, page, timeout=20)
        if got == "github":
            log(f"  已跳到 GitHub（入口页共用了 {time.time() - t0:.1f} 秒）")
            return "github"
        if got == "login":
            log(f"  站点直接就登上了，跳过 GitHub 登录（共 {time.time() - t0:.1f} 秒）")
            return "login"
        toast = _toast_text(page)
        log(f"  点击后暂未跳转，稍等再试（第 {attempt} 次）" + (f"：{toast}" if toast else ""))
        if attempt == 3 and start_oauth_directly(page, st, log, why_out=oauth_why):
            if find_github_page(ctx, timeout=15):
                return "github"
        if host and host in page.url and marker not in page.url:
            page.goto(url, wait_until="domcontentloaded", timeout=st.timeout_ms)
            wait_cf_challenge(page, st, log)
        page.wait_for_timeout(2500)
    if any("429" in w for w in oauth_why):
        raise FlowError(
            "站点在限流（/api/oauth/state 回 429）：GitHub 登录这条路暂时走不通。"
            "等十几分钟再跑，或者 --proxy 换个出口 IP；已经有登录态的账号不受影响"
        )
    state = terms_state(page)
    if state.get("has") and not state.get("ticked"):
        raise FlowError(
            "站点要求先勾「我已阅读并同意」，工具没能勾上（GitHub 按钮一直是禁用的）。"
            "站点改了那个勾选框的结构，往 runner.AGREE_CLICK 里加一条选择器即可；"
            "用 `python tools/probe_terms.py <站点>` 看它现在长什么样"
        )
    raise FlowError(
        "点了 GitHub 登录按钮但一直没跳转"
        + (
            "（前端未就绪，或当前 IP 请求太多被限流：等一会儿、换代理 --proxy 再试）"
        )
    )


def github_flagged(page: Page) -> bool:
    """这一页是不是 GitHub 的「账号被 flag，不能授权第三方应用」。"""
    try:
        return bool(GH_FLAGGED_RE.search(page.inner_text("body") or ""))
    except PWError:
        return False


def check_not_flagged(page: Page) -> None:
    """撞上被 flag 的账号就直接判死，别再重试。"""
    if github_flagged(page):
        raise FatalFlowError(
            "GitHub 账号被 flag，不能授权第三方应用（This account is flagged）。"
            "只能人工去 GitHub 申诉解封，重试没用，已跳过这个账号"
        )


def authorize_if_needed(page: Page, log: Logger) -> None:
    """首次授权时 GitHub 会多一页 Authorize 确认。"""
    try:
        if "/login/oauth/authorize" not in page.url:
            return
    except PWError:
        return
    check_not_flagged(page)
    if click_first(page, GH_AUTHORIZE, timeout=6):
        log("  已点击 Authorize 授权")
        page.wait_for_timeout(2500)


def _pages_on(ctx, needle: str) -> list[Page]:
    """当前 context 里 URL 命中 needle 的页面（OAuth 可能开在新标签页）。"""
    out = []
    for p in ctx.pages:
        try:
            if not p.is_closed() and needle in p.url:
                out.append(p)
        except PWError:
            continue
    return out


def find_github_page(ctx, timeout: float = 20.0) -> Page | None:
    """等出现 GitHub 登录/授权页，兼容同标签跳转和弹窗。

    只认 github.com/login，免得把 support.github.com 之类的页面当成登录页。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = _pages_on(ctx, "github.com/login")
        if hits:
            return hits[-1]
        time.sleep(0.5)
    return None


def site_origin(url: str) -> str:
    return re.match(r"^(https?://[^/]+)", url).group(1)


def get_site_page(ctx, origin: str, st: Settings, log: Logger = print) -> Page:
    """拿一个位于站点域下的页面；OAuth 结束后弹窗可能已经关掉，就新开一个。"""
    hits = _pages_on(ctx, origin)
    if hits:
        return hits[-1]
    page = ctx.new_page()
    page.goto(origin + "/keys", wait_until="domcontentloaded", timeout=st.timeout_ms)
    wait_cf_challenge(page, st, log)
    return page


def get_bearer(page: Page, tries: int = 1, gap: float = 1.5) -> str:
    """用 HttpOnly 的会话 cookie 换一个 access_token（site-a 那套）。

    refresh cookie 是一次性的，前端自己也会在页面加载时刷一次；两边撞上时先失败的那次
    拿不到 token，所以刚打开页面时多试几次。site-b 没有这个接口（404），会返回空串，
    改由 New-Api-User 头兜底，见 check_login。
    """
    for i in range(max(1, tries)):
        try:
            token = page.evaluate(_JS_BEARER) or ""
        except PWError:
            token = ""
        if token:
            return token
        if i + 1 < tries:
            page.wait_for_timeout(int(gap * 1000))
    return ""


def _self_info(page: Page, bearer: str) -> dict:
    """查 /api/user/self；用 bearer 和/或 New-Api-User 头。拿不到返回空 dict。"""
    try:
        return page.evaluate(_JS_SELF, bearer) or {}
    except PWError:
        return {}


def check_login(page: Page, tries: int = 1) -> tuple[bool, str, dict]:
    """判断当前页面是不是已登录，并给出调用接口用的凭据。

    返回 (是否登录, bearer, 用户信息)。两套站点都吃得下：
    - site-a：先 refresh 换到 bearer，self 才认；首次可能因一次性 cookie 竞争没换到，重试。
    - site-b：没有 refresh 接口，bearer 恒为空，靠 cookie + New-Api-User（uid 取自 localStorage）。
    """
    bearer = get_bearer(page, tries=1)
    info = _self_info(page, bearer)
    if info.get("id"):
        return True, bearer, info
    for _ in range(1, max(1, tries)):
        page.wait_for_timeout(1200)
        bearer = get_bearer(page, tries=1) or bearer
        info = _self_info(page, bearer)
        if info.get("id"):
            return True, bearer, info
    return False, bearer, info


def wait_logged_in(ctx, st: Settings, log: Logger, timeout: float = 90.0) -> tuple[Page, str, dict]:
    """等到确认登录为止，返回 (站点页面, bearer, 用户信息)。

    期间顺手点掉 GitHub 的 Authorize 页；撞上被 flag 的账号直接判死不再干等。
    inviter_id 可以用来确认邀请关系已绑定。
    """
    origin = site_origin(st.signup_url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_stop(st)                       # 最长要等 90 秒，别让「停止」干等
        for gh in _pages_on(ctx, "github.com/login/oauth/authorize"):
            authorize_if_needed(gh, log)
        for page in _pages_on(ctx, origin):
            ok, bearer, info = check_login(page)
            if ok:
                log(f"  登录成功: id={info.get('id')} user={info.get('username')}")
                return page, bearer, info
        for gh in _pages_on(ctx, "github.com"):
            check_not_flagged(gh)
        time.sleep(1.5)

    # 回调可能开在会自动关掉的窗口里，最后新开一页确认一次
    page = get_site_page(ctx, origin, st, log)
    ok, bearer, info = check_login(page, tries=3)
    if ok:
        log(f"  登录成功: id={info.get('id')} user={info.get('username')}")
        return page, bearer, info
    urls = [p.url for p in ctx.pages if not p.is_closed()]
    raise FlowError(f"OAuth 后仍未登录，当前页面: {urls}")


# __SITE_PART_4__


# --------------------------------------------------------------------------
# 生成 / 复用 API Key
# 接口链路（抓包确认，站点是 new-api + 自研前端）：
#   POST /api/user/auth/refresh   HttpOnly cookie → access_token
#   GET  /api/token/?p=1&size=100 列表，key 是打码的
#   POST /api/token/              建令牌，响应里不含 key
#   POST /api/token/{id}/key      取完整 key（不含 sk- 前缀，用时要自己加）
# 已经有满足条件（永不过期 + 无限配额 + 有分组 + 已启用）的令牌时直接取回来，不重复创建。
# --------------------------------------------------------------------------

TOKEN_LIST_PATH = "/api/token/?p=1&size=100"

_JS_TOKEN_FLOW = """
async ({bearer, name, prefix, create, reuse}) => {
  const H = {'Content-Type': 'application/json'};
  if (bearer) H['Authorization'] = 'Bearer ' + bearer;   // site-a 走这个
  try {                                                    // site-b 走 New-Api-User + cookie
    let uid = localStorage.getItem('uid');
    if (!uid) { const u = JSON.parse(localStorage.getItem('user') || '{}'); uid = u && u.id; }
    if (uid) H['New-Api-User'] = String(uid);
  } catch (e) {}
  const out = {steps: []};

  // 令牌必须挂在一个分组上，优先 default，没有就取第一个
  let group = 'default';
  try {
    const rg = await fetch('/api/user/self/groups', {credentials: 'include', headers: H});
    const jg = await rg.json();
    const names = Object.keys((jg && jg.data) || {});
    if (names.length && !names.includes('default')) group = names[0];
    out.groups = names;
  } catch (e) { /* 拿不到就按 default 提交 */ }
  out.group = group;

  const list = async () => {
    const r = await fetch('/api/token/?p=1&size=100', {credentials: 'include', headers: H});
    out.steps.push(['list', r.status, '']);
    const j = await r.json().catch(() => null);
    return (j && j.data && (j.data.items || j.data)) || [];
  };
  // 合格 = 永不过期 + 无限配额 + 有分组 + 状态启用
  const fits = t => t.expired_time === -1 && t.unlimited_quota === true
                    && !!t.group && (t.status === undefined || t.status === 1);

  let items = await list();
  out.count = items.length;
  let t = null;
  if (reuse) {
    const ok = items.filter(fits).sort((a, b) => (b.id || 0) - (a.id || 0));
    t = ok.find(x => (x.name || '').startsWith(prefix)) || ok[0] || null;
    if (t) out.reused = true;
  }
  if (!t && create) {
    const body = {name, group, expired_time: -1, unlimited_quota: true,
                  remain_quota: 0, model_limits_enabled: false, model_limits: '',
                  allow_ips: '', token_count: 1};
    const r = await fetch('/api/token/', {method: 'POST', credentials: 'include',
                                          headers: H, body: JSON.stringify(body)});
    out.steps.push(['create', r.status, (await r.text()).slice(0, 200)]);
    items = await list();
    t = items.find(x => x.name === name)
        || items.find(x => (x.name || '').startsWith(name))
        || (items.length ? items.reduce((a, b) => ((a.id || 0) > (b.id || 0) ? a : b)) : null);
  }
  if (!t) t = items.filter(fits).sort((a, b) => (b.id || 0) - (a.id || 0))[0] || null;
  if (!t) return out;
  out.picked = {id: t.id, name: t.name};

  // 复用的本来就合格；新建的再确认一遍"永不过期 + 无限配额 + 有分组"，不对就 PUT 改回来
  if (!fits(t)) {
    const fixed = Object.assign({}, t, {expired_time: -1, unlimited_quota: true,
                                        group: t.group || group});
    const rf = await fetch('/api/token/', {method: 'PUT', credentials: 'include',
                                           headers: H, body: JSON.stringify(fixed)});
    out.steps.push(['fix', rf.status, (await rf.text()).slice(0, 120)]);
    const rc = await fetch('/api/token/' + t.id, {credentials: 'include', headers: H});
    const jc = await rc.json().catch(() => null);
    if (jc && jc.data) t = jc.data;
  }
  out.settings = {expired_time: t.expired_time, unlimited_quota: t.unlimited_quota,
                  group: t.group};
  // 取完整 key 的接口会限流（实测 429），退避重试几次
  for (let i = 0; i < 3; i++) {
    const r3 = await fetch('/api/token/' + t.id + '/key',
                           {method: 'POST', credentials: 'include', headers: H});
    out.steps.push(['reveal', r3.status, '']);
    if (r3.status === 200) {
      const j = await r3.json().catch(() => null);
      out.key = (j && j.data && j.data.key) || '';
      if (out.key) return out;
    }
    if (r3.status !== 429) break;
    await new Promise(res => setTimeout(res, 4000));
  }
  return out;
}
"""


KEYS_PAGE = "/keys"
ADD_TOKEN_BTN = [
    "button:has-text('创建 API 密钥')",
    "button:has-text('添加令牌')",
    "button:has-text('新建令牌')",
    "button:has-text('Add Token')",
]
NAME_INPUT = ["[role='dialog'] input[name='name']", "input[placeholder='输入名称']", "#name"]
SUBMIT_BTN = [
    "[role='dialog'] button:has-text('保存更改')",
    "[role='dialog'] button:has-text('提交')",
    "[role='dialog'] button:has-text('确定')",
    "button:has-text('保存更改')",
]
GROUP_TRIGGER = [
    "[role='dialog'] button:has-text('选择一个分组')",
    "[role='dialog'] [role='combobox']",
    "button:has-text('选择一个分组')",
]
GROUP_OPTION = ["[role='option']:has-text('default')", "[role='option']"]
UNLIMITED_SWITCH = [
    "[role='dialog'] [role='switch']",
    "[role='dialog'] input[type='checkbox']",
]


def _pick_group(page: Page, log: Logger) -> None:
    """创建对话框里要选分组，优先 default，没有就取列表第一个。"""
    if not click_first(page, GROUP_TRIGGER, timeout=4):
        return
    page.wait_for_timeout(800)
    if not click_first(page, GROUP_OPTION, timeout=4):
        page.keyboard.press("Escape")
        log("  分组下拉打开了但没有可选项")
        return
    log("  已选择分组")
    page.wait_for_timeout(600)


def _ensure_unlimited(page: Page, log: Logger) -> None:
    """把「无限配额」开关拨到开。过期时间对话框默认就是永不过期，不动它。"""
    sw = _first_visible(page, UNLIMITED_SWITCH, timeout=3)
    if sw is None:
        return
    try:
        checked = sw.get_attribute("aria-checked") == "true" or sw.is_checked()
    except PWError:
        checked = False
    if not checked:
        try:
            sw.click()
            log("  已打开无限配额")
        except PWError:
            pass


def create_api_key(page: Page, bearer: str, st: Settings, log: Logger) -> tuple[str, str]:
    """拿一个合格的 API Key：已有满足条件的就直接取回来，没有才新建。

    合格 = 永不过期 + 无限配额 + 有分组 + 已启用。接口不通就退回界面点一遍再读。
    """
    name = f"{st.key_name}-{int(time.time())}"
    key, summary = _token_flow(page, bearer, name, log, create=True, reuse=st.reuse_key)
    if key:
        return key, summary
    log("  接口建不出来，改用界面操作")
    origin = site_origin(page.url)
    if _create_via_ui(page, st, name, origin, log):
        key, summary = _token_flow(page, bearer, name, log, create=False)
        if key:
            return key, summary
    raise FlowError("没能生成 API Key：接口和界面两种方式都失败")


def _token_flow(page: Page, bearer: str, name: str, log: Logger, create: bool,
                reuse: bool = False) -> tuple[str, str]:
    """列表→（复用合格的 / 没有就新建）→校正过期配额分组→取完整 key。返回 (key, 摘要)。"""
    try:
        res = page.evaluate(_JS_TOKEN_FLOW, {
            "bearer": bearer, "name": name, "prefix": name.split("-")[0],
            "create": create, "reuse": reuse,
        })
    except PWError as exc:
        log(f"  令牌接口异常: {str(exc).splitlines()[0][:120]}")
        return "", ""
    for step in res.get("steps", []):
        detail = f" {step[2]}" if len(step) > 2 and step[2] else ""
        log(f"  {step[0]} → {step[1]}{detail[:120]}")
    picked = res.get("picked") or {}
    cfg = res.get("settings") or {}
    origin_text = "复用" if res.get("reused") else "新建"
    summary = (
        f"{origin_text} 分组={cfg.get('group') or '(空)'} "
        f"过期={'永不' if cfg.get('expired_time') == -1 else cfg.get('expired_time')} "
        f"配额={'无限' if cfg.get('unlimited_quota') else '有限'}"
    )
    raw = res.get("key") or ""
    if raw and "*" not in raw:
        action = "复用已有令牌" if res.get("reused") else "新建令牌"
        log(f"  {action} id={picked.get('id')} name={picked.get('name')}  {summary}")
        if cfg.get("expired_time") != -1 or not cfg.get("unlimited_quota") or not cfg.get("group"):
            log("  ⚠ 过期时间/无限配额/分组 没能改成预期值，建议到站点上手动确认")
        return (raw if raw.startswith("sk-") else f"sk-{raw}"), summary
    return "", summary


def _create_via_ui(page: Page, st: Settings, name: str, origin: str, log: Logger) -> bool:
    """在 /keys 页面点"创建 API 密钥"，填名字后保存。只负责建，不负责读 key。"""
    try:
        page.goto(origin + KEYS_PAGE, wait_until="domcontentloaded", timeout=st.timeout_ms)
        wait_cf_challenge(page, st, log, tries=2)
    except (PWError, FlowError) as exc:
        log(f"  打开密钥页失败: {str(exc).splitlines()[0][:100]}")
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWError:
        pass
    page.wait_for_timeout(max(3000, st.settle_ms // 2))
    if not click_first(page, ADD_TOKEN_BTN, timeout=15):
        log("  没找到『创建 API 密钥』按钮")
        return False
    page.wait_for_timeout(1500)
    fill_first(page, NAME_INPUT, name, timeout=6)
    _pick_group(page, log)
    _ensure_unlimited(page, log)
    if not click_first(page, SUBMIT_BTN, timeout=6):
        log("  没找到保存按钮")
        return False
    page.wait_for_timeout(3500)
    log("  界面上已提交创建")
    return True


# --------------------------------------------------------------------------
# 每日签到
# 状态：GET  /api/user/checkin  → data.enabled / data.stats.checked_in_today / records
# 签到：POST /api/user/checkin  需要 Turnstile token，纯接口调用会被拒（"Turnstile token 为空"），
#      所以必须走界面：头像菜单 → 个人资料 → 点签到按钮
# --------------------------------------------------------------------------

QUOTA_PER_USD = 500_000  # 站点用额度单位计价，500000 = $1

_JS_CHECKIN_STATUS = """
async (bearer) => {
  const h = {};
  if (bearer) h['Authorization'] = 'Bearer ' + bearer;
  try {
    let uid = localStorage.getItem('uid');
    if (!uid) { const u = JSON.parse(localStorage.getItem('user') || '{}'); uid = u && u.id; }
    if (uid) h['New-Api-User'] = String(uid);
  } catch (e) {}
  const r = await fetch('/api/user/checkin', {credentials: 'include', headers: h});
  const j = await r.json().catch(() => null);
  if (!j || !j.data) return {http: r.status, ok: false};
  const d = j.data, s = d.stats || {};
  const day = new Date().toLocaleDateString('en-CA');  // yyyy-mm-dd，按本地时区
  const rec = (s.records || []).find(x => x.checkin_date === day);
  return {http: r.status, ok: true, enabled: !!d.enabled,
          checked_in_today: !!s.checked_in_today,
          count: s.checkin_count || 0, award: rec ? (rec.quota_awarded || 0) : 0, day};
}
"""

# --------------------------------------------------------------------------
# 给站点账号设一个登录密码（GitHub 登不了时的备用入口）
# OAuth 注册出来的号密码字段是空的，站点「更改密码」弹窗又要填「当前密码」，界面走不通；
# 但 new-api 的 PUT /api/user/self 原生语义是「password 非空就更新」，不校验原密码。
# 各家前端可能加了校验，所以下面按几种 body 依次试，把每次的状态码和响应都记下来。
# --------------------------------------------------------------------------

_JS_SET_PASSWORD = """
async ({bearer, username, display_name, password, original}) => {
  const H = {'Content-Type': 'application/json'};
  if (bearer) H['Authorization'] = 'Bearer ' + bearer;
  try {
    let uid = localStorage.getItem('uid');
    if (!uid) { const u = JSON.parse(localStorage.getItem('user') || '{}'); uid = u && u.id; }
    if (uid) H['New-Api-User'] = String(uid);
  } catch (e) {}

  const r = await fetch('/api/user/self', {
    method: 'PUT', credentials: 'include', headers: H,
    body: JSON.stringify({original_password: original || '', password}),
  });
  const text = await r.text();
  let j = null;
  try { j = JSON.parse(text); } catch (e) {}
  return {status: r.status, ok: !!(j && j.success === true),
          message: (j && j.message) || text.slice(0, 200)};
}
"""


def set_site_password(page: Page, bearer: str, info: dict, password: str,
                      log: Logger, original: str = "") -> str:
    """改站点登录密码。成功返回说明，失败抛 FlowError（带站点的原话）。

    走的就是站点自己那个「更改密码」用的接口：
    ``PUT /api/user/self`` body ``{original_password, password}``。

    实测（site-b rc.21 / site-a rc.23 都一样）：
    - ``original_password`` 填错 → 回「当前账号未设置密码，请使用密码重置或联系管理员重置密码」，
      说明 OAuth 注册的账号在库里根本没有密码，这个接口不给它设第一个密码
    - ``original_password`` 留空 → 回 ``success:true``，但那只是资料（用户名/显示名）更新成功，
      **密码并没有被设上**（拿它去登录会失败）。所以留空这条路是假成功，直接不让走
    """
    if not original:
        raise FatalFlowError(
            "站点改密必须带原密码：留空的话接口只更新资料、密码不会变（假成功）。"
            "OAuth 注册的账号站点上没有密码，得先绑邮箱走「忘记密码」拿到第一个密码，"
            "之后再用 --current-password 改"
        )
    try:
        res = page.evaluate(_JS_SET_PASSWORD, {
            "bearer": bearer,
            "username": info.get("username") or "",
            "display_name": info.get("display_name") or info.get("username") or "",
            "password": password,
            "original": original,
        })
    except PWError as exc:
        raise FlowError(f"改密接口异常: {str(exc).splitlines()[0][:120]}") from exc
    log(f"  PUT /api/user/self（改密）→ {res.get('status')} {res.get('message', '')[:120]}")
    if res.get("ok"):
        return "PUT /api/user/self"
    msg = res.get("message") or "(没有返回原因)"
    if "未设置密码" in msg:
        raise FatalFlowError(
            f"站点拒绝：{msg}。这个账号在站点上还没有密码（OAuth 注册的都是这样），"
            "只能先绑邮箱走「忘记密码」重置，或找管理员"
        )
    raise FlowError(f"改密失败：{msg}")


LOGIN_USER_INPUT = [
    "input[name='username']",
    "input[placeholder*='用户名']",
    "form input[type='text']",
]
LOGIN_PASS_INPUT = ["input[type='password']", "input[name='password']"]
LOGIN_SUBMIT = [
    "button[type='submit']",
    "button:has-text('登录')",
    "button:has-text('Sign in')",
]


def verify_password_login(browser, st: Settings, username: str, password: str,
                          log: Logger) -> bool:
    """开一个全新的浏览器上下文（不带任何登录态），用用户名+密码登一次，确认真能进。

    这一步才是"备用入口"到底管不管用的证据；成功后不保存这个上下文的登录态。
    """
    origin = site_origin(st.signup_url)
    ctx = new_context(browser, st, username="", log=log)  # 不带 session，等于全新设备
    try:
        page = ctx.new_page()
        page.goto(origin + "/sign-in", wait_until="domcontentloaded", timeout=st.timeout_ms)
        wait_cf_challenge(page, st, log, tries=2)
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PWError:
            pass
        _wait_turnstile(page, log, timeout=25)
        page.wait_for_timeout(1500)
        if not fill_first(page, LOGIN_USER_INPUT, username, timeout=10):
            log("  验证登录：找不到用户名输入框")
            return False
        if not fill_first(page, LOGIN_PASS_INPUT, password, timeout=6):
            log("  验证登录：找不到密码输入框")
            return False
        accept_terms(page, log)      # 有「我已阅读并同意」的站点不勾上，登录按钮也是禁用的
        if not click_first(page, LOGIN_SUBMIT, timeout=6):
            log("  验证登录：找不到登录按钮")
            return False
        page.wait_for_timeout(4000)
        ok = check_login(page, tries=4)[0]
        if not ok:
            toast = _toast_text(page)
            log(f"  验证登录失败{('：' + toast) if toast else ''}（当前页 {page.url[:60]}）")
        return ok
    finally:
        ctx.close()


PROFILE_MENU_ITEM = ["[role='menuitem']:has-text('个人资料')", "text=个人资料"]
CHECKIN_TEXT = re.compile(r"签到|打卡|领取")


def checkin_status(page: Page, bearer: str, log: Logger = print) -> dict:
    """查签到状态。拿不到就返回空 dict，由调用方决定怎么办。"""
    try:
        res = page.evaluate(_JS_CHECKIN_STATUS, bearer)
    except PWError as exc:
        log(f"  查签到状态失败: {str(exc).splitlines()[0][:100]}")
        return {}
    return res if res.get("ok") else {}


def open_profile(page: Page, st: Settings, log: Logger) -> bool:
    """从头像菜单进个人资料页。直接 goto /profile 会被路由守卫弹回 dashboard。

    ⚠ 这里 catch 的是 `Exception` 而不是 `PWError`：CloakBrowser 的拟人化层（humanize）点击前
    自己做可点性检查，抛的是它自己的异常类（比如 `ElementNotAttachedError`——React 重渲染时
    头像按钮被换掉就会撞上），不是 playwright 的 `Error`。实测这个异常会一路窜出去，
    把本来重试一下就好的账号记成失败。
    """
    for attempt in range(1, 4):
        try:
            page.locator("header button").last.click(timeout=6000)
            page.wait_for_timeout(1200)
            if click_first(page, PROFILE_MENU_ITEM, timeout=5):
                page.wait_for_timeout(3500)
                if "/profile" in page.url:
                    return True
        except Exception as exc:                          # noqa: BLE001
            log(f"  点头像/菜单没成（{type(exc).__name__}），再试一次")
        log(f"  没进到个人资料页，重试（第 {attempt} 次）")
        page.wait_for_timeout(1500)
    return "/profile" in page.url


def click_checkin(page: Page, log: Logger) -> bool:
    """点签到按钮。外层卡片和内层按钮都可能命中，从最里层往外试。

    同上：catch `Exception`——拟人化层的异常不是 `PWError`，漏掉会让整条流程白白失败。
    """
    btns = (
        page.locator("button, [role='button']")
        .filter(has_text=CHECKIN_TEXT)
        .filter(has_not_text="已签到")
    )
    try:
        total = btns.count()
    except Exception:                                     # noqa: BLE001
        return False
    for i in range(total - 1, -1, -1):
        b = btns.nth(i)
        try:
            if b.is_visible() and b.is_enabled():
                text = " ".join((b.inner_text() or "").split())[:30]
                b.click()
                log(f"  点了签到按钮「{text}」")
                return True
        except Exception:                                 # noqa: BLE001
            continue
    return False


def _award_text(quota: int) -> str:
    return f"+${quota / QUOTA_PER_USD:.2f}" if quota else ""


def _log_checkin_area(page: Page, log: Logger) -> None:
    """点不动时把签到区域的按钮和文字打出来，方便对着改选择器。"""
    try:
        found = page.evaluate(
            "() => [...document.querySelectorAll('button,[role=button]')]"
            ".map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim())"
            ".filter(t => /签到|打卡|领取/.test(t)).slice(0, 6)"
        )
        log(f"  页面上带「签到」字样的按钮: {found}")
    except PWError:
        pass


TURNSTILE_EMPTY = "Turnstile token 为空"


_JS_MINT_TOKEN = """
async (bearer) => {
""" + _JS_HEADERS_FN + """
  try {
    const r = await fetch('/api/user/token',
                          {credentials: 'include', headers: _authHeaders(bearer, false)});
    const text = await r.text();
    try {
      const j = JSON.parse(text);
      return {ok: j && j.success === true, token: String((j && j.data) || '').trim()};
    } catch (e) { return {ok: false, token: '', body: text.slice(0, 120)}; }
  } catch (e) { return {ok: false, token: '', body: String(e).slice(0, 120)}; }
}
"""


def mint_token_in_page(page: Page, bearer: str, st: Settings, username: str,
                       uid, log: Logger) -> str:
    """在**已登录的页面里**换一个站点访问令牌并存下来。

    比纯 HTTP 靠得住：页面里两套鉴权（Bearer / New-Api-User）都带得上，
    site-a 那种必须用 Bearer 的站点也能拿到。
    `GET /api/user/token` 是重新生成，所以只在还没有存过时调。
    """
    if not st.tokens_path or tokenstore.get(st.tokens_path, username):
        return ""
    try:
        res = page.evaluate(_JS_MINT_TOKEN, bearer)
    except PWError as exc:
        log(f"  生成访问令牌失败: {str(exc).splitlines()[0][:80]}")
        return ""
    token = (res or {}).get("token", "")
    if not token:
        log(f"  生成访问令牌没成: {(res or {}).get('body', '')[:80]}")
        return ""
    tokenstore.put(st.tokens_path, username, token, uid=uid)
    log("  已生成站点访问令牌（长期凭据，以后查状态/取 key 不用开浏览器）")
    return token


def ensure_access_token(st: Settings, username: str, log: Logger) -> str:
    """确保这个账号有一个站点访问令牌（长期凭据，替代会过期的 session）。

    已经有就直接返回；没有才调 `GET /api/user/token` 生成一个——那个接口是**重新生成**，
    会作废旧令牌，所以绝不能重复调。
    """
    if not st.tokens_path:
        return ""
    have = tokenstore.get(st.tokens_path, username)
    if have:
        return have
    client = httpapi.SiteClient(site_origin(st.signup_url), username,
                                st.session_dir, st.tokens_path)
    token = client.mint_token()
    log("  已生成站点访问令牌（以后查状态/取 key 不用再开浏览器）" if token
        else "  生成访问令牌没成（不影响本次结果）")
    return token


def _checkin_client(acct: Account, st: Settings):
    """这个账号在这个站点的免浏览器客户端（访问令牌优先，没有才用 session cookie）。"""
    return httpapi.SiteClient(site_origin(st.signup_url), acct.username,
                              st.session_dir, st.tokens_path)


def _method_order(st: Settings, need_turnstile: bool) -> list[str]:
    """这个账号该按什么顺序试签到方式。

    站点没开人机验证时"取令牌"那条没意义（直接接口签就行）；开了的话接口直签必然回
    「Turnstile token 为空」，所以也跳过——除非用 `--checkin-token` 手动喂了一个令牌。
    `st.checkin_method`（界面/`sites.json` 里配的，或上次成功的那种）排在最前面，
    但后面几种仍然留作兜底：万一那条今天正好抽风，不至于整批签不上。
    """
    order = ["token", "ui"] if need_turnstile else ["api", "ui"]
    if need_turnstile and st.checkin_token:
        order.insert(0, "api")
    first = st.checkin_method if st.checkin_method in ("api", "token", "ui") else ""
    if first:
        order = [first] + [m for m in order if m != first]
    return order


def _settle_checkin(acct: Account, st: Settings, client, log: Logger,
                    how: str) -> tuple[str, str] | None:
    """回查接口确认站点认没认。认了就记账并返回 (ok, 备注)，没认返回 None。

    站点写库和接口读到有时差（实测撞到过"提交说成功、回查说没签"），所以第一次说没签就再等
    2 秒问一遍——比让这个账号白白失败一次划算。
    """
    after = client.checkin_status() or {}
    if not after.get("checked_in_today"):
        if sleep_unless_stopped(st, 2.0):
            check_stop(st)
        after = client.checkin_status() or {}
    if not after.get("checked_in_today"):
        return None
    record_meta(acct.username, st.session_dir, last_checkin=after.get("day"))
    award = _award_text(after.get("award", 0))
    quota = record_quota(st, acct.username, client.self_info(), log)
    return "ok", (f"签到成功（累计 {after.get('count')} 天 {award}）"
                  + (f" 余额 {quota}" if quota else "") + f" {how}").strip()


def _try_api(acct: Account, st: Settings, client, log: Logger) -> tuple[bool, str]:
    """方式 api：直接打签到接口（站点没开人机验证时 1 秒搞定，也不开浏览器）。"""
    ok, msg = client.checkin(st.checkin_token)
    return ok, msg or ("提交成功" if ok else "站点没接受")


def _try_token(acct: Account, st: Settings, client, log: Logger) -> tuple[bool, str]:
    """方式 token：CloakBrowser 在承载页上取 Turnstile 令牌，并在那个页面里提交。"""
    import cloaksolve                                    # noqa: PLC0415

    usable, why = cloaksolve.available()
    if not usable:
        return False, f"用不了 CloakBrowser：{why}"
    return cloaksolve.checkin_by_token(acct, st, client, log)


def _try_ui(acct: Account, st: Settings, client, log: Logger) -> tuple[bool, str]:
    """方式 ui：CloakBrowser 走站点界面（头像 → 个人资料 → 立即签到 →（弹验证就点））。"""
    import cloakui                                       # noqa: PLC0415
    import cloaksolve                                    # noqa: PLC0415

    usable, why = cloaksolve.available()
    if not usable:
        return False, f"用不了 CloakBrowser：{why}"
    return cloaksolve.run_in_thread(cloakui.checkin_via_ui, acct, st, log)


METHOD_FN = {"api": _try_api, "token": _try_token, "ui": _try_ui}
METHOD_LABEL = {"api": "接口直签", "token": "CloakBrowser 取令牌",
                "ui": "CloakBrowser 走界面"}
# 这几种失败重试也是一样的结果，别浪费一轮（其余的都当"暂时的"，交给 _batch 重试）
NO_RETRY_HINTS = ("没有保存的登录态", "用不了 CloakBrowser", "站点没有开启每日签到")


def _worth_retrying(note: str) -> bool:
    """这条失败原因值不值得当场再来一次。

    实测（site-c 2026-08-21）：29 个失败的账号**全部**在后来的重跑里成功了——
    「登录态失效，进不去站点」（其实是拦截页/时序）、「走界面签到没成」、拟人化层的
    `ElementNotAttachedError`、网络抽风…都是暂时的。所以签到失败默认抛 `FlowError` 让
    `_batch` 按 `--retries` 当场重试，不用你手工把整批再跑几遍。
    """
    return not any(h in (note or "") for h in NO_RETRY_HINTS)


def checkin_one(acct: Account, st: Settings, log: Logger) -> tuple[str, str]:
    """一个账号的每日签到（自动）。返回 (状态, 备注)。

    状态：ok 本次签上 / done 今天已签过 / skip 站点没开签到 / fail 没搞定。

    顺序：先用接口查状态（不开浏览器，已签就直接跳过）→ 按 `_method_order` 依次试 →
    哪种成了就记回 `sites.json` 的 `last_ok_method`，下次这个站点直接从那条开始。
    """
    import sites                                         # noqa: PLC0415

    client = _checkin_client(acct, st)
    if not client.ready:
        return "fail", "没有保存的登录态，先跑一次注册/取 Key"
    stat = client.checkin_status()
    if stat:
        if not stat.get("enabled"):
            return "skip", "站点没有开启每日签到"
        if stat.get("checked_in_today"):
            record_meta(acct.username, st.session_dir, last_checkin=stat.get("day"))
            award = _award_text(stat.get("award", 0))
            quota = record_quota(st, acct.username, client.self_info(), log)
            return "done", (f"今天已签到（累计 {stat.get('count')} 天 {award}）"
                            + (f" 余额 {quota}" if quota else "") + " 没开浏览器").strip()
    else:
        log("  接口查不到状态（登录态可能过期），还是照着签一次试试")

    import cloaksolve                                    # noqa: PLC0415

    need, _sitekey = cloaksolve.site_turnstile(client.origin)
    order = _method_order(st, need)
    log(f"  站点人机验证：{'开着' if need else '没开'}，"
        f"签到方式依次试：{' → '.join(METHOD_LABEL[m] for m in order)}")
    last = ""
    for method in order:
        check_stop(st)                    # 按了停止就立刻抛出去，别再试下一种
        label = METHOD_LABEL[method]
        try:
            ok, note = METHOD_FN[method](acct, st, client, log)
        except Stopped:
            raise
        except Exception as exc:                          # noqa: BLE001
            ok, note = False, f"{type(exc).__name__} {str(exc).splitlines()[0][:90]}"
        last = f"{label}：{note}"
        if ok:
            settled = _settle_checkin(acct, st, client, log, label)
            if settled:
                sites.remember_ok_method(st.site_key, method)
                return settled
            last = f"{label}：提交完站点还是说没签到（{note}）"
        check_stop(st)                    # 上一种方式跑完了：先看要不要停，再换下一种
        log(f"  [--] {last}，换下一种")
    note = last or "所有签到方式都没成"
    if _worth_retrying(note):
        raise FlowError(note)             # 交给 _batch 按 --retries 当场重试
    return "fail", note


def _checkin_assist_task(get_pw, acct: Account, st: Settings,
                         log: Logger) -> tuple[str, str, str]:
    """协助签到：CloakBrowser 开一个能看见的窗口，先自动试，不成再等人点。"""
    import cloaksolve                                     # noqa: PLC0415
    import cloakui                                        # noqa: PLC0415

    usable, why = cloaksolve.available()
    if not usable:
        return "fail", "", f"用不了 CloakBrowser：{why}"
    # 等人点的时间要算进去，不然线程兜底超时会把还开着的窗口判死
    status, note = cloaksolve.run_in_thread(
        cloakui.assist_one, acct, st, log,
        _timeout=cloakui.ASSIST_WAIT + 240)
    log(f"  {'[OK]' if status in ('ok', 'done') else '[--]'} {note}")
    return status, "", note


# --------------------------------------------------------------------------
# 单账号 / 批量编排
# --------------------------------------------------------------------------

# 每个账号独立开一个浏览器、独立上下文（等于无痕），账号之间不串 cookie/缓存。
# UA 一律用本机真实浏览器的（不伪造版本，免得和 Sec-CH-UA 客户端提示对不上）；
# 设备参数（分辨率/核数/内存/显卡）按账号在 fingerprint.py 里做区分。
# 站点前面挂了 Cloudflare 托管拦截页，自动化特征太明显会一直卡在"请稍候…"
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# 后台模式：还是真实有头浏览器（无头过不了 Cloudflare），只是把窗口挪到屏幕外面去，
# 这样跑批量时不会一直有窗口抢焦点、挡着你干活。JS 里的窗口坐标由 fingerprint 修正。
BACKGROUND_ARGS = ["--window-position=-32000,-32000"]


CHROME_PATHS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
}


def pick_channel(want: str = "auto") -> str:
    """选浏览器：优先用本机装好的 Chrome / Edge，省掉下载内置 chromium 这一步。

    用真实浏览器跑，Cloudflare Turnstile 也更容易自动过。
    """
    if want and want != "auto":
        return "" if want == "chromium" else want
    for channel, paths in CHROME_PATHS.items():
        if any(Path(p).exists() for p in paths):
            return channel
    return ""


def _launch(pw, st: Settings, log: Logger, visible: bool = False):
    """按设置启动浏览器（注册取 Key 用本机 Chrome）；指定的 channel 起不来就退回内置 chromium。

    去掉 --enable-automation 这类特征位，Cloudflare 的拦截页才会放行。**一律有头**——无头过不了
    站点前面那个托管拦截页；"不占屏幕"用后台模式（窗口挪到屏幕外）解决。
    ``visible=True`` 表示这次要给人看，忽略后台模式。
    """
    hidden = st.background and not visible
    kw: dict = {
        "headless": False,
        "args": LAUNCH_ARGS + (BACKGROUND_ARGS if hidden else []),
        "ignore_default_args": ["--enable-automation"],
    }
    if hidden:
        log("  后台运行：浏览器窗口挪到屏幕外（想看过程就关掉「后台运行」/ 加 --no-background）")
    if st.slow_mo_ms:
        kw["slow_mo"] = st.slow_mo_ms
    if st.proxy:
        kw["proxy"] = {"server": st.proxy}
    channel = pick_channel(st.channel)
    if channel:
        try:
            return pw.chromium.launch(channel=channel, **kw)
        except PWError as exc:
            log(f"  {channel} 启动失败（{str(exc).splitlines()[0][:80]}），改用内置 chromium")
    try:
        return pw.chromium.launch(**kw)
    except PWError as exc:
        raise FlowError(
            "浏览器起不来：本机没找到 Chrome/Edge，内置 chromium 也没装。"
            "跑 `python -m playwright install chromium`，或用 --channel chrome 指定。"
            f"原始报错: {str(exc).splitlines()[0][:120]}"
        ) from exc


def new_context(browser, st: Settings, username: str = "", log: Logger = print,
                visible: bool = False):
    """建一个全新的（等于无痕）浏览器上下文；有保存的登录态就一起带上。

    每个账号一套固定的设备指纹（按账号名派生，同号每次一致、不同号各异），
    降低同机多号被判定为同一台机器的概率。见 fingerprint.py。
    """
    fp = fingerprint.for_user(username or "default") if st.spoof_device else None
    hidden = st.background and not visible
    kw: dict = {"locale": "zh-CN", "timezone_id": "Asia/Shanghai"}
    if fp:
        kw["viewport"] = fp["viewport"]
        kw["screen"] = fp["screen"]
        kw["device_scale_factor"] = fp["device_scale_factor"]
    else:
        kw["viewport"] = {"width": 1440, "height": 900}
    if username and st.use_session:
        state = load_state(username, st.session_dir)
        if state:
            kw["storage_state"] = state
    ctx = browser.new_context(**kw)
    ctx.set_default_timeout(st.timeout_ms)
    ctx.add_init_script(fingerprint.stealth_script(fp, background=hidden))
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    except PWError:
        pass
    return ctx


def persist_session(ctx, username: str, st: Settings, log: Logger, **meta) -> None:
    """存登录态。

    站点的 refresh cookie 是一次性的：每调一次 /api/user/auth/refresh 就换一个新的，
    旧的立刻失效。所以每次换到 token 都要马上存回去，否则进程一被杀，会话就废了。
    """
    try:
        save_session(ctx, username, st.session_dir, **meta)
    except Exception as exc:  # noqa: BLE001 - 存不下也不该影响主流程
        log(f"  登录态保存失败: {exc}")


def reuse_session(page: Page, st: Settings, username: str, log: Logger) -> bool:
    """带着保存的登录态直接进后台；还有效就不用再走 GitHub 了。"""
    if not st.use_session or not has_session(username, st.session_dir):
        return False
    origin = site_origin(st.signup_url)
    try:
        page.goto(origin + KEYS_PAGE, wait_until="domcontentloaded", timeout=st.timeout_ms)
        wait_cf_challenge(page, st, log, tries=2)
    except (PWError, FlowError) as exc:
        log(f"  复用登录态时打不开页面: {str(exc).splitlines()[0][:90]}")
        return False
    if check_login(page, tries=3)[0]:
        persist_session(page.context, username, st, log)  # 立刻把轮换后的 cookie 存回去
        log("  复用已保存的登录态，跳过 GitHub 登录")
        return True
    log("  保存的登录态已失效，改成重新登录")
    return False


def run_one(pw, acct: Account, st: Settings, log: Logger) -> tuple[str, str]:
    """跑完一个账号的完整流程，返回 (API Key, 备注)。每个账号独立浏览器，不串 cookie。

    每个大步骤之间查一次「停止」（`check_stop`）：按了就抛 `Stopped`，浏览器在 `finally`
    里关掉，这个账号记 skip，不用等整套 GitHub 登录跑完。
    """
    check_stop(st)
    browser = _launch(pw, st, log)
    try:
        ctx = new_context(browser, st, username=acct.username, log=log)
        page = ctx.new_page()
        try:
            if not reuse_session(page, st, acct.username, log):
                check_stop(st)
                # 注册过的账号（key 表里有 key / 存过登录态）走登录页，没注册过才进邀请页
                started = open_invite_and_start_oauth(
                    page, st, log, login_page=looks_registered(st, acct.username))
                gh = find_github_page(ctx, timeout=20) if started == "github" else None
                if gh is not None:
                    check_stop(st)
                    github_login(gh, acct, log)
                    authorize_if_needed(gh, log)
                elif started != "login":
                    log("  没跳到 GitHub（可能已有会话），继续检查登录态")
            site, bearer, info = wait_logged_in(ctx, st, log)
            persist_session(ctx, acct.username, st, log, uid=info.get("id"))
            check_stop(st)
            key, key_summary = create_api_key(site, bearer, st, log)
            if not key:
                raise FlowError("流程走完但没拿到 API Key")
            # 取完 key 再问一次 self，额度是最新的（建令牌本身不花额度，但顺序上更准）
            quota = record_quota(st, acct.username, _self_info(site, bearer) or info, log)
            note = (
                f"uid={info.get('id')} inviter_id={info.get('inviter_id', '?')} {key_summary}"
                + (f" 余额 {quota}" if quota else "")
            )
            if quota:
                log(f"  站点剩余额度 {quota}")
            mint_token_in_page(site, bearer, st, acct.username, info.get('id'), log)
            if st.keys_path:  # key 的正式存放处，别只留在日志里
                try:
                    keystore.put(st.keys_path, acct.username, key,
                                 uid=info.get("id"), note=key_summary)
                    log(f"  已记入 {Path(st.keys_path).name}")
                except OSError as exc:
                    log(f"  写 key 表失败（日志里还有）: {exc}")
            return key, note
        except Exception:
            if not st.stop_flag():        # 是被停的就不用留截图了
                _save_shot(ctx, acct, st, log)
            raise
        finally:
            ctx.close()
    finally:
        browser.close()


def open_account(
    username: str, st: Settings, log: Logger = print, acct: Account | None = None
) -> bool:
    """打开一个已登录到指定账号的浏览器窗口，窗口关掉才返回。

    有保存的登录态就直接用；没有或已失效就用 GitHub 账号密码 + 2FA 登一次，登完存起来。
    做补注册、改设置、手动签到之类的手工操作时用它。

    用的是 **CloakBrowser**（反检测 Chromium）：以前这里开的是普通自动化 Chrome，站点的人机
    验证在里面点了不算（复选框能画出来但永远不通过），所以只能干看着；换成 CloakBrowser 之后
    这个窗口里的验证是能过的，签到/密码登录都能手动干。
    """
    import cloaksolve                                    # noqa: PLC0415

    usable, why = cloaksolve.available()
    if not usable:
        log(f"用不了 CloakBrowser（{why}），窗口里的人机验证会点不过")
    origin = site_origin(st.signup_url)
    browser = cloaksolve.launch_browser(headless=False)
    state = load_state(username, st.session_dir) if st.use_session else None
    ctx, page = cloaksolve.new_page(browser, state)
    ok = False
    try:
        if state:
            page.goto(origin + KEYS_PAGE, wait_until="domcontentloaded",
                      timeout=st.timeout_ms)
            wait_cf_challenge(page, st, log, tries=2)
            ok = check_login(page, tries=3)[0]
            if ok:
                persist_session(ctx, username, st, log)
                log(f"已用保存的登录态进入 {username}")
            else:
                log("保存的登录态已失效")
        else:
            log("没有保存的登录态")

        if not ok and acct is not None:
            log(f"改用 GitHub 登录 {username} …")
            started = open_invite_and_start_oauth(
                page, st, log, login_page=looks_registered(st, username))
            gh = find_github_page(ctx, timeout=20) if started == "github" else None
            if gh is not None:
                github_login(gh, acct, log)
                authorize_if_needed(gh, log)
            page, _, info = wait_logged_in(ctx, st, log)
            persist_session(ctx, username, st, log, uid=info.get("id"))
            record_quota(st, username, info, log)
            ok = True

        if ok:
            try:  # 停在密钥页，方便接着手工操作
                page.goto(origin + KEYS_PAGE, wait_until="domcontentloaded",
                          timeout=st.timeout_ms)
            except PWError:
                pass
            log("窗口交给你了，关掉窗口即结束。这是 CloakBrowser，"
                "站点的人机验证在这里点得过（签到、密码登录都能手动做）")
        else:
            log("没有登录态、也没有账号密码，登不进去")
        while browser.is_connected() and ctx.pages and not st.stop_flag():
            time.sleep(1)
    except (PWError, FlowError) as exc:
        log(f"打开失败: {str(exc).splitlines()[0][:120]}")
    finally:
        for closer in (ctx.close, browser.close):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
    return ok


# 老名字留个别名，免得外面调用处报错
browse_session = open_account


def _save_shot(ctx, acct: Account, st: Settings, log: Logger) -> None:
    """失败时给每个还活着的页面留一张截图，方便回头判断卡在哪一步。"""
    try:
        st.shot_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        for i, page in enumerate(p for p in ctx.pages if not p.is_closed()):
            path = st.shot_dir / f"{acct.username}-{stamp}-{i}.png"
            page.screenshot(path=str(path), full_page=True)
            log(f"  失败截图: {path}")
    except Exception:  # noqa: BLE001 - 截图失败不能影响主流程
        pass


def _wait_if_paused(st: Settings, log: Logger) -> None:
    """暂停时卡在这里，直到取消暂停或收到停止。"""
    if not st.pause_flag() or st.stop_flag():
        return
    log("[暂停] 等待继续…")
    while st.pause_flag() and not st.stop_flag():
        time.sleep(0.5)
    if not st.stop_flag():
        log("[继续]")


# 任务收的第一个参数是"要浏览器时才调的回调"，这样纯接口能搞定的账号连驱动都不启
TaskFn = Callable[[Callable[[], object], Account, Settings, Logger], tuple[str, str, str]]


def _register_task(get_pw, acct: Account, st: Settings, log: Logger) -> tuple[str, str, str]:
    key, note = run_one(get_pw(), acct, st, log)
    log(f"  [OK] API Key: {key}")
    return "ok", key, note


def _checkin_task(get_pw, acct: Account, st: Settings, log: Logger) -> tuple[str, str, str]:
    """自动签到（不需要 `get_pw`：三条路都不用本机 Chrome 那套 playwright 实例）。"""
    status, note = checkin_one(acct, st, log)
    log(f"  {'[OK]' if status in ('ok', 'done') else '[--]'} {note}")
    return status, "", note


def password_one(pw, acct: Account, st: Settings, log: Logger) -> tuple[str, str]:
    """给一个账号改站点登录密码。返回 (状态, 备注)。

    走站点自己的改密接口，**要带原密码**（``Settings.current_password``，不给就用账号库里
    存的 ``site_password``）。OAuth 注册的账号从来没有密码，站点会直接拒绝——
    这种情况记一条 fail 并不再重试，得先绑邮箱走「忘记密码」才能有第一个密码。
    """
    password = st.site_password
    if not password:
        return "skip", "没给密码（--set-password）"
    original = st.current_password or acct.site_password
    browser = _launch(pw, st, log)
    try:
        ctx = new_context(browser, st, username=acct.username, log=log)
        try:
            page = ctx.new_page()
            if not reuse_session(page, st, acct.username, log):
                started = open_invite_and_start_oauth(page, st, log, login_page=True)
                gh = find_github_page(ctx, timeout=20) if started == "github" else None
                if gh is not None:
                    github_login(gh, acct, log)
                    authorize_if_needed(gh, log)
            site, bearer, info = wait_logged_in(ctx, st, log)
            persist_session(ctx, acct.username, st, log, uid=info.get("id"))
            record_quota(st, acct.username, info, log)
            how = set_site_password(site, bearer, info, password, log, original=original)
            site_user = info.get("username") or acct.username
        except Exception:
            _save_shot(ctx, acct, st, log)
            raise
        finally:
            ctx.close()

        verified = None
        if st.verify_password:
            log("  用「用户名 + 密码」在干净浏览器里登一次验证…")
            verified = verify_password_login(browser, st, site_user, password, log)
        record_meta(acct.username, st.session_dir,
                    password_set=datetime.now().strftime("%Y-%m-%d"),
                    password_login_ok=verified)
        tail = ("" if verified is None else
                f"；密码登录验证：{'通过' if verified else '没通过（登录页 Turnstile 拦住了自动化）'}")
        return "ok", f"已改密码（{how}）；站点用户名 {site_user}{tail}"
    finally:
        browser.close()


def _password_task(get_pw, acct: Account, st: Settings, log: Logger) -> tuple[str, str, str]:
    status, note = password_one(get_pw(), acct, st, log)
    log(f"  {'[OK]' if status == 'ok' else '[--]'} {note}")
    return status, "", note


def _batch(
    accounts: Sequence[Account],
    st: Settings,
    store: ResultStore,
    task: TaskFn,
    skip: dict[str, str],
    log: Logger,
    on_result: Callable[[str, str, str, str], None] | None,
    on_progress: Callable[[int, int, str], None] | None,
) -> RunStats:
    """批量跑一个任务（注册取 key / 签到），逐条写结果。

    ``skip`` 是 {账号: 跳过原因}；命中的账号不跑，记一条 skip。
    ``pause_flag`` 置位时原地等待。``stop_flag`` 置位时**当前账号也会立刻中断**：各处的等待
    循环（拦截页、等令牌、等人点、账号之间的间隔…）每几百毫秒查一次标志，查到就抛 `Stopped`，
    那个账号记一条 `skip 被停止（中途）`，浏览器在 `finally` 里关掉。

    ``st.concurrency`` > 1 就开一个线程池同时跑几个账号：
    **每条线程各起一个 playwright 实例**（sync API 不能跨线程共用），写日志/回调/计数都在锁里；
    并发时每行日志前面加 `[账号]`，不然几个账号的输出混在一起看不出是谁的。
    """
    stats = RunStats()
    total = len(accounts)
    lock = threading.Lock()
    workers = max(1, min(int(st.concurrency or 1), 8))

    def emit(user: str, status: str, key: str, note: str) -> None:
        with lock:
            store.append(user, status, key, note)
            if on_result:
                on_result(user, status, key, note)

    def bump(status: str) -> None:
        with lock:
            stats.ok += 1 if status in ("ok", "done") else 0
            stats.failed += 1 if status == "fail" else 0
            stats.skipped += 1 if status == "skip" else 0

    def one(idx: int, acct: Account, get_pw, say: Logger) -> None:
        """跑一个账号（含重试和落盘）。顺序/并发两条路共用这一段。"""
        head = f"[{idx}/{total}] {acct.username}"
        if on_progress:
            with lock:
                on_progress(idx, total, acct.username)
        if acct.username in skip:
            reason = skip[acct.username]
            say(f"{head} {reason}，跳过")
            bump("skip")
            emit(acct.username, "skip", "", reason)
            return
        say(head)
        note, settled = "", False
        for attempt in range(1, st.retries + 1):
            try:
                status, key, note = task(get_pw, acct, st, say)
                bump(status)
                emit(acct.username, status, key, f"{note} 第{attempt}次尝试".strip())
                settled = True
                break
            except Stopped:                # 点了「停止」：不算失败、不重试
                say("  [停止] 这个账号中途停下了")
                bump("skip")
                emit(acct.username, "skip", "", "被停止（中途）")
                return
            except FatalFlowError as exc:  # 账号本身的问题，重试也是一样的结果
                note = f"{type(exc).__name__}: {exc}".split("\n")[0][:300]
                say(f"  [失败] {note}")
                break
            except (FlowError, PWTimeout, PWError) as exc:
                note = f"{type(exc).__name__}: {exc}".split("\n")[0][:300]
                say(f"  [失败] 第 {attempt} 次: {note}")
                if attempt < st.retries:
                    # 被站点限流（429）时马上重试必然还是 429，多等一会儿
                    gap = 30.0 if ("429" in note or "限流" in note) else 3.0
                    if gap > 3:
                        say(f"  站点在限流，等 {gap:.0f} 秒再试")
                    if sleep_unless_stopped(st, gap):
                        break
        if not settled:
            bump("fail")
            with lock:
                stats.errors.append(f"{acct.username}: {note}")
            emit(acct.username, "fail", "", note)

    if workers > 1:
        log(f"并发 {workers} 个账号一起跑（每个账号一个独立浏览器；日志前面的 [账号] 是它自己的）")
        _batch_parallel(accounts, st, one, workers, log)
    else:
        _batch_serial(accounts, st, one, total, log)

    log(f"完成：成功 {stats.ok} / 失败 {stats.failed} / 跳过 {stats.skipped}")
    return stats


def _batch_serial(accounts: Sequence[Account], st: Settings, one, total: int,
                  log: Logger) -> None:
    """顺序跑：一个 playwright 实例，全员跳过时根本不启动它（懒启动）。"""
    stack = ExitStack()
    pw = None

    def browser_driver():
        nonlocal pw
        if pw is None:
            pw = stack.enter_context(sync_playwright())
        return pw

    try:
        for idx, acct in enumerate(accounts, start=1):
            _wait_if_paused(st, log)
            if st.stop_flag():
                log(f"收到停止指令，剩余 {total - idx + 1} 个账号未处理（重跑即继续）")
                break
            one(idx, acct, browser_driver, log)
            if idx < total and st.delay_between:
                if sleep_unless_stopped(st, st.delay_between):
                    log(f"收到停止指令，剩余 {total - idx} 个账号未处理（重跑即继续）")
                    break
    finally:
        stack.close()


def _batch_parallel(accounts: Sequence[Account], st: Settings, one, workers: int,
                    log: Logger) -> None:
    """并发跑：一个待办队列 + N 条线程，每条线程自己一个 playwright 实例。"""
    import queue as _queue                                  # noqa: PLC0415

    pending: _queue.Queue = _queue.Queue()
    for idx, acct in enumerate(accounts, start=1):
        pending.put((idx, acct))

    def worker() -> None:
        stack = ExitStack()
        pw = None

        def browser_driver():
            nonlocal pw
            if pw is None:
                pw = stack.enter_context(sync_playwright())
            return pw

        try:
            while True:
                try:
                    idx, acct = pending.get_nowait()
                except _queue.Empty:
                    return
                _wait_if_paused(st, log)
                if st.stop_flag():
                    return
                one(idx, acct, browser_driver,
                    lambda msg, u=acct.username: log(f"[{u}] {msg}"))
                if st.delay_between and sleep_unless_stopped(st, st.delay_between):
                    return
        finally:
            stack.close()

    threads = [threading.Thread(target=worker, name=f"batch{i + 1}", daemon=True)
               for i in range(workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if st.stop_flag():
        log(f"收到停止指令，剩余 {pending.qsize()} 个账号未处理（重跑即继续）")


def registered_users(results_path, session_dir=None, keys_path=None) -> set[str]:
    """在某站点算"注册成功"的账号：key 表里有的、注册日志里有 ok+key 的，或已经存了登录态的
    （最后一条覆盖用『登录选中账号』手工注册、没写日志的情况）。"""
    users = {
        row["user"] for row in read_rows(results_path)
        if row["status"] == "ok" and row["key"]
    }
    if keys_path:
        users |= set(keystore.load(keys_path))
    for s in list_sessions(session_dir):
        users.add(s.username)
    return users


def looks_registered(st: Settings, username: str) -> bool:
    """这个账号在本站点注册过没有：key 表里有 key，或者存过登录态（哪怕已经失效）。

    注册过就直接走登录页（`LOGIN_PATH`），不用再绕邀请页，也不用重复带 aff。
    """
    if st.keys_path and keystore.get(st.keys_path, username).get("key"):
        return True
    return bool(get_meta(username, st.session_dir))


def record_quota(st: Settings, username: str, info: dict,
                 log: Logger = lambda _m: None) -> str:
    """把 `/api/user/self` 里的剩余额度记进 key 表，返回 `$8.13` 这样的文本（没有就空串）。

    额度是"上次看到的值"：注册取 key、签到成功之后各记一次，界面「运行」页和导出都读它。
    """
    if not st.keys_path or not isinstance(info, dict) or info.get("quota") is None:
        return ""
    try:
        keystore.set_quota(st.keys_path, username, info.get("quota"),
                           used=info.get("used_quota"), uid=info.get("id"))
    except OSError as exc:
        log(f"  记额度失败（不影响结果）: {exc}")
    return keystore.fmt_quota(info.get("quota"))


def run_set_password(
    accounts: Sequence[Account],
    st: Settings,
    store: ResultStore,
    register_results: str = "",
    log: Logger = print,
    on_result: Callable[[str, str, str, str], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> RunStats:
    """批量给站点账号设登录密码（GitHub 登不了时的备用入口）。

    没在这个站点注册成功的账号跳过——它压根没有站点账号可设。
    ``skip_done`` 打开时，本站点已经设过并验证通过的也跳过。
    """
    skip: dict[str, str] = {}
    if register_results:
        reg = registered_users(register_results, st.session_dir, st.keys_path)
        for a in accounts:
            if a.username not in reg:
                skip[a.username] = "未在本站点注册成功，没有站点账号可设密码"
    if st.skip_done:
        for s in list_sessions(st.session_dir):
            if s.meta.get("password_set"):
                skip.setdefault(s.username, f"已设过站点密码（{s.meta['password_set']}）")
    return _batch(accounts, st, store, _password_task, skip, log, on_result, on_progress)


def run_batch(
    accounts: Sequence[Account],
    st: Settings,
    store: ResultStore,
    log: Logger = print,
    on_result: Callable[[str, str, str, str], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> RunStats:
    """批量注册/登录并生成 API Key；已经成功过的账号默认跳过。"""
    skip = {u: "已存在成功记录" for u in store.done_usernames()} if st.skip_done else {}
    return _batch(accounts, st, store, _register_task, skip, log, on_result, on_progress)


def run_checkin(
    accounts: Sequence[Account],
    st: Settings,
    store: ResultStore,
    register_results: str = "",
    log: Logger = print,
    on_result: Callable[[str, str, str, str], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    assist: bool = False,
) -> RunStats:
    """批量每日签到。``assist=True`` 是协助模式（开可见窗口、必要时等人点）。

    ``register_results`` 给了注册日志路径时，**没在本站点注册成功**的账号直接跳过、不开浏览器
    （想补注册用『登录选中账号』/ ``--open``）。

    今天签过没有会自动判断，两个来源都算，命中就连浏览器都不起：
    签到日志里今天有 ok/done，或登录态索引里 ``last_checkin`` 是今天（日志被清掉也认）。
    真跑起来的话第一步还是用接口查一次 ``GET /api/user/checkin`` 兜底。
    """
    skip: dict[str, str] = {}
    if register_results:
        reg = registered_users(register_results, st.session_dir, st.keys_path)
        for a in accounts:
            if a.username not in reg:
                skip[a.username] = "未在本站点注册成功，先用『登录选中账号』注册"
    if st.skip_done:
        today = datetime.now().strftime("%Y-%m-%d")
        for row in read_rows(store.path):
            if row["time"].startswith(today) and row["status"] in ("ok", "done"):
                skip.setdefault(row["user"], "今天已签到（签到日志里有记录）")
        for s in list_sessions(st.session_dir):
            if s.last_checkin == today:
                skip.setdefault(s.username, "今天已签到（登录态里记着）")
    task = _checkin_task
    if assist:
        task = _checkin_assist_task
        log("协助模式：CloakBrowser 逐个开一个**能看见的**已登录窗口，先自己把能点的点了；"
            "实在不成才轮到你点「每日签到」（有人机验证就勾一下）。签上工具会自动关窗口继续下一个")
    else:
        log("自动签到：先接口直签（1 秒）→ 不行就 CloakBrowser 取令牌再调接口（约 14 秒）"
            "→ 还不行就 CloakBrowser 走站点界面（约 25 秒）。哪种成了就记进 sites.json，"
            "下次这个站点直接用那种。全程无头、不占屏幕鼠标")
    return _batch(accounts, st, store, task, skip, log, on_result, on_progress)
