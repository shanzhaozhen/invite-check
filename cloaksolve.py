"""CloakBrowser 这一层：起浏览器、灌登录态、点 Cloudflare 的复选框、承载页取 Turnstile 令牌。

**CloakBrowser 就是 Playwright**：`cloakbrowser.launch()` 内部是 `sync_playwright().start()` +
`pw.chromium.launch(executable_path=<打过补丁的 chrome.exe>)`，返回标准 Browser 对象，
`goto` / `route` / `evaluate` / `mouse.*` / `storage_state` 全是 Playwright API。它只多干三件事：
下那份二进制、拼反检测启动参数、把鼠标键盘换成拟人版（`humanize=True`）。
换它的理由：**普通 Chromium 拿不到这个站点的 Turnstile 令牌**，换内核就能（见 docs）。

这里提供两类东西：

1. 通用件：`available()` / `launch_browser()` / `new_page(storage_state=…)`、
   widget 的定位与点击（`wait_widget` / `click_widget` / `pass_cf_widget`）——`cloakui.py`
   走站点界面时也用它们
2. 签到方式 `token`（`checkin_by_token`）：

   - 在**站点 origin 下**开一个空白承载页（`page.route` 直接 fulfill，不加载站点 SPA）
     —— Turnstile 只校验 (sitekey, hostname)，hostname 对了令牌就有效
   - 自己 `turnstile.render()` 一个 widget、点复选框，等 Cloudflare 签发令牌
   - **在页面里** `fetch('/api/user/checkin?turnstile=…')` 提交 —— 站点后端校验时把 `remoteip`
     一起交给 siteverify，Python 那条连接的出口 IP 不一定和浏览器相同（IPv6/IPv4 优先级不同
     就够了），对不上就回「Turnstile 校验失败」

   所以这条路不加载站点前端、站点改版也不影响，只要账号有访问令牌
   （`data/tokens/<站点>.json`）或者 session cookie 就能签。

依赖（在 `requirements.txt` 里，但浏览器要单独下一次）：

    pip install cloakbrowser
    python -m cloakbrowser install     # 约 562MB 的 Chromium，缓存在 ~/.cloakbrowser

⚠ 只消费 Cloudflare 正常签发的令牌，不伪造、不改 hostname 骗域名绑定。
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpapi
import session
from store import Account

HOST_PATH = "/__checkin_turnstile__"        # 站点前端不会接管的路径
MIN_WIDGET_H = 50                           # widget 挂上之后约 65~74px
MOUNT_WAIT = 20.0                           # 等 widget 挂载（秒）
TOKEN_WAIT = float(os.environ.get("CLOAK_TOKEN_WAIT", 20))     # 点完等令牌（秒）
POLL = 0.4                                  # 轮询间隔：每次 evaluate 都是一条 CDP 命令，别太密
ATTEMPTS = int(os.environ.get("CLOAK_ATTEMPTS", 2))
FIRST_CLICK_AFTER = 3.0                     # 挂载后先让它自己试几秒（复选框那会儿才画出来）
CLICK_EVERY = 5.0                           # 没签发就每隔几秒补一下
MAX_CLICKS = 3
JOIN_TIMEOUT = 240.0                        # 求解线程的兜底上限（秒）

# 默认无头：不占屏幕、不抢鼠标。CLOAK_HEADFUL=1 改成带窗口（排查时看得见）
HEADLESS = os.environ.get("CLOAK_HEADFUL") != "1"


# 承载页的内容：空白页，标题随便给一个。故意什么都不加载。
CARRIER_HTML = ("<!doctype html><html><head><meta charset=utf-8><title>checkin</title>"
                "</head><body></body></html>")

# 在页面里注入的引导脚本：加载 api.js、explicit render，把令牌写进 host 的 data-token。
# 用 DOM 属性传值（而不是 return），这样谁在哪个世界读都一样。
BOOTSTRAP = r"""(() => {
  const SITEKEY = '__SITEKEY__';
  const host = document.createElement('div');
  host.id = 'ck-ts-host';
  host.setAttribute('data-state', 'init');
  host.style.cssText = 'position:fixed;left:24px;top:24px;width:320px;'
    + 'z-index:2147483647;background:#fff;padding:4px';
  const slot = document.createElement('div');
  slot.id = 'ck-ts-slot';
  host.appendChild(slot);
  (document.body || document.documentElement).appendChild(host);
  const render = () => {
    try {
      window.turnstile.render(slot, {
        sitekey: SITEKEY,
        callback: (t) => { host.setAttribute('data-token', t);
                           host.setAttribute('data-state', 'done'); },
        'error-callback': (c) => { host.setAttribute('data-state', 'error');
                                   host.setAttribute('data-error', String(c || 'unknown')); },
        'timeout-callback': () => host.setAttribute('data-state', 'timeout'),
      });
      host.setAttribute('data-state', 'rendered');
    } catch (e) {
      host.setAttribute('data-state', 'error');
      host.setAttribute('data-error', String((e && e.message) || e));
    }
  };
  if (window.turnstile && window.turnstile.render) { render(); return; }
  const s = document.createElement('script');
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.async = true;
  s.onload = () => { let n = 0; const w = setInterval(() => {
      if (window.turnstile && window.turnstile.render) { clearInterval(w); render(); }
      else if (++n > 100) { clearInterval(w); host.setAttribute('data-state', 'no-global'); }
    }, 100); };
  s.onerror = () => { host.setAttribute('data-state', 'error');
                      host.setAttribute('data-error', 'api.js load failed'); };
  (document.head || document.documentElement).appendChild(s);
})()"""

# 读状态：令牌两个来源都看（我们 callback 写的，和 Turnstile 自己填进 input 的）
STATE = """() => {
  const host = document.getElementById('ck-ts-host');
  const slot = document.getElementById('ck-ts-slot');
  const r = slot ? slot.getBoundingClientRect() : null;
  let token = (host && host.getAttribute('data-token')) || '';
  if (!token) {
    for (const f of document.querySelectorAll('input[name="cf-turnstile-response"],'
                                              + 'textarea[name="cf-turnstile-response"]')) {
      const v = typeof f.value === 'string' ? f.value : String(f.textContent || '');
      if (v.trim()) { token = v.trim(); break; }
    }
  }
  return {state: (host && host.getAttribute('data-state')) || 'missing',
          error: (host && host.getAttribute('data-error')) || '', token: token,
          slot: r ? {x: r.x, y: r.y, w: r.width, h: r.height} : null};
}"""

# 在**浏览器页面里**打签到接口（同源 fetch）。为什么必须在页面里发：见模块开头第 3 条。
# 鉴权用访问令牌（`Authorization` + `New-Api-User`）；没有令牌就靠灌进去的 cookie。
SUBMIT_IN_PAGE = """async ([token, uid, accessToken]) => {
  const url = '/api/user/checkin?turnstile=' + encodeURIComponent(token);
  const headers = {'Accept': 'application/json, text/plain, */*'};
  if (accessToken) headers['Authorization'] = accessToken;
  if (uid) headers['New-Api-User'] = String(uid);
  try {
    const r = await fetch(url, {method: 'POST', headers: headers, credentials: 'include'});
    const text = await r.text();
    return {status: r.status, body: text.slice(0, 300)};
  } catch (e) { return {status: 0, body: String(e).slice(0, 200)}; }
}"""


def available() -> tuple[bool, str]:
    """这条路能不能走：包装库装了没、562MB 的浏览器下了没。返回 (能不能, 说明)。"""
    try:
        import cloakbrowser                              # noqa: PLC0415
    except ImportError:
        return False, "没装 cloakbrowser（pip install cloakbrowser）"
    try:
        info = cloakbrowser.binary_info()
    except Exception as exc:                             # noqa: BLE001
        return False, f"查 CloakBrowser 浏览器状态出错：{str(exc)[:80]}"
    if not info.get("installed"):
        return False, (f"CloakBrowser 的浏览器还没下（{info.get('binary_path')}）："
                       "python -m cloakbrowser install")
    return True, f"CloakBrowser {info.get('version')}（{info.get('tier', '?')}）"


def site_turnstile(origin: str) -> tuple[bool, str]:
    """站点开着 Turnstile 吗、sitekey 是什么。返回 (开没开, sitekey)。"""
    code, text = httpapi.call(origin, "/api/status", "")
    try:
        data = (json.loads(text).get("data") or {}) if code == 200 else {}
    except ValueError:
        data = {}
    return bool(data.get("turnstile_check")), str(data.get("turnstile_site_key") or "")


def launch_browser(headless: bool = True):
    """起一个 CloakBrowser。返回的就是 Playwright 的 Browser 对象（内核换成打过补丁的）。

    `humanize=True` 会把 `page.mouse.*` / `click` / 键盘输入换成拟人版（贝塞尔轨迹、
    随机按压时长），Cloudflare 那道验证要的就是这个。
    """
    import cloakbrowser                                  # noqa: PLC0415

    return cloakbrowser.launch(headless=headless, humanize=True)


def new_page(browser, state: dict | None = None):
    """开一个上下文 + 页面；给了 storage_state 就把 cookie 和 localStorage 一起灌进去。

    **不加**我们自己那套指纹注入脚本（`fingerprint.py`）——CloakBrowser 在 C++ 层已经做了，
    再盖一层 JS 反而是破绽。
    """
    ctx = browser.new_context(**({"storage_state": state} if state else {}))
    return ctx, ctx.new_page()


# --------------------------------------------------------------------------
# Cloudflare widget 的位置与点击（承载页和站点自己的弹窗都用这套）
# --------------------------------------------------------------------------

# 从隐藏 input 往上逐层取祖先的位置，交给 Python 挑出真正的 widget：
# 弹窗刚出来那半秒量到的是过渡布局（400×32 之类），按它算复选框会点空。
WIDGET_JS = """() => {
  const inp = document.querySelector('input[name="cf-turnstile-response"],'
                                   + 'textarea[name="cf-turnstile-response"]');
  if (!inp) return null;
  const cands = [];
  let el = inp.parentElement;
  for (let i = 0; el && i < 6; i++, el = el.parentElement) {
    const r = el.getBoundingClientRect();
    cands.push({x: r.x, y: r.y, w: r.width, h: r.height});
  }
  return {cands: cands, token: String(inp.value || '').length};
}"""

WIDGET_W = 300            # Cloudflare 标准 widget 宽度（用来判断量到的是不是它 + 缩放比）
# 复选框中心离 widget 左边的候选偏移（按宽度等比缩放，一轮点一个）：
# 站点弹窗里的 widget 是 300×69，实测 22 命中；承载页上我们自己 render 的是 320×70，实测 30 命中
POPUP_DXS = (22, 30, 16)
CARRIER_DXS = (30, 22, 38)


def pick_widget(info: dict | None) -> dict | None:
    """从祖先候选里挑出长得像 Turnstile widget 的那一块（240~380 × 45~110）。"""
    for c in (info or {}).get("cands", []):
        if 240 <= c["w"] <= 380 and 45 <= c["h"] <= 110:
            return c
    return None


def wait_widget(page, log, timeout: float = 20.0, stop=None) -> dict | None:
    """等站点弹窗里的 widget 定型（连续两次量到同一个位置才算），返回它的位置。"""
    last = None
    deadline = time.monotonic() + timeout
    info = None
    while time.monotonic() < deadline:
        if stop and stop():
            return None
        info = page.evaluate(WIDGET_JS)
        if (info or {}).get("token"):
            return pick_widget(info) or {}
        cur = pick_widget(info)
        if cur and last and abs(cur["x"] - last["x"]) < 0.5 \
                and abs(cur["h"] - last["h"]) < 0.5:
            log(f"  验证组件定型：{cur['w']:.0f}x{cur['h']:.0f} @({cur['x']:.0f},{cur['y']:.0f})")
            return cur
        last = cur
        time.sleep(0.5)
    log("  没等到验证组件定型，量到的候选："
        f"{[(round(c['w']), round(c['h'])) for c in (info or {}).get('cands', [])]}")
    return None


def click_widget(page, widget: dict, log, n: int = 1, dxs: tuple = POPUP_DXS) -> None:
    """点复选框：widget 左边 ``dxs[n-1]``（按宽度等比缩放）、垂直居中。

    ⚠ 时序：widget 刚挂上那几秒是「正在验证…」的自动阶段，复选框还没画出来，那时候点等于
    点在动画上（点击落进 iframe，父文档收不到事件，看着像点中了）。调用方要先等几秒。
    """
    scale = max(0.5, min(1.5, (widget.get("w") or WIDGET_W) / WIDGET_W))
    dx = dxs[(n - 1) % len(dxs)] * scale
    cx = widget["x"] + dx
    cy = widget["y"] + widget["h"] / 2
    log(f"  第 {n} 下点复选框 @({cx:.0f},{cy:.0f})")
    page.mouse.move(max(cx + 70, 8.0), max(cy + 50, 8.0), steps=4)
    page.mouse.move(cx, cy, steps=6)
    time.sleep(0.2)
    page.mouse.click(cx, cy)


def pass_cf_widget(page, log, wait: float = 30.0, done=None, stop=None) -> bool:
    """站点弹出人机验证时把它点过去。返回是否拿到了令牌（或 ``done()`` 说已经成了）。

    ``done`` 是可选的"外部判据"（比如用接口查站点说签上了没有）——有些站点前端拿到令牌就
    自己把请求发了，隐藏 input 随后会被清空，所以不能只看 input。
    ``stop`` 给了就每轮问一句"要不要停"，按了停止立刻返回（不干等满 `wait` 秒）。
    """
    widget = wait_widget(page, log, timeout=min(wait, 20.0), stop=stop)
    if widget is None:
        return bool(done and done())
    t0 = time.monotonic()
    clicks = 0
    while time.monotonic() - t0 < wait:
        if stop and stop():
            return False
        info = page.evaluate(WIDGET_JS) or {}
        if info.get("token"):
            log(f"  拿到令牌了（{info['token']} 字符）")
            return True
        if done and done():
            return True
        if clicks < MAX_CLICKS and time.monotonic() - t0 >= FIRST_CLICK_AFTER \
                + clicks * CLICK_EVERY:
            clicks += 1
            cur = pick_widget(page.evaluate(WIDGET_JS)) or widget
            click_widget(page, cur, log, clicks)
            time.sleep(0.6)
            continue
        time.sleep(POLL)
    return bool(done and done())


def _open_carrier(page, origin: str, log) -> None:
    """在站点 origin 下开一个最小页面：请求直接 fulfill 成空白 HTML，不加载站点 SPA。

    （拿 127.0.0.1 当承载页是不行的——sitekey 有域名绑定，换 hostname 连 widget 都不渲染。）
    """
    target = origin.rstrip("/") + HOST_PATH
    page.route(target, lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=CARRIER_HTML))
    page.goto(target, wait_until="domcontentloaded", timeout=30000)
    log(f"  承载页已打开：{page.evaluate('() => location.hostname')}{HOST_PATH}（没加载 SPA）")


def _one_attempt(page, n: int, log, stop=None) -> tuple[str, str]:
    """等挂载 → 等它自己试一会儿 → 点复选框（没成就再点）→ 拿令牌。返回 (令牌, 失败原因)。

    ⚠ 时序是这条路唯一的坑：widget 刚挂上那几秒显示的是「正在验证…」的**自动阶段**，
    复选框还没画出来（实测 1.5~4 秒后才出现）。挂上就立刻点等于点在空白上——点击落进了
    iframe（父文档收不到 mousedown，看着像点中了），但 CF 那边什么都没发生，然后干等超时。
    所以：先给 `FIRST_CLICK_AFTER` 秒让它自己跑（有时自动就签发了，压根不用点），
    还没令牌再点，之后每 `CLICK_EVERY` 秒补一下，最多 `MAX_CLICKS` 次。
    """
    deadline = time.monotonic() + MOUNT_WAIT
    slot: dict = {}
    while True:
        if stop and stop():
            return "", "被停止"
        info = page.evaluate(STATE)
        if info.get("token"):
            return info["token"], ""
        state = info.get("state") or "missing"
        if state in {"missing", "no-global"}:
            return "", f"widget 起不来（{state}）"
        if state == "error":
            # 挂载之前就报错基本都是「api.js load failed」这种网络抽风（实测 35 个账号里 4 个），
            # 别在这儿干等满 MOUNT_WAIT——立刻交给外层重开一页重试，能省 20 秒
            return "", f"widget 起不来（{info.get('error') or 'error'}）"
        slot = info.get("slot") or {}
        if slot.get("h", 0) >= MIN_WIDGET_H:
            log(f"  第 {n} 次：widget 挂上了 {slot['w']:.0f}x{slot['h']:.0f}")
            break
        if time.monotonic() >= deadline:
            return "", (f"挂载超时（state={state} err={info.get('error')} "
                        f"高度 {slot.get('h', 0)}）")
        time.sleep(POLL)

    t0 = time.monotonic()
    deadline = t0 + TOKEN_WAIT
    clicks = 0
    grace = None
    last = ""
    while True:
        if stop and stop():
            return "", "被停止"
        info = page.evaluate(STATE)
        if info.get("token"):
            return info["token"], ""
        state, err = info.get("state") or "missing", info.get("error") or ""
        line = f"state={state} err={err or '-'}"
        if line != last:
            log(f"    [{max(0, deadline - time.monotonic()):.0f}s 剩] {line}")
            last = line
        now = time.monotonic()
        if (clicks < MAX_CLICKS
                and now - t0 >= FIRST_CLICK_AFTER + clicks * CLICK_EVERY):
            clicks += 1
            cur = (page.evaluate(STATE).get("slot") or slot)   # 位置现量，别用老的
            click_widget(page, cur if cur.get("h", 0) >= MIN_WIDGET_H else slot,
                         log, clicks, CARRIER_DXS)
            time.sleep(0.6)
            continue
        if state in {"error", "timeout"}:
            grace = grace or now + 1.5      # 600xxx 有时先报错再自己恢复，留点宽限
            if now >= grace:
                return "", f"widget 错误 {err or state}"
        elif grace:
            grace = None
        if now >= deadline:
            return "", f"等令牌超时（点了 {clicks} 下）"
        time.sleep(POLL)


def solve(origin: str, sitekey: str, *, headless: bool = True, log=print,
          attempts: int = ATTEMPTS, cookies: list | None = None,
          submit: tuple | None = None, stop=None) -> tuple[str, str]:
    """起 CloakBrowser 取一个 Turnstile 令牌。返回 (令牌, 失败原因/提交结果)。

    ``cookies`` 是保存的登录态里那批 cookie（灌进上下文，页面里提交时就能带上）；
    ``submit`` 给 ``(uid, 访问令牌)`` 就在**页面里**顺手提交签到——必须和解令牌走同一条连接。
    """
    bootstrap = BOOTSTRAP.replace("__SITEKEY__", sitekey)
    t0 = time.time()
    browser = launch_browser(headless=headless)
    try:
        ctx, page = new_page(browser)
        if cookies:
            try:
                ctx.add_cookies(cookies)
            except Exception as exc:                      # noqa: BLE001
                log(f"  灌 cookie 没成（不影响取令牌）：{str(exc).splitlines()[0][:70]}")
        log(f"  CloakBrowser 起好了（{time.time() - t0:.1f} 秒，"
            f"{'无头' if headless else '有窗口'}）")
        _open_carrier(page, origin, log)
        page.add_script_tag(content=bootstrap)
        reason = ""
        for n in range(1, max(1, attempts) + 1):
            if stop and stop():
                return "", "被停止"
            token, reason = _one_attempt(page, n, log, stop)
            if token:
                note = f"令牌 {len(token)} 字符"
                if submit is not None:
                    uid, access = submit
                    res = page.evaluate(SUBMIT_IN_PAGE, [token, uid, access])
                    note = f"页面内提交 HTTP {res.get('status')} {res.get('body')}"
                    log(f"  {note}")
                return token, note
            log(f"  第 {n} 次没成：{reason}")
            if reason == "被停止":
                return "", reason
            if n < max(1, attempts):
                # 重来一次用**全新的页面 + 全新的 widget**，不走 turnstile.reset()：
                # reset 之后拿到的令牌实测会被站点判 timeout-or-duplicate 拒掉
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.add_script_tag(content=bootstrap)
                log("  （重开承载页 + 重新注入 widget）")
                time.sleep(1.0)
        return "", reason
    finally:
        try:
            browser.close()
        except Exception:                                 # noqa: BLE001
            pass


def run_in_thread(fn, *args, _timeout: float = JOIN_TIMEOUT, **kwargs):
    """在独立线程里跑：调用方那条线程里可能已经有一个 sync playwright 实例了，不能嵌套。

    ``_timeout`` 是兜底上限（协助模式要等人，得给得比 `ASSIST_WAIT` 长）。真卡死了也别把整批
    签到拖住——超时就当这条路失败，交给下一种方式。
    """
    box: dict = {}

    def work() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:                      # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=work, name="cloaksolve", daemon=True)
    th.start()
    th.join(_timeout)
    if th.is_alive():
        raise TimeoutError(f"CloakBrowser 那条线程 {_timeout:.0f} 秒还没回来")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def checkin_by_token(acct: Account, st, client, log) -> tuple[bool, str]:
    """签到方式 ``token``：CloakBrowser 在承载页上取令牌，并**在那个页面里**提交签到。

    返回 (提交出去了没有, 说明)。真正算不算签上由调用方回查接口决定。
    全程不碰站点前端，也不需要普通 Chrome 的配置目录。
    """
    origin = client.origin
    need, sitekey = site_turnstile(origin)
    if not need:
        return False, "站点没开 Turnstile，这条路用不上（该用接口直签）"
    if not sitekey:
        return False, "站点开着 Turnstile 但 /api/status 没给 sitekey"
    # 页面里提交要鉴权：优先访问令牌（那接口是**重新生成**，只在没存过时才调）
    if not (client.token and client.uid) and client.cookies:
        if client.mint_token():
            log("  顺手生成了站点访问令牌（页面内提交要用它鉴权）")
    cookies = (session.load_state(acct.username, st.session_dir) or {}).get("cookies")
    try:
        token, note = run_in_thread(solve, origin, sitekey, headless=HEADLESS, log=log,
                                    cookies=cookies or [],
                                    submit=(client.uid, client.token),
                                    stop=st.stop_flag)
    except Exception as exc:                              # noqa: BLE001
        # 起不来浏览器 / playwright 报错都算这条路不通，交给下一种方式，别打断整批
        return False, (f"CloakBrowser 出错：{type(exc).__name__} "
                       f"{str(exc).splitlines()[0][:90]}")
    if not token:
        return False, f"没拿到 Turnstile 令牌（{note}）"
    return True, note
