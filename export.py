"""把拿到的 API Key 导出成好用的格式。

数据源是 `data/keys/<站点>.json`（keystore，取到 key 时就写进去了），再叠上
`data/keys-meta/<站点>.json` 里你标的分组和使用备注。日志 `logs/<站点>/results.txt`
只当流水账，不再当数据源（老日志里的 key 会由 keystore.sync_from_log 收进来）。

    python export.py                      # 默认站点，表格
    python export.py --all-sites          # 所有站点，多一列站点
    python export.py --format keys        # 只输出 api-key，一行一个
    python export.py --format csv --out keys.csv
    python export.py --group 待用          # 只导出某个分组
"""

from __future__ import annotations

import argparse
import csv
import io
import unicodedata

import keymeta
import keystore
import sites
from paths import ACCOUNTS_PATH, APP_DIR
from store import load_accounts

FORMATS = ("table", "keys", "csv")


def _width(text: str) -> int:
    """按终端显示宽度算长度，中文算两格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _order_from(accounts: str) -> list[str] | None:
    if not accounts:
        return None
    try:
        return [a.username for a in load_accounts(accounts)]
    except (OSError, ValueError):
        return None


def collect_scoped(site_objs, base=None, accounts: str = "",
                   group: str = "") -> list[tuple[str, ...]]:
    """跨一个或多个站点收集 key。

    返回 [(账号, api_key, 时间, 分组, 备注, 站点, 剩余额度)]，
    按站点登记顺序、每站内按账号库顺序。
    """
    base = base or APP_DIR
    order = _order_from(accounts)
    rows: list[tuple[str, ...]] = []
    for s in site_objs:
        meta = keymeta.load(str(s.keymeta_path(base)))
        for user, key, when, quota in keystore.rows(s.keys_path(base), order):
            m = meta.get(user, {})
            g, n = m.get("group", ""), m.get("note", "")
            if not group or g == group:
                rows.append((user, key, when, g, n, s.name, quota))
    return rows


def collect(site, base=None, accounts: str = "", group: str = "") -> list[tuple[str, ...]]:
    """单个站点的 key（返回值同 collect_scoped）。"""
    return collect_scoped([site], base, accounts, group)


def render(rows: list[tuple[str, ...]], fmt: str = "table", with_site: bool = False) -> str:
    """rows 是 collect_scoped 给的 [(账号, key, 时间, 分组, 备注, 站点, 剩余额度)]。

    ``with_site=True`` 时表格/CSV 多带一列站点。「剩余额度」是上次看到的值
    （注册取 key / 签到时记的），不是实时查的。
    """
    if not rows:
        return "（没有符合条件的记录）"
    rows = [tuple(r) + ("",) * (7 - len(r)) for r in rows]     # 兼容老的 5/6 列调用
    if fmt == "keys":
        return "\n".join(row[1] for row in rows)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if with_site:
            writer.writerow(["account", "site", "group", "api_key", "quota", "note", "time"])
            for r in rows:
                writer.writerow([r[0], r[5], r[3], r[1], r[6], r[4], r[2]])
        else:
            writer.writerow(["account", "group", "api_key", "quota", "note", "time"])
            for r in rows:
                writer.writerow([r[0], r[3], r[1], r[6], r[4], r[2]])
        return buf.getvalue().rstrip("\n")

    if with_site:
        cols = ("账号", "站点", "分组", "API Key", "剩余额度", "备注")
        picks = [(r[0], r[5], r[3], r[1], r[6], r[4]) for r in rows]
    else:
        cols = ("账号", "分组", "API Key", "剩余额度", "备注")
        picks = [(r[0], r[3], r[1], r[6], r[4]) for r in rows]
    widths = [
        max(_width(cols[i]), *(_width(p[i]) for p in picks)) for i in range(len(cols))
    ]
    head = "  ".join(_pad(c, w) for c, w in zip(cols, widths)).rstrip()
    sep = "-" * _width(head)
    body = ["  ".join(_pad(v, w) for v, w in zip(p, widths)).rstrip() for p in picks]
    return "\n".join([head, sep, *body, sep, f"共 {len(rows)} 个"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="导出 API Key")
    p.add_argument("--site", default="", choices=sites.keys(),
                   help=f"导哪个站点：{' / '.join(sites.keys())}（默认第一个）")
    p.add_argument("--accounts", default=str(ACCOUNTS_PATH), help="账号库，用来决定排序")
    p.add_argument("--format", default="table", choices=FORMATS, help="输出格式")
    p.add_argument("--group", default="", help=f"只导出某个分组：{' / '.join(keymeta.GROUPS)}")
    p.add_argument("--all-sites", action="store_true",
                   help="导出所有站点的 key（表格/CSV 会多一列站点）")
    p.add_argument("--out", default="", help="写入文件，不给就打印到屏幕")
    args = p.parse_args(argv)

    keystore.sync_all(sites.SITES, APP_DIR)  # 先把日志里的历史 key 收进 keystore
    picked = list(sites.SITES) if args.all_sites else [sites.resolve(args.site)]
    rows = collect_scoped(picked, APP_DIR, args.accounts, args.group)
    text = render(rows, args.format, with_site=args.all_sites)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
