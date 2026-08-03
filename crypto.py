#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对称加密 —— 对「后厨管家账号/密码」做 at-rest 加密，零外部依赖。

说明：
- 采用 PBKDF2 派生密钥 + HMAC-SHA256 密钥流做 XOR（一种轻量流密码）。
- 这是「静态数据加密」级别，用于防止数据库/磁盘文件明文泄露凭据；
  部署环境若已安装 cryptography 库，可在 config 中启用 Fernet 以获得更强保障。
- 加密结果格式：base64( salt[16] || ciphertext )。
"""

import os
import base64
import hmac
import hashlib

from config import SETTINGS


def _derive_key(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 200_000, 32)


def _keystream(key: bytes, length: int) -> bytes:
    out = b""
    i = 0
    while len(out) < length:
        out += hmac.new(key, i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return out[:length]


def encrypt(plaintext: str, secret: str = None) -> str:
    secret = secret or SETTINGS["APP_SECRET"]
    salt = os.urandom(16)
    key = _derive_key(secret, salt)
    data = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, len(data))))
    return base64.b64encode(salt + ct).decode("ascii")


def decrypt(token: str, secret: str = None) -> str:
    secret = secret or SETTINGS["APP_SECRET"]
    raw = base64.b64decode(token)
    salt, ct = raw[:16], raw[16:]
    key = _derive_key(secret, salt)
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(key, len(ct))))
    return pt.decode("utf-8")


if __name__ == "__main__":
    t = encrypt("at0001|at123456@")
    print("enc =", t)
    print("dec =", decrypt(t))
