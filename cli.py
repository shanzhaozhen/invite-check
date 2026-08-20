"""命令行入口。

    python cli.py                          # 注册/登录并生成 key（已成功的自动跳过）
    python cli.py --site site-b          # 换站点；--site all 依次跑所有启用站点
    python cli.py --limit 3                # 只跑前 3 个，适合测试
    python cli.py --checkin                # 每日签到（没在本站点注册成功的会跳过）
    python cli.py --export table --all-sites   # 导出所有站点的 key，不开浏览器
    python cli.py --sites                  # 看站点登记表
    python cli.py --open user-c            # 用保存的登录态开一个已登录窗口
Ctrl+C 可以随时中断，已完成的账号会跳过，重跑同一条命令就是继续。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import export
import keystore
import session as sess
import sites
from paths import ACCOUNTS_PATH, APP_DIR, safe_console
from runner import Settings, open_account, run_batch, run_checkin, run_set_password
from store import (
    ResultStore,
    detect_format,
    dump_accounts,
    load_accounts,
    merge_accounts,
    read_rows,
    save_accounts,
)
from totp_util import totp

BASE = APP_DIR
CHECKIN_HEADER = "# 时间\t账号\t状态\t保留列\t备注"
ALL = "all"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="邀请站点注册 / API Key / 每日签到（支持多站点）")
    p.add_argument("--accounts", default=str(ACCOUNTS_PATH),
                   help="账号库（json/csv/txt），多站点共用，默认 data/accounts.json")
    p.add_argument("--site", default="",
                   help=f"选站点：{' / '.join(sites.keys())}；`all` = 所有启用站点依次跑；"
                        "也可以逗号分隔按顺序跑几个，如 --site site-a,site-b")
    p.add_argument("--out", default="", help="注册结果日志（不给按站点自动取名）")
    p.add_argument("--checkin-out", default="", help="签到结果日志（不给按站点自动取名）")
    p.add_argument("--url", default="", help="邀请注册链接（给了就按它的域名认站点，覆盖 --site）")
    p.add_argument("--checkin", action="store_true",
                   help="自动签到：接口直签 → CloakBrowser 取令牌 + 接口 → CloakBrowser 走站点界面，"
                        "哪种成了就记进 sites.json，下次这个站点直接用那种。全程无头不占鼠标")
    p.add_argument("--checkin-assist", action="store_true",
                   help="协助签到：CloakBrowser 开一个能看见的已登录窗口，先自动试着点，"
                        "不成才轮到你点「每日签到」；签上工具自动关窗口继续下一个")
    p.add_argument("--checkin-method", default="", choices=["", *sites.METHODS],
                   help="这次强制用哪种签到方式（api / token / ui；auto = 按顺序试探）。"
                        "不给就用站点登记表里配的，没配就用上次成功的那种")
    p.add_argument("--checkin-token", metavar="token", default="",
                   help="配合 --checkin：直接给一个现成的 Turnstile token（从可信浏览器里抓的，"
                        "一次性、有有效期），工具用它直接打签到接口，不开浏览器")
    p.add_argument("--set-password", metavar="密码", default="",
                   help="改站点登录密码（要带 --current-password；OAuth 注册的账号本来没有密码，"
                        "站点会拒，得先绑邮箱走「忘记密码」）；填 auto 就随机生成一个 16 位的")
    p.add_argument("--current-password", metavar="原密码", default="",
                   help="配合 --set-password：站点上现在的密码（不给就用账号库里存的 site_password）")
    p.add_argument("--verify-password", action="store_true",
                   help="配合 --set-password：设完试着用密码登一次"
                        "（登录页有隐藏式 Turnstile，自动化基本过不了，默认不试）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 个账号")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="同时跑几个账号（1=顺序，上限 8）。签到实测 3~6 都稳，每个账号一个"
                        "独立浏览器（约 500MB 内存）；注册并发高了容易被 GitHub 风控盯上")
    p.add_argument("--start", type=int, default=1, help="从第几个账号开始（1 起）")
    p.add_argument("--only", default="", help="只跑指定账号，逗号分隔")
    p.add_argument("--include-disabled", action="store_true", help="连账号库里停用的也跑")
    p.add_argument("--retries", type=int, default=2, help="单账号失败重试次数")
    p.add_argument("--delay", type=float, default=5.0, help="账号之间的间隔秒数")
    p.add_argument("--timeout", type=int, default=30, help="单步等待超时（秒）")
    p.add_argument("--slowmo", type=int, default=0, help="每步放慢毫秒，便于观察")
    p.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")
    p.add_argument("--channel", default="auto", choices=["auto", "chrome", "msedge", "chromium"],
                   help="用哪个浏览器，默认自动挑本机的 Chrome/Edge")
    p.add_argument("--key-name", default="auto", help="生成的 key 名称前缀")
    p.add_argument("--new-key", action="store_true",
                   help="每次都新建 key（默认已有满足条件的就直接复用）")
    p.add_argument("--rerun-done", action="store_true", help="不跳过已成功/今天已签到的账号")
    p.add_argument("--no-session", action="store_true", help="不复用保存的登录态")
    p.add_argument("--no-device-spoof", action="store_true",
                   help="关闭按账号区分设备指纹（分辨率/核数/内存/显卡；默认开，防同机多号被关联）")
    p.add_argument("--no-background", action="store_true",
                   help="别在后台跑，把浏览器窗口显示出来（默认后台：窗口挪到屏幕外，不占屏幕）")

    g = p.add_argument_group("不开浏览器的辅助命令")
    g.add_argument("--export", nargs="?", const="table", choices=export.FORMATS,
                   help="导出 key：table / keys / csv（默认当前 --site 那个站点）")
    g.add_argument("--all-sites", action="store_true",
                   help="配合 --export：导出所有站点的 key（表格/CSV 会多一列站点）")
    g.add_argument("--group", default="", help="导出时只要某个分组：待用 / 已用 / 签到累积 / 已作废")
    g.add_argument("--export-out", default="", help="导出到文件，不给就打印")
    g.add_argument("--check", action="store_true", help="体检账号库：格式、重复、密钥是否可用")
    g.add_argument("--mint-tokens", action="store_true",
                   help="给还没有站点访问令牌的账号各生成一个（长期凭据，之后查状态/取 key "
                        "不用开浏览器；用现有登录态换，不开浏览器）")
    g.add_argument("--sync-quota", action="store_true",
                   help="刷新每个账号在站点上的剩余额度（走接口，不开浏览器；界面和导出都读它）")
    g.add_argument("--sync-checkin", action="store_true",
                   help="把站点侧「今天已签到」对齐到本地日志/索引（**只读站点，绝不发签到请求**，"
                        "没签的原样保留）")
    g.add_argument("--sites", action="store_true", dest="list_sites",
                   help="列出站点登记表（sites.json）和每个站点的进度")
    g.add_argument("--add-site", metavar="链接", default="",
                   help="往登记表里加一个站点（邀请链接，key 从域名推；可配 --site-name）")
    g.add_argument("--site-name", default="", help="配合 --add-site：站点显示名")
    g.add_argument("--import-accounts", metavar="文件", default="",
                   help="从 json/csv/txt 导入账号库")
    g.add_argument("--import-mode", default="merge", choices=["merge", "replace"],
                   help="导入方式：merge 只加没有的（默认）/ replace 覆盖")
    g.add_argument("--export-accounts", metavar="文件", default="",
                   help="把账号库导出成 json/csv/txt（按后缀）")
    g.add_argument("--list-sessions", action="store_true", help="列出已保存的登录态")
    g.add_argument("--export-sessions", metavar="文件", default="", help="打包导出全部登录态")
    g.add_argument("--import-sessions", metavar="文件", default="", help="导入登录态打包文件")
    g.add_argument("--drop-session", metavar="账号", default="", help="删掉某个账号的登录态")
    g.add_argument("--open", default="", metavar="账号", help="用保存的登录态开一个已登录窗口")
    return p


def check_accounts(path: str) -> int:
    """体检账号库：能不能解析、有没有重复、TOTP 密钥能不能算码。"""
    try:
        accounts = load_accounts(path)
    except (OSError, ValueError) as exc:
        print(f"账号库有问题: {exc}", file=sys.stderr)
        return 2
    on = sum(1 for a in accounts if a.enabled)
    print(f"共 {len(accounts)} 个账号（启用 {on}，停用 {len(accounts) - on}）")
    seen: dict[str, int] = {}
    bad = 0
    for i, a in enumerate(accounts, start=1):
        problems = []
        if a.username in seen:
            problems.append(f"和第 {seen[a.username]} 条重复")
        seen.setdefault(a.username, i)
        try:
            totp(a.totp_secret)
        except ValueError as exc:
            problems.append(str(exc))
        if problems:
            bad += 1
            print(f"  第 {i} 条 {a.username}: {'; '.join(problems)}")
    print("账号库没问题" if not bad else f"{bad} 个账号需要处理")
    return 0 if not bad else 1


def show_sites(accounts_path: str) -> int:
    """打印站点登记表 + 每个站点的进度（有 key 数 / 今天签到数 / 有访问令牌数）。"""
    from datetime import datetime

    import tokenstore
    from runner import registered_users
    from store import read_rows

    try:
        total = len(load_accounts(accounts_path))
    except (OSError, ValueError):
        total = 0
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"登记表: {sites.config_path(BASE)}"
          f"{'（不存在，用的是内置默认）' if not sites.config_path(BASE).exists() else ''}")
    print(f"账号库共 {total} 个账号\n")
    if not sites.SITES:
        print("登记表里还没有站点。加一个：\n"
              "  python cli.py --add-site https://xxx.com/sign-up?aff=abc --site-name 显示名\n"
              "或者开界面（python gui.py）在「站点」页填。")
        return 0
    print(f"{'站点':<14}{'状态':<6}{'邀请码':<8}{'有 Key':<9}{'今日签到':<9}{'令牌':<8}"
          f"{'签到方式':<12}邀请链接")
    for s in sites.SITES:
        keys = len({r["user"] for r in read_rows(s.results_path(BASE))
                    if r["status"] == "ok" and r["key"]})
        reg = len(registered_users(s.results_path(BASE), s.sessions_dir(BASE),
                                  s.keys_path(BASE)))
        today_ok = len({r["user"] for r in read_rows(s.checkin_path(BASE))
                        if r["time"].startswith(today) and r["status"] in ("ok", "done")})
        state = "启用" if s.enabled else "停用"
        method = s.checkin_method if s.checkin_method != "auto" else (
            f"auto>{s.last_ok_method}" if s.last_ok_method else "auto")
        print(f"{s.name:<14}{state:<6}{s.aff or '-':<8}"
              f"{f'{keys}/{total}':<9}{f'{today_ok}/{reg}':<9}"
              f"{f'{tokenstore.count(s.tokens_path(BASE))}/{reg}':<8}"
              f"{method:<12}{s.signup_url}")
    print("\n签到方式：" + "；".join(f"{k}={v}" for k, v in sites.METHODS.items()))
    print("增删改站点（含签到方式）：界面「站点」页，或 python cli.py --add-site <邀请链接>")
    return 0


def add_site(url: str, name: str = "") -> int:
    """往登记表里加一个站点。已有同 key 的就改成新链接。"""
    site = sites.make(name, url)
    if not site.signup_url.startswith("http"):
        print(f"链接看着不对: {url}", file=sys.stderr)
        return 2
    current = sites.load(BASE)
    hit = next((i for i, s in enumerate(current) if s.key == site.key), None)
    if hit is None:
        current.append(site)
        what = "已新增"
    else:  # 保留原来的默认标记和显示名（没给新名字时）
        old = current[hit]
        current[hit] = sites.Site(key=old.key, name=name or old.name,
                                  signup_url=site.signup_url, default=old.default,
                                  enabled=old.enabled)
        what = "已更新"
    sites.save(current, BASE)
    s = sites.by_key(site.key)
    print(f"{what}站点 {s.name}（key={s.key} aff={s.aff or '无'}）")
    print(f"  注册日志 {s.results_path(BASE).name} / 签到日志 {s.checkin_path(BASE).name}"
          f" / 登录态 {s.sessions_dir(BASE).name}")
    print(f"开始跑: python cli.py --site {s.key}")
    return 0


def make_settings(args) -> Settings:
    return Settings(
        signup_url=args.url,
        timeout_ms=args.timeout * 1000,
        slow_mo_ms=args.slowmo,
        key_name=args.key_name,
        retries=max(1, args.retries),
        delay_between=args.delay,
        skip_done=not args.rerun_done,
        shot_dir=getattr(args, "_shot_dir", None) or Path(args.out).resolve().parent / "shots",
        proxy=args.proxy,
        channel=args.channel,
        use_session=not args.no_session,
        reuse_key=not args.new_key,
        spoof_device=not args.no_device_spoof,
        background=not args.no_background,
        site_password=getattr(args, "_password", ""),
        checkin_token=args.checkin_token.strip(),
        current_password=args.current_password,
        verify_password=args.verify_password,
        session_dir=getattr(args, "_session_dir", None),
        keys_path=getattr(args, "_keys_path", None),
        tokens_path=getattr(args, "_tokens_path", None),
        site_key=getattr(args, "_site_key", ""),
        checkin_method=getattr(args, "_checkin_method", ""),
        concurrency=max(1, int(getattr(args, "concurrency", 1) or 1)),
    )


def pick_accounts(args) -> list:
    accounts = load_accounts(args.accounts)
    if not args.include_disabled:
        accounts = [a for a in accounts if a.enabled]
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        accounts = [a for a in accounts if a.username in wanted]
    if args.start > 1:
        accounts = accounts[args.start - 1 :]
    if args.limit:
        accounts = accounts[: args.limit]
    return accounts


def mint_tokens(args, site: sites.Site) -> int:
    """给这个站点上还没有访问令牌的账号各生成一个（用现有登录态换，不开浏览器）。"""
    import httpapi
    import tokenstore
    from runner import registered_users

    tokens_path = site.tokens_path(BASE)
    have = set(tokenstore.load(tokens_path))
    reg = registered_users(site.results_path(BASE), site.sessions_dir(BASE),
                           site.keys_path(BASE))
    try:
        order = [a.username for a in load_accounts(args.accounts)]
    except (OSError, ValueError):
        order = sorted(reg)
    todo = [u for u in order if u in reg and u not in have]
    print(f"[{site.name}] 注册成功 {len(reg)} 个，已有令牌 {len(have)} 个，这次要生成 {len(todo)} 个")
    ok = fail = 0
    for i, user in enumerate(todo, start=1):
        client = httpapi.SiteClient(site.origin, user, site.sessions_dir(BASE), tokens_path)
        if not client.cookies:
            print(f"  [{i}/{len(todo)}] {user}: 没有登录态，跳过")
            fail += 1
            continue
        token = client.mint_token()
        if token:
            ok += 1
            print(f"  [{i}/{len(todo)}] {user}: 已生成（长度 {len(token)}）")
        else:
            fail += 1
            print(f"  [{i}/{len(todo)}] {user}: 生成失败（登录态可能过期了）")
    print(f"[{site.name}] 完成：成功 {ok} / 失败 {fail}，共存 "
          f"{tokenstore.count(tokens_path)} 个 → {tokens_path.relative_to(BASE)}")
    return 0 if fail == 0 else 1


def sync_quota(args, site: sites.Site) -> int:
    """把每个账号在站点上的剩余额度刷新一遍（走接口，不开浏览器）。"""
    import httpapi
    import keystore
    from runner import registered_users

    keys_path = site.keys_path(BASE)
    reg = registered_users(site.results_path(BASE), site.sessions_dir(BASE), keys_path)
    try:
        order = [a.username for a in load_accounts(args.accounts)]
    except (OSError, ValueError):
        order = sorted(reg)
    todo = [u for u in order if u in reg]
    print(f"[{site.name}] 注册成功 {len(reg)} 个，逐个查剩余额度")
    ok = fail = 0
    for i, user in enumerate(todo, start=1):
        client = httpapi.SiteClient(site.origin, user, site.sessions_dir(BASE),
                                    site.tokens_path(BASE))
        info = client.self_info() if client.ready else {}
        if info.get("quota") is None:
            fail += 1
            print(f"  [{i}/{len(todo)}] {user}: 查不到（登录态/令牌失效？）")
            continue
        keystore.set_quota(keys_path, user, info["quota"], used=info.get("used_quota"),
                           uid=info.get("id"))
        ok += 1
        print(f"  [{i}/{len(todo)}] {user}: {keystore.fmt_quota(info['quota'])}"
              f"（已用 {keystore.fmt_quota(info.get('used_quota'))}）")
    total = sum(v.get("quota", 0) or 0 for v in keystore.load(keys_path).values())
    print(f"[{site.name}] 完成：成功 {ok} / 失败 {fail}，"
          f"账上合计 {keystore.fmt_quota(total)}")
    return 0 if fail == 0 else 1


def sync_checkin(args, site: sites.Site) -> int:
    """把站点侧"今天已签到"的状态对齐到本地日志和登录态索引。

    **只读站点、绝不发签到请求**：站点说今天签过了才补一条 `done`，没签过的原样放着
    （留着做测试）。适合修补"探测脚本签上了但没写进日志"这类不一致。
    """
    from datetime import datetime

    import httpapi
    import keystore
    from runner import registered_users
    from session import record_meta
    from store import ResultStore, read_rows

    today = datetime.now().strftime("%Y-%m-%d")
    chk = site.checkin_path(BASE)
    logged = {row["user"] for row in read_rows(chk)
              if row["time"].startswith(today) and row["status"] in ("ok", "done")}
    reg = registered_users(site.results_path(BASE), site.sessions_dir(BASE),
                           site.keys_path(BASE))
    try:
        order = [a.username for a in load_accounts(args.accounts)]
    except (OSError, ValueError):
        order = sorted(reg)
    todo = [u for u in order if u in reg]
    store = ResultStore(chk, CHECKIN_HEADER)
    added = signed = unsigned = unknown = 0
    for user in todo:
        client = httpapi.SiteClient(site.origin, user, site.sessions_dir(BASE),
                                    site.tokens_path(BASE))
        stat = client.checkin_status() if client.ready else {}
        if not stat:
            unknown += 1
            continue
        if not stat.get("checked_in_today"):
            unsigned += 1
            continue
        signed += 1
        record_meta(user, site.sessions_dir(BASE), last_checkin=stat.get("day"))
        info = client.self_info()
        if info.get("quota") is not None:
            keystore.set_quota(site.keys_path(BASE), user, info["quota"],
                               used=info.get("used_quota"), uid=info.get("id"))
        if user in logged:                     # 日志里今天已经有记录了，不重复写
            continue
        award = f"+${(stat.get('award') or 0) / keystore.QUOTA_PER_USD:.2f}"
        store.append(user, "done", "",
                     f"今天已签到（累计 {stat.get('count')} 天 {award}）对齐站点状态")
        added += 1
    print(f"[{site.name}] 站点说已签 {signed} 个（补写 {added} 条 done）"
          f"，未签 {unsigned} 个（原样保留，没动）"
          f"{f'，查不到状态 {unknown} 个' if unknown else ''}")
    return 0


def run_helpers(args) -> int | None:
    """处理不需要开浏览器的命令；返回 None 表示不是这类命令。"""
    if args.mint_tokens:
        targets = args._targets or [sites.resolve(args.site, args.url)]
        return max(mint_tokens(args, s) for s in targets)

    if args.sync_quota:
        targets = args._targets or [sites.resolve(args.site, args.url)]
        return max(sync_quota(args, s) for s in targets)

    if args.sync_checkin:
        targets = args._targets or [sites.resolve(args.site, args.url)]
        return max(sync_checkin(args, s) for s in targets)

    if args.list_sites:
        return show_sites(args.accounts)

    if args.add_site:
        return add_site(args.add_site, args.site_name)

    if args.check:
        return check_accounts(args.accounts)

    if args.import_accounts:
        src = Path(args.import_accounts)
        incoming = load_accounts(src)
        current: list = []
        if Path(args.accounts).exists():
            try:
                current = load_accounts(args.accounts)
            except (OSError, ValueError):
                current = []
        merged, stats = merge_accounts(current, incoming, args.import_mode)
        save_accounts(args.accounts, merged)
        if args.import_mode == "merge":
            print(f"从 {src.name} 读到 {len(incoming)} 个：新增 {stats['added']}，"
                  f"重名跳过 {stats['skipped']}，原有 {stats['kept']} → 共 {len(merged)} 个")
        else:
            print(f"从 {src.name} 覆盖导入 {len(merged)} 个账号 → {Path(args.accounts).name}")
        return 0

    if args.export_accounts:
        dst = Path(args.export_accounts)
        accounts = load_accounts(args.accounts)
        dst.write_text(dump_accounts(accounts, detect_format(dst)) + "\n",
                       encoding="utf-8", newline="\n")
        print(f"已导出 {len(accounts)} 个账号 → {dst}（{detect_format(dst)} 格式）")
        return 0

    if args.export:
        keystore.sync_all(sites.SITES, BASE)  # 老日志里的 key 先收进 keystore
        picked = list(sites.SITES) if args.all_sites else [sites.resolve(args.site, args.url)]
        rows = export.collect_scoped(picked, BASE, args.accounts, args.group)
        text = export.render(rows, args.export, with_site=args.all_sites)
        if args.export_out:
            Path(args.export_out).write_text(text + "\n", encoding="utf-8", newline="\n")
            print(f"已写入 {args.export_out}")
        else:
            print(text)
        return 0

    if args.list_sessions:
        sessions = sess.list_sessions(args._session_dir)
        if not sessions:
            print("还没有保存的登录态")
        for s in sessions:
            extra = f" uid={s.uid}" if s.uid else ""
            extra += f" 最近签到 {s.last_checkin}" if s.last_checkin else ""
            print(f"{s.username:20} 保存于 {s.saved_text}{extra}")
        return 0

    if args.export_sessions:
        n = sess.export_bundle(args.export_sessions, base=args._session_dir)
        print(f"已打包 {n} 个登录态 → {args.export_sessions}（含会话 cookie，注意保管）")
        return 0

    if args.import_sessions:
        users = sess.import_bundle(args.import_sessions, base=args._session_dir,
                                   overwrite=(args.import_mode == "replace"))
        how = "覆盖" if args.import_mode == "replace" else "新增"
        print(f"已{how}导入 {len(users)} 个登录态: {', '.join(users) or '(空)'}")
        return 0

    if args.drop_session:
        ok = sess.drop_session(args.drop_session, args._session_dir)
        print(f"{'已删除' if ok else '没找到'} {args.drop_session} 的登录态")
        return 0 if ok else 1

    if args.open:
        acct = next((a for a in load_accounts(args.accounts)
                     if a.username == args.open), None)
        if acct is None:
            print(f"账号库里没有 {args.open}，登录态失效时无法自动重登", file=sys.stderr)
        return 0 if open_account(args.open, make_settings(args), acct=acct) else 1
    return None


def apply_site(args, site: sites.Site, honor_overrides: bool = True) -> sites.Site:
    """把随站点变化的值（url / 日志文件 / 登录态目录 / key 标记）填进 args。

    ``honor_overrides`` 为真时优先用命令行显式给的 --url/--out/--checkin-out；
    依次跑多个站点时必须为假，否则几个站点会写进同一个日志。
    """
    args.url = (args._url_opt if honor_overrides else "") or site.signup_url
    args.out = (args._out_opt if honor_overrides else "") or str(site.results_path(BASE))
    args.checkin_out = ((args._checkin_out_opt if honor_overrides else "")
                        or str(site.checkin_path(BASE)))
    args._session_dir = site.sessions_dir(BASE)
    args._meta_path = str(site.keymeta_path(BASE))
    args._keys_path = site.keys_path(BASE)
    args._tokens_path = site.tokens_path(BASE)
    args._shot_dir = site.shot_dir(BASE)
    args._site_key = site.key
    # 这次先用哪种签到方式：命令行显式给的最高，其次站点登记表里配的 / 上次成功的那种
    override = sites.normalize_method(getattr(args, "checkin_method", ""))
    args._checkin_method = (override if override in sites.METHOD_ORDER
                            else sites.first_method(site))
    return site


def resolve_site(args) -> sites.Site:
    """定站点。`--site all` 或逗号分隔的多个站点时，先按第一个填，真正跑的时候再逐站点覆盖。"""
    args._url_opt, args._out_opt = args.url, args.out
    args._checkin_out_opt = args.checkin_out
    args._targets = site_targets(args)          # 空列表 = 单站点（下面那个 site）
    args._all_sites_run = bool(args._targets)
    args._password = make_password(args.set_password) if args.set_password else ""
    if args._targets:
        site = args._targets[0]
    else:
        try:
            site = sites.resolve(args.site, args.url)
        except KeyError:
            raise SystemExit(f"没有叫 {args.site!r} 的站点"
                             f"（现有：{', '.join(sites.keys())}；all = 全部启用）") from None
    return apply_site(args, site)


def site_targets(args) -> list[sites.Site]:
    """`--site` 要跑哪些站点：`all` = 所有启用的；逗号分隔 = 按你写的顺序依次跑。

    返回空列表表示"就一个站点"，由 `sites.resolve` 去认（可能是 --url 带出来的临时站点）。
    """
    raw = (getattr(args, "site", "") or "").strip()
    if raw == ALL:
        return sites.enabled()
    if "," not in raw:
        return []
    picked: list[sites.Site] = []
    for name in (n.strip() for n in raw.split(",")):
        if not name:
            continue
        try:
            site = sites.by_key(name)
        except KeyError:
            raise SystemExit(f"--site 里的 {name!r} 不在站点登记表里"
                             f"（现有：{', '.join(sites.keys())}）") from None
        if site.key not in {s.key for s in picked}:
            picked.append(site)
    return picked


PASSWORD_HEADER = "# 时间\t账号\t状态\t保留列\t备注"
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_password(raw: str) -> str:
    """--set-password 的值：auto 就生成一个 16 位随机密码，否则原样用（至少 8 位）。"""
    import secrets

    if raw.strip().lower() == "auto":
        return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(16))
    return raw


def save_site_passwords(path: str, users: set[str], password: str) -> int:
    """把设好的站点密码写回账号库的 site_password 列，方便以后手工登录时查。"""
    accounts = load_accounts(path)
    hit = 0
    for a in accounts:
        if a.username in users:
            a.site_password = password
            hit += 1
    if hit:
        save_accounts(path, accounts)
    return hit


def run_one_site(args, site: sites.Site, single: bool) -> int:
    """在一个站点上跑注册 / 签到 / 改密码，返回失败个数。"""
    apply_site(args, site, honor_overrides=single)
    accounts = pick_accounts(args)
    if not accounts:
        print(f"[{site.name}] 没有可运行的账号")
        return 0

    st = make_settings(args)
    if args._password:
        out, header, what = str(site.password_log_path(BASE)), PASSWORD_HEADER, "改站点密码"
    elif args.checkin or args.checkin_assist:
        out, header = args.checkin_out, CHECKIN_HEADER
        what = "协助签到" if args.checkin_assist else "自动签到"
    else:
        out, header, what = args.out, None, "注册取 Key"
    print(f"[{site.name}] {what}：{len(accounts)} 个账号，结果写入 {out}")

    store = ResultStore(out, header) if header else ResultStore(out)
    if args._password:
        stats = run_set_password(accounts, st, store, register_results=args.out)
        picked = {a.username for a in accounts}
        ok_users = {row["user"] for row in read_rows(out)
                    if row["status"] == "ok" and row["user"] in picked}
        if ok_users:
            n = save_site_passwords(args.accounts, ok_users, args._password)
            print(f"[{site.name}] 已把新密码写进账号库的 site_password 列（{n} 个账号）")
            args._password_done = True
    elif args.checkin or args.checkin_assist:
        # 没在本站点注册成功的账号不签到（args.out 是本站点的注册日志）
        stats = run_checkin(accounts, st, store, register_results=args.out,
                            assist=args.checkin_assist)
    else:
        stats = run_batch(accounts, st, store)
    return stats.failed


def main(argv: list[str] | None = None) -> int:
    safe_console()
    args = build_parser().parse_args(argv)
    resolve_site(args)
    early = run_helpers(args)
    if early is not None:
        return early

    targets = args._targets or [sites.resolve(args.site, args.url)]
    targets = [s for s in targets if s.signup_url]
    if not targets:
        print("还没有可跑的站点。先加一个：\n"
              "  python cli.py --add-site https://xxx.com/sign-up?aff=abc --site-name 显示名\n"
              "或者开界面（python gui.py）在「站点」页填。", file=sys.stderr)
        return 2
    single = len(targets) == 1
    if not single:
        print(f"依次跑 {len(targets)} 个站点：{', '.join(s.name for s in targets)}")
        if args._out_opt or args._checkin_out_opt:
            print("（多站点时忽略 --out/--checkin-out，各站点写自己的日志）")

    failed = 0
    try:
        for site in targets:
            failed += run_one_site(args, site, single)
    except KeyboardInterrupt:
        print("\n已中断。已完成的账号会跳过，重跑同一条命令即可继续。")
        return 130
    if args._password:
        if getattr(args, "_password_done", False):
            print(f"\n新的站点登录密码：{args._password}")
            print("站点用户名 = GitHub 用户名；密码也写进了账号库的 site_password 列，"
                  "accounts.json 请保管好。")
        else:
            print("\n没有账号改成功（详见上面的原因）。OAuth 注册的账号站点上本来没有密码，"
                  "得先绑邮箱走「忘记密码」拿到第一个密码，之后才能用 --set-password 改。")
    elif not (args.checkin or args.checkin_assist):
        scope = "--all-sites" if not single else f"--site {targets[0].key}"
        print(f"导出结果: python cli.py --export table {scope}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
