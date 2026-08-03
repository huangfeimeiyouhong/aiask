#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据层 —— SQLite 账号体系、会话、后厨管家凭据存储。

设计：
- users 表：应用账号（username/password_hash/role），以及绑定的后厨管家凭据（加密存储）。
- sessions 表：服务端会话，cookie 只存随机 sid，避免在前端暴露任何敏感信息。
- 每个函数独立打开连接，配合 ThreadingHTTPServer 天然线程安全。
"""

import os
import sqlite3
import hashlib
import secrets
import datetime

from config import SETTINGS
import crypto

DB_PATH = os.path.join(SETTINGS["DATA_DIR"], "aiqa.db")


def _conn():
    os.makedirs(SETTINGS["DATA_DIR"], exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = _conn()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            pw_hash       TEXT NOT NULL,
            pw_salt       TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            hcg_username  TEXT,
            hcg_enc       TEXT,
            hcg_bound     INTEGER NOT NULL DEFAULT 0,
            disabled      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            sid        TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """)
        c.commit()
    finally:
        c.close()


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), 100_000).hex()


def create_user(username: str, password: str, role: str = "user") -> (bool, str):
    c = _conn()
    try:
        salt = secrets.token_hex(16)
        ph = _hash_pw(password, salt)
        try:
            c.execute(
                "INSERT INTO users (username, pw_hash, pw_salt, role, created_at) "
                "VALUES (?,?,?,?,?)",
                (username, ph, salt, role, _now()))
            c.commit()
            return True, ""
        except sqlite3.IntegrityError:
            return False, "用户名已存在"
    finally:
        c.close()


def verify_user(username: str, password: str):
    c = _conn()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        if row["disabled"]:
            return None
        if row["pw_hash"] != _hash_pw(password, row["pw_salt"]):
            return None
        return dict(row)
    finally:
        c.close()


def get_user(username: str):
    c = _conn()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def set_hcg(username: str, hcg_username: str, hcg_password: str) -> bool:
    c = _conn()
    try:
        enc = crypto.encrypt(f"{hcg_username}|{hcg_password}")
        c.execute(
            "UPDATE users SET hcg_username=?, hcg_enc=?, hcg_bound=1 WHERE username=?",
            (hcg_username, enc, username))
        c.commit()
        return c.total_changes > 0
    finally:
        c.close()


def get_hcg(username: str):
    """返回 (hcg_username, hcg_password) 解密后的明文，未绑定返回 (None, None)。"""
    u = get_user(username)
    if not u or not u.get("hcg_bound") or not u.get("hcg_enc"):
        return None, None
    try:
        plain = crypto.decrypt(u["hcg_enc"])
        u_name, _, p_w = plain.partition("|")
        return u_name, p_w
    except Exception:
        return None, None


def list_users():
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id, username, role, hcg_username, hcg_bound, disabled, created_at "
            "FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def set_disabled(username: str, disabled: bool) -> bool:
    c = _conn()
    try:
        c.execute("UPDATE users SET disabled=? WHERE username=? AND role<>'admin'",
                  (1 if disabled else 0, username))
        c.commit()
        return c.total_changes > 0
    finally:
        c.close()


def delete_user(username: str) -> bool:
    c = _conn()
    try:
        c.execute("DELETE FROM users WHERE username=? AND role<>'admin'", (username,))
        c.commit()
        return c.total_changes > 0
    finally:
        c.close()


# ---- 会话 ----
def create_session(username: str, ttl_hours: int = 12) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.datetime.now()
    expires = now + datetime.timedelta(hours=ttl_hours)
    c = _conn()
    try:
        c.execute("INSERT INTO sessions (sid, username, created_at, expires_at) VALUES (?,?,?,?)",
                  (sid, username, now.isoformat(), expires.isoformat()))
        c.commit()
    finally:
        c.close()
    return sid


def get_session_username(sid: str):
    if not sid:
        return None
    c = _conn()
    try:
        row = c.execute("SELECT username, expires_at FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not row:
            return None
        exp = datetime.datetime.fromisoformat(row["expires_at"])
        if exp < datetime.datetime.now():
            c.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            c.commit()
            return None
        return row["username"]
    finally:
        c.close()


def delete_session(sid: str):
    c = _conn()
    try:
        c.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        c.commit()
    finally:
        c.close()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def seed_admin():
    """首次启动若没有 admin 账号，创建一个默认 admin（账号/密码 admin / admin123!）。"""
    c = _conn()
    try:
        n = c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
        if n == 0:
            ok, msg = create_user("admin", "admin123!", "admin")
            return ok
        return True
    finally:
        c.close()


if __name__ == "__main__":
    init_db()
    seed_admin()
    print("DB initialized at", DB_PATH)
