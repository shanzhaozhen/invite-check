"""用 CloakBrowser 走**站点自己的界面**做签到（自动 / 协助两条路都在这里）。

以前这件事只能用普通 Chrome：Playwright 驱动的普通 Chromium 拿不到 Turnstile 令牌，所以要把
登录态灌进 `data/chrome-profiles/`、`subprocess` 裸起 chrome.exe、再手写 CDP 短连去点复选框
（老代码 `cdpclick.py` / `autoclick.py`）。换成 CloakBrowser 之后这些全都不需要了——它就是
Playwright，只是内核打过补丁，Cloudflare 认它是真人，所以：

- 页面直接 `browser.new_context(storage_state=...)` 灌登录态（cookie + localStorage 一起）
- 站点的 CF 拦截页照常过（实测 site-b 无头 3.7 秒进 dashboard）
- 签到弹窗里的复选框用 `page.mouse.click()` 点就行（`cloaksolve.pass_cf_widget`）

两个入口：

- `checkin_via_ui`：自动签到的**最后一条路**（接口直签、承载页取令牌都不成时用），无头、不占屏幕
- `assist_one`：协助签到——**有窗口**，先自己把能点的都点了，实在不行才等你动手

⚠ 走界面会加载站点前端，site-a 那张 refresh cookie 会被换掉，所以结束前一定要
`persist_session` 把新的登录态存回 `sessions/`，否则下次拿废票开局。
"""

from __future__ import annotations

import time

import cloaksolve
import session
from store import Account

ASSIST_WAIT = 300.0          # 协助模式最多等你多久（秒）
ASSIST_POLL = 4.0            # 多久回查一次站点状态
CF_WAIT = 30.0               # 点完签到按钮后给人机验证的时间


def _enter_site(browser, acct: Account, st, log):
    """灌登录态 → 打开站点 → 过 CF 拦截页 → 确认登录。返回 (ctx, page, 登进去没有, bearer, info)。

    ⚠ `bearer` 空**不代表没登录**：site-b 那个版本没有 `/api/user/auth/refresh`，
    它的鉴权是 cookie + `New-Api-User` 头，bearer 一直是空的。判据只看 `check_login` 的第一项。
    """
    import runner                                        # noqa: PLC0415

    origin = runner.site_origin(st.signup_url)
    state = session.load_state(acct.username, st.session_dir)
    ctx, page = cloaksolve.new_page(browser, state)
    page.goto(origin + "/dashboard", wait_until="domcontentloaded", timeout=st.timeout_ms)
    runner.wait_cf_challenge(page, st, log, tries=3)
    ok, bearer, info = runner.check_login(page, tries=2)
    if not ok:
        log("  站点说没登录（登录态过期了？）")
        # 没登上大概会被弹到登录页：那上面有「我已阅读并同意」的话先替人勾上，
        # 协助模式里接手的人就少点一下（自动模式下勾了也没坏处）
        runner.accept_terms(page, log)
        return ctx, page, False, "", {}
    log(f"  已进入站点：{info.get('username') or acct.username} uid={info.get('id')}")
    # 顺手补一张长期访问令牌（没存过才生成，那个接口是重新生成）
    runner.mint_token_in_page(page, bearer, st, acct.username, info.get("id"), log)
    return ctx, page, True, bearer, info


def _try_checkin_on_page(page, st, bearer: str, log) -> bool:
    """在已登录的页面上把签到点完：个人资料页 → 立即签到 →（弹验证就点过去）。

    返回"站点是不是已经说签上了"。判据一律用接口（`GET /api/user/checkin`），不看页面文案。
    """
    import runner                                        # noqa: PLC0415

    def signed() -> bool:
        return bool((runner.checkin_status(page, bearer, log) or {}).get("checked_in_today"))

    if signed():
        return True
    if not runner.open_profile(page, st, log):
        log("  打不开个人资料页")
        return False
    if not runner.click_checkin(page, log):
        runner._log_checkin_area(page, log)
        return signed()          # 有可能按钮已经是「已签到」了
    page.wait_for_timeout(1500)
    if signed():
        log("  站点没要求验证，点完就签上了")
        return True
    # 站点弹了「安全验证」：把复选框点过去（挂载后等 3 秒再点，见 cloaksolve）
    return cloaksolve.pass_cf_widget(page, log, wait=CF_WAIT, done=signed,
                                     stop=st.stop_flag)


