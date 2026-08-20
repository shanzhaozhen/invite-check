"""签到相关的探测工具（不会真的点签到）。

用保存的登录态打开个人资料页，报告签到状态、页面上找到的签到按钮和它们的可点状态。
站点改版或签到点不动时先跑这个：
    python probe_profile.py user-a            # 默认 site-a
    python probe_profile.py user-a site-b   # 指定站点
"""

import json
import sys

from playwright.sync_api import sync_playwright

import sites
from paths import APP_DIR
from runner import (
    CHECKIN_TEXT,
    LAUNCH_ARGS,
    Settings,
    check_login,
    checkin_status,
    new_context,
    open_profile,
    persist_session,
    site_origin,
    wait_cf_challenge,
)

JS_AREA = """
() => {
  const out = {url: location.href, buttons: [], snippet: ''};
  for (const e of document.querySelectorAll('button, a, [role=button]')) {
    const t = (e.innerText || '').trim();
    if (/签到|打卡|领取/.test(t)) {
      out.buttons.push({text: t.replace(/\\s+/g, ' ').slice(0, 40),
                        tag: e.tagName.toLowerCase(),
                        disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true'});
    }
  }
  const m = document.body.innerText.match(/.{0,60}签到.{0,120}/s);
  out.snippet = m ? m[0].replace(/\\s+/g, ' ') : '';
  return out;
}
"""


def main() -> int:
    user = sys.argv[1] if len(sys.argv) > 1 else "user-a"
    site = sites.by_key(sys.argv[2]) if len(sys.argv) > 2 else sites.DEFAULT
    st = Settings(signup_url=site.signup_url, headless=False,
                  session_dir=site.sessions_dir(APP_DIR))
    origin = site_origin(st.signup_url)
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=False, args=LAUNCH_ARGS,
                               ignore_default_args=["--enable-automation"])
        ctx = new_context(b, st, username=user, log=print)
        page = ctx.new_page()
        try:
            page.goto(f"{origin}/keys", wait_until="domcontentloaded")
            wait_cf_challenge(page, st, print)
            ok, bearer, info = check_login(page, tries=3)
            if not ok:
                print(f"登录态失效，先跑一次: python cli.py --site {site.key} --only {user} --rerun-done")
                return 1
            persist_session(ctx, user, st, print)  # 换过票就存回去，别把会话烧掉
            print(f"[{site.name}] 登录: id={info.get('id')} user={info.get('username')}")
            print("签到状态:", json.dumps(checkin_status(page, bearer), ensure_ascii=False))

            page.wait_for_timeout(2000)
            print("进个人资料页:", open_profile(page, st, print), "->", page.url)
            print("签到区域:", json.dumps(page.evaluate(JS_AREA), ensure_ascii=False)[:600])

            btns = page.locator("button, [role='button']").filter(
                has_text=CHECKIN_TEXT).filter(has_not_text="已签到")
            print(f"click_checkin 会看到 {btns.count()} 个候选按钮：")
            for i in range(btns.count()):
                bt = btns.nth(i)
                print(f"  [{i}] 可见={bt.is_visible()} 可点={bt.is_enabled()} "
                      f"文本={' '.join((bt.inner_text() or '').split())[:40]!r}")
            persist_session(ctx, user, st, print)
        finally:
            b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
