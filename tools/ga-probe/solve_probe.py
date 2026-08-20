"""独立的 Turnstile 取令牌探测脚本（给 GitHub Actions 用，**不带任何凭证**）。

    python solve_probe.py <站点域名> [--testkey]

干的事：问站点 `/api/status` 拿 sitekey → 起 CloakBrowser → 在站点域名下的空白承载页上
自己渲一个 Turnstile widget → 等 3 秒点复选框 → 看 Cloudflare 给不给令牌。
顺带打印这台机器的出口 IP，方便判断"是不是因为数据中心 IP 被 CF 压分"。

**站点域名要自己填**（`site-b.example` 只是占位符，跑不通）。
**只读**：不登录、不签到、不碰账号数据。所以可以放心丢到一个空的私有仓库里跑。
`--testkey` 换成 Cloudflare 官方测试 sitekey（任何环境都该过），作为对照组。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

TEST_KEY = "1x00000000000000000000AA"
HOST_PATH = "/__checkin_turnstile__"
CARRIER = ("<!doctype html><html><head><meta charset=utf-8><title>probe</title>"
           "</head><body></body></html>")
FIRST_CLICK_AFTER = 3.0      # 挂载后先等几秒：那几秒 widget 在「正在验证…」，复选框还没画出来
CLICK_EVERY = 5.0
MAX_CLICKS = 3
WAIT = 40.0

BOOTSTRAP = r"""(() => {
  const host = document.createElement('div');
  host.id = 'ck';
  host.setAttribute('data-state', 'init');
  host.style.cssText = 'position:fixed;left:24px;top:24px;width:320px;background:#fff;padding:4px';
  const slot = document.createElement('div');
  slot.id = 'ck-slot';
  host.appendChild(slot);
  (document.body || document.documentElement).appendChild(host);
  const render = () => {
    try {
      window.turnstile.render(slot, {
        sitekey: '__SITEKEY__',
        callback: (t) => { host.setAttribute('data-token', t);
                           host.setAttribute('data-state', 'done'); },
        'error-callback': (c) => { host.setAttribute('data-state', 'error');
                                   host.setAttribute('data-error', String(c || '?')); },
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

STATE = """() => {
  const h = document.getElementById('ck'), s = document.getElementById('ck-slot');
  const r = s ? s.getBoundingClientRect() : null;
  let tok = (h && h.getAttribute('data-token')) || '';
  if (!tok) { const i = document.querySelector('input[name="cf-turnstile-response"]');
              if (i && i.value) tok = i.value; }
  return {state: (h && h.getAttribute('data-state')) || 'missing',
          error: (h && h.getAttribute('data-error')) || '', token: tok,
          box: r ? {x: r.x, y: r.y, w: r.width, h: r.height} : null};
}"""


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(4000).decode("utf-8", "replace")


def egress_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            return _get(url, 10).strip()
        except Exception:                                # noqa: BLE001
            continue
    return "?"


def site_sitekey(origin: str) -> tuple[bool, str]:
    try:
        data = (json.loads(_get(origin + "/api/status")).get("data") or {})
    except Exception as exc:                             # noqa: BLE001
        print(f"  问 /api/status 失败：{type(exc).__name__} {exc}")
        return False, ""
    return bool(data.get("turnstile_check")), str(data.get("turnstile_site_key") or "")


def main(argv: list[str]) -> int:
    import cloakbrowser                                  # noqa: PLC0415

    origin = "https://" + (argv[0] if argv and not argv[0].startswith("-")
                           else "site-b.example")
    print(f"出口 IP：{egress_ip()}")
    info = cloakbrowser.binary_info()
    print(f"CloakBrowser {info.get('version')}（{info.get('tier')}）{info.get('platform')} "
          f"installed={info.get('installed')}")
    need, sitekey = site_sitekey(origin)
    print(f"站点 {origin}  turnstile_check={need}  sitekey={sitekey}")
    if "--testkey" in argv:
        sitekey = TEST_KEY
        print(f"改用官方测试 sitekey {sitekey}（对照组）")
    if not sitekey:
        print("拿不到 sitekey，没法测")
        return 2

    t0 = time.time()
    browser = cloakbrowser.launch(headless=True, humanize=True)
    try:
        page = browser.new_context().new_page()
        target = origin + HOST_PATH
        page.route(target, lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=CARRIER))
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
        print(f"承载页已开：{page.evaluate('() => location.hostname')}{HOST_PATH}"
              f"（{time.time() - t0:.1f}s）")
        page.add_script_tag(content=BOOTSTRAP.replace("__SITEKEY__", sitekey))

        box = None
        for _ in range(60):                              # 等 widget 挂载
            st = page.evaluate(STATE)
            if st.get("token"):
                print(f"*** 没点就签发了：{len(st['token'])} 字符")
                return 0
            if (st.get("box") or {}).get("h", 0) >= 50:
                box = st["box"]
                print(f"widget 挂上了 {box['w']:.0f}x{box['h']:.0f}")
                break
            if st.get("state") in ("missing", "no-global", "error"):
                print(f"widget 起不来：state={st.get('state')} err={st.get('error')}")
                return 1
            time.sleep(0.4)
        if not box:
            print("widget 挂载超时")
            return 1

        clicks, start = 0, time.monotonic()
        while time.monotonic() - start < WAIT:
            st = page.evaluate(STATE)
            if st.get("token"):
                print(f"\n*** 拿到令牌：{len(st['token'])} 字符，前 30 位 {st['token'][:30]}…"
                      f"（点了 {clicks} 下，共 {time.time() - t0:.1f}s）")
                return 0
            if clicks < MAX_CLICKS and (time.monotonic() - start
                                        >= FIRST_CLICK_AFTER + clicks * CLICK_EVERY):
                clicks += 1
                cx = box["x"] + 30
                cy = box["y"] + box["h"] / 2
                page.mouse.move(cx + 70, cy + 50, steps=4)
                page.mouse.move(cx, cy, steps=6)
                time.sleep(0.2)
                page.mouse.click(cx, cy)
                print(f"  第 {clicks} 下点复选框 @({cx:.0f},{cy:.0f}) state={st.get('state')}")
                continue
            time.sleep(0.4)
        st = page.evaluate(STATE)
        print(f"\n没拿到令牌：state={st.get('state')} err={st.get('error') or '-'}"
              f"（点了 {clicks} 下，共 {time.time() - t0:.1f}s）")
        return 1
    finally:
        try:
            browser.close()
        except Exception:                                # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