def checkin_via_ui(acct: Account, st, log, visible: bool = False) -> tuple[bool, str]:
    """走站点界面签到（自动签到的最后一条路）。返回 (成没成, 说明)。

    ``visible=True`` 就开一个能看见的窗口（协助模式复用这条）。
    """
    import runner                                        # noqa: PLC0415

    if not session.load_state(acct.username, st.session_dir):
        return False, "没有保存的登录态，先跑一次注册/取 Key"
    if st.stop_flag():
        return False, "被停止"
    t0 = time.time()
    browser = cloaksolve.launch_browser(headless=not visible)
    try:
        ctx, page, entered, bearer, info = _enter_site(browser, acct, st, log)
        if not entered:
            return False, "登录态失效，进不去站点"
        if st.stop_flag():
            return False, "被停止"
        try:
            ok = _try_checkin_on_page(page, st, bearer, log)
        finally:
            # 加载过前端就可能换发了 refresh cookie，成没成都要存回去
            runner.persist_session(ctx, acct.username, st, log, uid=info.get("id"))
        return ok, (f"走界面签到{'成功' if ok else '没成'}（{time.time() - t0:.0f} 秒）")
    finally:
        try:
            browser.close()
        except Exception:                                # noqa: BLE001
            pass


def assist_one(acct: Account, st, log) -> tuple[str, str]:
    """协助签到一个账号：先自动试，不成再等你点。返回 (状态, 备注)。

    状态：ok 签上了 / done 今天已签过 / skip 站点没开签到 / fail 没搞定。
    你点完不用关窗口——工具查到签上了会自己关掉，接着下一个。窗口被你关掉也会往下走。
    """
    import httpapi                                       # noqa: PLC0415
    import runner                                        # noqa: PLC0415

    origin = runner.site_origin(st.signup_url)
    client = httpapi.SiteClient(origin, acct.username, st.session_dir, st.tokens_path)
    if not client.ready:
        return "fail", "没有保存的登录态，先跑一次注册/取 Key"
    stat = client.checkin_status()
    if stat:
        if not stat.get("enabled"):
            return "skip", "站点没有开启每日签到"
        if stat.get("checked_in_today"):
            runner.record_meta(acct.username, st.session_dir, last_checkin=stat.get("day"))
            quota = runner.record_quota(st, acct.username, client.self_info(), log)
            return "done", (f"今天已签到（累计 {stat.get('count')} 天）"
                            + (f" 余额 {quota}" if quota else "") + " 不用麻烦你")

    browser = cloaksolve.launch_browser(headless=False)
    try:
        ctx, page, entered, bearer, info = _enter_site(browser, acct, st, log)
        signed = False
        if entered:
            signed = _try_checkin_on_page(page, st, bearer, log)
            if signed:
                log("  自动就签上了，没麻烦你")
        else:
            log("  自动登录没成，窗口给你了：自己登进去再点「每日签到」")
        if not signed:
            log("  轮到你了：点「每日签到」（有人机验证就勾一下），签上我就自动关窗口继续下一个")
            signed = _wait_for_human(browser, page, client, log, st)
        try:
            runner.persist_session(ctx, acct.username, st, log, uid=info.get("id"))
        except Exception as exc:                          # noqa: BLE001
            log(f"  存登录态没成：{str(exc).splitlines()[0][:70]}")
    finally:
        try:
            browser.close()
        except Exception:                                # noqa: BLE001
            pass

    after = client.checkin_status() or {}
    if after.get("checked_in_today"):
        runner.record_meta(acct.username, st.session_dir, last_checkin=after.get("day"))
        award = runner._award_text(after.get("award", 0))
        quota = runner.record_quota(st, acct.username, client.self_info(), log)
        return "ok", (f"签到成功（累计 {after.get('count')} 天 {award}）"
                      + (f" 余额 {quota}" if quota else "")).strip()
    return "fail", "窗口关了但站点还是显示没签到"


def _wait_for_human(browser, page, client, log, st=None,
                    wait: float = ASSIST_WAIT) -> bool:
    """等人在窗口里点。每 `ASSIST_POLL` 秒用接口回查一次；签上、窗口被关、按了停止就返回。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        for _ in range(int(ASSIST_POLL / 0.3) or 1):     # 拆成小段睡，「停止」才能立刻生效
            if st is not None and st.stop_flag():
                log("  收到停止指令，不等了")
                return False
            time.sleep(0.3)
        if (client.checkin_status() or {}).get("checked_in_today"):
            log("  站点已经显示签到成功")
            return True
        if page.is_closed() or not browser.is_connected():
            log("  窗口被关掉了")
            return False
    log(f"  等了 {int(wait)} 秒还没签上，先跳过这个账号")
    return False

