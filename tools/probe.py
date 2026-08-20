"""探测页面结构：打印按钮/链接/输入框，站点改版时用来核对选择器。

用法：
    python probe.py                       # 探测邀请注册页
    python probe.py https://site-a.example/console/token
"""

from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

from runner import SIGNUP_URL, pick_channel

JS_DUMP = """
() => {
  const pick = (sel) => [...document.querySelectorAll(sel)].slice(0, 40).map(e => ({
    tag: e.tagName.toLowerCase(),
    text: (e.innerText || e.value || '').trim().slice(0, 60),
    id: e.id || undefined,
    name: e.name || undefined,
    type: e.type || undefined,
    href: e.getAttribute('href') || undefined,
    cls: (e.className || '').toString().slice(0, 80) || undefined,
  }));
  return {
    title: document.title,
    url: location.href,
    buttons: pick('button'),
    links: pick('a'),
    inputs: pick('input'),
  };
}
"""


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else SIGNUP_URL
    with sync_playwright() as pw:
        channel = pick_channel()
        browser = pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
        page = browser.new_context(locale="zh-CN").new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(3500)
        print(json.dumps(page.evaluate(JS_DUMP), ensure_ascii=False, indent=2))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
