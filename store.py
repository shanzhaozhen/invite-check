"""账号库与结果的读写。

账号库以 `accounts.json` 为主格式（结构清晰、能带启用开关和备注），同时兼容早期的
`账号----密码----密钥` 纯文本，并支持 json / csv / txt 三种格式互相导入导出。
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

SEP = "----"
RESULT_HEADER = "# 时间\t账号\t状态\tAPI Key\t备注"
JSON_VERSION = 1
FIELDS = ("username", "password", "totp_secret", "email", "email_password",
          "site_password", "enabled", "note")


@dataclass
class Account:
    """账号库中的一条记录。

    ``password`` / ``totp_secret`` 是 GitHub 的；``site_password`` 是给邀请站点自己设的
    登录密码（GitHub 登不了时的备用入口，站点用户名就是 GitHub 用户名）。
    """

    username: str
    password: str
    totp_secret: str
    email: str = ""
    email_password: str = ""
    site_password: str = ""
    enabled: bool = True
    note: str = ""
    line_no: int = 0

    def __str__(self) -> str:  # 日志里只显示账号，不泄露密码
        return self.username

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("line_no", None)
        return d


def _from_row(row: dict, line_no: int = 0) -> Account:
    """把一行 dict（json / csv 都归到这里）变成 Account。"""
    def pick(*names: str) -> str:
        for n in names:
            if row.get(n) not in (None, ""):
                return str(row[n]).strip()
        return ""

    enabled = row.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "停用", "否")
    return Account(
        username=pick("username", "account", "账号", "user"),
        password=pick("password", "密码", "pass"),
        totp_secret=pick("totp_secret", "totp", "secret", "密钥", "2fa"),
        email=pick("email", "邮箱"),
        email_password=pick("email_password", "邮密", "邮箱密码"),
        site_password=pick("site_password", "站点密码", "site_pass"),
        enabled=bool(enabled),
        note=pick("note", "备注"),
        line_no=line_no,
    )


def parse_text(text: str) -> list[Account]:
    """解析 `账号----密码----密钥（----邮箱----邮密）`。开头带 `-` 或 `!` 视为停用。"""
    accounts: list[Account] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        enabled = True
        if line[0] in "-!":
            enabled, line = False, line[1:].strip()
        parts = [p.strip() for p in line.split(SEP)]
        if len(parts) < 3:
            raise ValueError(f"第 {line_no} 行字段不足（至少 账号{SEP}密码{SEP}密钥）: {line}")
        accounts.append(
            Account(
                username=parts[0], password=parts[1], totp_secret=parts[2],
                email=parts[3] if len(parts) > 3 else "",
                email_password=parts[4] if len(parts) > 4 else "",
                enabled=enabled, line_no=line_no,
            )
        )
    return accounts


def parse_json(text: str) -> list[Account]:
    data = json.loads(text)
    rows = data.get("accounts", []) if isinstance(data, dict) else data
    return [_from_row(r, i) for i, r in enumerate(rows, start=1) if isinstance(r, dict)]


def parse_csv(text: str) -> list[Account]:
    rows = list(csv.DictReader(io.StringIO(text)))
    return [_from_row(r, i) for i, r in enumerate(rows, start=2)]


def detect_format(path: str | os.PathLike) -> str:
    """按后缀判断格式：json / csv / txt。"""
    suffix = Path(path).suffix.lower()
    return {".json": "json", ".csv": "csv"}.get(suffix, "txt")


def parse_accounts(text: str, fmt: str) -> list[Account]:
    return {"json": parse_json, "csv": parse_csv, "txt": parse_text}[fmt](text)


def load_accounts(path: str | os.PathLike) -> list[Account]:
    """读账号库。

    指定的是 accounts.json 但文件不存在时，自动把同目录的 accounts.txt 迁移过来，
    老用户不用手动转换。两个都没有就返回空列表——第一次跑（尤其是打包出去的 exe）
    账号库本来就是空的，不该报错。
    """
    p = Path(path)
    if not p.exists() and p.suffix.lower() == ".json":
        legacy = p.with_suffix(".txt")
        if legacy.exists():
            accounts = parse_text(legacy.read_text(encoding="utf-8-sig"))
            save_accounts(p, accounts)
            return accounts
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8-sig")
    return parse_accounts(text, detect_format(p))


def dump_accounts(accounts: Sequence[Account], fmt: str) -> str:
    """把账号列表序列化成指定格式的文本。"""
    if fmt == "json":
        payload = {
            "version": JSON_VERSION,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": [a.to_dict() for a in accounts],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(FIELDS), lineterminator="\n")
        writer.writeheader()
        for a in accounts:
            writer.writerow({k: a.to_dict()[k] for k in FIELDS})
        return buf.getvalue().rstrip("\n")

    lines = [
        "# 账号库（纯文本格式）：账号----密码----密钥（----邮箱----邮密）",
        "# 行首加 - 表示停用；# 开头为注释。主格式是 accounts.json，这里是导出副本。",
        "# 注意：站点密码（site_password）这一列纯文本格式装不下，只在 json / csv 里有。",
        "",
    ]
    for a in accounts:
        fields = [a.username, a.password, a.totp_secret]
        if a.email or a.email_password:
            fields += [a.email, a.email_password]
        lines.append(("" if a.enabled else "-") + SEP.join(f.strip() for f in fields))
    return "\n".join(lines)


def save_accounts(path: str | os.PathLike, accounts: Sequence[Account], fmt: str = "") -> None:
    """写账号库；先写临时文件再替换，避免写坏原文件。"""
    p = Path(path)
    text = dump_accounts(accounts, fmt or detect_format(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, p)


class ResultStore:
    """把结果追加写入制表符分隔的文本文件，随写随 flush，中断也不丢数据。"""

    def __init__(self, path: str | os.PathLike, header: str = RESULT_HEADER) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(header + "\n")

    def done_usernames(self, statuses: Iterable[str] = ("ok",)) -> set[str]:
        """已经成功过的账号，用于断点续跑时跳过。"""
        want = set(statuses)
        return {
            row["user"] for row in read_rows(self.path)
            if row["status"] in want and (row["key"] or row["status"] != "ok")
        }

    def append(self, username: str, status: str, api_key: str = "", note: str = "") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = "\t".join(
            [stamp, username, status, api_key, note.replace("\t", " ").replace("\n", " ")]
        )
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(row + "\n")
            fh.flush()


def merge_accounts(
    existing: Sequence[Account], incoming: Sequence[Account], mode: str = "merge"
) -> tuple[list[Account], dict[str, int]]:
    """合并两份账号列表。

    ``mode="merge"``：保留现有的，只把没见过的账号追加到后面（重名的跳过，不动原来的密码）。
    ``mode="replace"``：直接用导入的那份。
    返回 (合并结果, {"added": .., "skipped": .., "kept": ..})。
    """
    if mode == "replace":
        return list(incoming), {"added": len(incoming), "skipped": 0, "kept": 0}
    have = {a.username for a in existing}
    added = [a for a in incoming if a.username not in have]
    return (
        list(existing) + added,
        {"added": len(added), "skipped": len(incoming) - len(added), "kept": len(existing)},
    )


def read_rows(path: str | os.PathLike) -> list[dict[str, str]]:
    """读结果文件里的所有数据行。"""
    rows: list[dict[str, str]] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            cols = line.rstrip("\n").split("\t")
            cols += [""] * (5 - len(cols))
            rows.append(
                {"time": cols[0], "user": cols[1], "status": cols[2],
                 "key": cols[3], "note": cols[4]}
            )
    return rows


@dataclass
class RunStats:
    """一次批量运行的计数。"""

    ok: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.ok + self.failed + self.skipped
