"""验证 CloakBrowser 能不能**直接开站点前端**（灌登录态 → 过 CF 拦截页 → 头像菜单 → 个人资料）。

    python tools/probe_cloak_ui.py <账号> [站点] [--headful]

这是"流程化签到"（自动签到的第三条路）和协助签到要用的那套能力：以前这一步只能用普通 Chrome
（Playwright 驱动的 Chromium 拿不到 Turnstile 令牌），CloakBrowser 如果能顶下来，
`seed_profile` / `cdpclick` / `autoclick` 那一堆就都不需要了。

只读不写：不签到、不改 sessions/（storage_state 只往浏览器里灌，不回存）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths                                             # noqa: E402
import runner                                            # noqa: E402
import session                                           # noqa: E402
import sites                                             # noqa: E402

BTN_STATE = """() => {
  const out = [];
  for (const b of document.querySelectorAll("button,[role='button']")) {
    const s = (b.textContent || '').trim();
    if (s && s.length < 24 && /签到|打卡|领取/.test(s)) {
      const r = b.getBoundingClientRect();
      out.push({text: s, w: Math.round(r.width), h: Math.round(r.height)});
    }
  }
  return {path: location.pathname, 签到相关按钮: out,
          有头像: !!document.querySelector('header button')};
}"""


def main(argv: list[str]) -> int:
    import cloakbrowser                                  # noqa: PLC0415

    if not argv:
        print(__doc__)
        return 2
    user = argv[0]
    site = sites.by_key(argv[1]) if len(argv) > 1 and not argv[1].startswith("-") \
        else sites.DEFAULT
    headless = "--headful" not in argv
    base = paths.APP_DIR
    st = runner.Settings(signup_url=site.signup_url, session_dir=site.sessions_dir(base),
                         tokens_path=site.tokens_path(base), keys_path=site.keys_path(base))
    state = session.load_state(user, st.session_dir)
    if not state:
        print(f"{user} 没有保存的登录态")
        return 1
    print(f"账号 {user}  站点 {site.name} {site.origin}  headless={headless}")

    t0 = time.time()
    browser = cloakbrowser.launch(headless=headless, humanize=True)
    try:
        ctx = browser.new_context(storage_state=state)   # cookie + localStorage 一起灌
        page = ctx.new_page()
        print(f"  浏览器起好了（{time.time() - t0:.1f} 秒）")
        t1 = time.time()
        page.goto(site.origin + "/dashboard", wait_until="domcontentloaded", timeout=60000)
        print(f"  goto dashboard 用了 {time.time() - t1:.1f} 秒，title={page.title()[:40]!r}")
        runner.wait_cf_challenge(page, st, print, tries=3)
        print(f"  过拦截页之后 url={page.url}  title={page.title()[:40]!r}"
              f"（累计 {time.time() - t0:.1f} 秒）")
        ok, bearer, info = runner.check_login(page, tries=2)
        print(f"  登录态：{'有效' if ok else '无效'}  uid={info.get('id')} "
              f"用户名={info.get('username')} 余额={info.get('quota')}")
        if not ok:
            print("  没登进去，后面就不用试了")
            return 1
        stat = runner.checkin_status(page, bearer, print)
        print(f"  接口查签到状态：{stat}")
        print(f"  页面按钮实况：{page.evaluate(BTN_STATE)}")
        if runner.open_profile(page, st, print):
            print(f"  进到个人资料页了：{page.url}")
        else:
            print(f"  没进去，现在在 {page.url}")
        print(f"  个人资料页按钮实况：{page.evaluate(BTN_STATE)}")
        print(f"  合计 {time.time() - t0:.1f} 秒")
        return 0
    finally:
        try:
            browser.close()
        except Exception:                                # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
