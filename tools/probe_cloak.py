"""验证 CloakBrowser 那条路能不能拿到站点的 Turnstile 令牌（**只取令牌，不签到**）。

    python tools/probe_cloak.py [站点] [--headful] [--attempts N] [--testkey]

拿到就打印长度和前 30 位。要真签到走 `cli.py --site <站点> --checkin-auto`（会优先用这条路）。

`--testkey` 换成 Cloudflare 官方测试 sitekey（`1x00000000000000000000AA`，任何域名都放行、
总是通过）：**它能过、真 sitekey 不过 = 点击和管线都是对的，卡在 CF 对这个浏览器的判分**；
它也不过 = 点没点到复选框那类自己的问题。

和 `probe_camoufox.py` 是同一套做法（站点 origin 下的空白承载页 + 自己 render 的 widget），
只把浏览器换成 CloakBrowser（打了 C++ 补丁的 Chromium，起得比 Camoufox 快很多）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloaksolve                                        # noqa: E402
import sites                                             # noqa: E402


def main(argv: list[str]) -> int:
    site = sites.by_key(argv[0]) if argv and not argv[0].startswith("-") else sites.DEFAULT
    headless = "--headful" not in argv
    attempts = cloaksolve.ATTEMPTS
    for i, a in enumerate(argv):
        if a == "--attempts" and i + 1 < len(argv):
            attempts = int(argv[i + 1])

    ok, why = cloaksolve.available()
    print(f"CloakBrowser: {why}")
    if not ok:
        return 1
    need, sitekey = cloaksolve.site_turnstile(site.origin)
    print(f"站点 {site.name} {site.origin}\nturnstile_check={need} sitekey={sitekey}")
    if not need:
        print("这个站点没开 Turnstile，不用求解")
        return 0
    if "--testkey" in argv:
        sitekey = "1x00000000000000000000AA"
        print(f"改用官方测试 sitekey {sitekey}（对照组）")

    t0 = time.time()
    token, note = cloaksolve.solve(site.origin, sitekey, headless=headless,
                                   attempts=attempts)
    if not token:
        print(f"\n没拿到令牌（{note}，共 {time.time() - t0:.1f} 秒）")
        return 1
    print(f"\n*** 拿到令牌：{len(token)} 字符，前 30 位 {token[:30]}…"
          f"（共 {time.time() - t0:.1f} 秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
