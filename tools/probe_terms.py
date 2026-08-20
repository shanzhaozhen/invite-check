"""看注册页/登录页上有没有「我已阅读并同意」这类勾选框，以及 GitHub 按钮是什么状态。

    python tools/probe_terms.py [站点...]        # 不给就把登记表里所有站点都看一遍

用 CloakBrowser 打开每个站点的**注册页和登录页**（不登录、不注册、什么都不点），把页面上所有
勾选框和 GitHub 按钮的实况打出来——`runner.AGREE_CLICK` 的候选选择器就是照这个结果配的。
顺带跑一次 `runner.accept_terms`，看勾得上勾不上、勾完按钮解不解禁。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloaksolve                                        # noqa: E402
import runner                                            # noqa: E402
import sites                                             # noqa: E402

SCAN = """() => {
  const box = (e) => { const r = e.getBoundingClientRect();
    return {w: Math.round(r.width), h: Math.round(r.height)}; };
  const near = (e) => {
    let p = e, out = '';
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      out = (p.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
      if (out) break;
    }
    return out;
  };
  const cbs = [...document.querySelectorAll(
    'input[type=checkbox],[role=checkbox],[class*=checkbox],[class*=Checkbox]')]
    .slice(0, 12).map(e => ({
      tag: e.tagName.toLowerCase(), type: e.type || '', id: e.id || '',
      cls: (e.className || '').toString().slice(0, 60),
      checked: e.checked === undefined ? null : e.checked,
      aria: e.getAttribute('aria-checked'),
      可见: box(e).w > 0 && box(e).h > 0, 尺寸: box(e), 附近文字: near(e),
    }));
  const btns = [...document.querySelectorAll('button,a,[role=button]')]
    .filter(e => /github/i.test(e.textContent || '') || /github/i.test(e.getAttribute('href') || ''))
    .slice(0, 5).map(e => ({
      tag: e.tagName.toLowerCase(), 文字: (e.textContent || '').trim().slice(0, 30),
      disabled: e.disabled === undefined ? null : e.disabled,
      cls: (e.className || '').toString().slice(0, 60),
      href: (e.getAttribute('href') || '').slice(0, 60), 尺寸: box(e),
    }));
  const agree = [...document.querySelectorAll('*')]
    .filter(e => e.children.length === 0
                 && /已阅读|同意|agree|terms|条款|隐私/i.test(e.textContent || ''))
    .slice(0, 6).map(e => ({tag: e.tagName.toLowerCase(),
                            文字: (e.textContent || '').trim().slice(0, 50)}));
  return {url: location.href, 勾选框: cbs, GitHub按钮: btns, 同意相关文字: agree};
}"""


def main(argv: list[str]) -> int:
    keys = argv or [s.key for s in sites.SITES]
    browser = cloaksolve.launch_browser(headless=True)
    try:
        for key in keys:
            site = sites.by_key(key)
            st = runner.Settings(signup_url=site.signup_url)
            pages = [("注册页", site.signup_url),
                     ("登录页", site.origin + runner.LOGIN_PATH)]
            for label, url in pages:
                ctx, page = cloaksolve.new_page(browser)
                try:
                    print(f"\n===== {site.name} · {label} {url} =====")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    runner.wait_cf_challenge(page, st, print, tries=3)
                    page.wait_for_timeout(2500)      # 等 React 把表单画出来
                    print(json.dumps(page.evaluate(SCAN), ensure_ascii=False, indent=2))
                    print(f"  terms_state: {runner.terms_state(page)}")
                    if runner.accept_terms(page, print):
                        print(f"  勾完再看: {runner.terms_state(page)}")
                except Exception as exc:             # noqa: BLE001
                    print(f"  出错：{type(exc).__name__} {str(exc).splitlines()[0][:100]}")
                finally:
                    ctx.close()
        return 0
    finally:
        try:
            browser.close()
        except Exception:                            # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
