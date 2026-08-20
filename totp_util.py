"""TOTP 验证码生成（RFC 6238），纯标准库实现。

与 https://substar.cc 这类在线工具算法一致：输入 Base32 密钥，输出 6 位码。
本地计算避免了额外的网络请求和被限流，同时不会把密钥发到第三方站点。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def _normalize(secret: str) -> bytes:
    """把用户粘贴的密钥整理成 Base32 字节串。

    容忍小写、空格、连字符以及缺失的 '=' 填充。
    """
    cleaned = secret.strip().replace(" ", "").replace("-", "").upper()
    if not cleaned:
        raise ValueError("TOTP 密钥为空")
    padding = (-len(cleaned)) % 8
    try:
        return base64.b32decode(cleaned + "=" * padding, casefold=True)
    except Exception as exc:  # noqa: BLE001 - 统一成可读报错
        raise ValueError(f"不是合法的 Base32 密钥: {secret!r}") from exc


def totp(secret: str, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    """返回指定时刻的 TOTP 验证码，默认取当前时间。"""
    key = _normalize(secret)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def seconds_left(period: int = 30, at: float | None = None) -> float:
    """当前验证码还有多少秒有效，用于避免在窗口边缘提交。"""
    now = time.time() if at is None else at
    return period - (now % period)


def fresh_totp(secret: str, min_validity: float = 3.0, period: int = 30,
               avoid: str = "") -> str:
    """取一个至少还剩 ``min_validity`` 秒的验证码，必要时先等到下一个窗口。

    ``avoid`` 给上次提交过的那个码：算出来一样就再等一个窗口——GitHub 会拒绝**重复使用**
    同一个码（"already been used or is too old"），所以重试时必须换一个新的。
    """
    if seconds_left(period) < min_validity:
        time.sleep(seconds_left(period) + 0.3)
    code = totp(secret, period=period)
    if avoid and code == avoid:
        time.sleep(seconds_left(period) + 0.3)
        code = totp(secret, period=period)
    return code


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        print(f"{arg} -> {totp(arg)}  (剩余 {seconds_left():.0f}s)")
