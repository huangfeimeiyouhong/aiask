# -*- coding: utf-8 -*-
"""SSO 路由回归测试（mock 后厨管家鉴权，不走外网）。

覆盖：
  1) 合法 token   -> 302 + Set-Cookie + Location:/
  2) token 带 m_ 前缀（后厨管家前端可能直传请求头形式） -> 自动剥离并成功
  3) token 无效   -> 302 /?sso=invalid
  4) 缺 token     -> 302 /?sso=missing
  5) 开放重定向   -> redirect=//evil.com 被拦截，回退 /
  6) SSO 会话可用 -> 带 Cookie 请求 /api/me 返回登录态
"""
import http.client
import threading
import time
from http.server import ThreadingHTTPServer

import app
import hcg_client

PORT = 8099
VALID = "a" * 64
_seen_tokens = []


def _fake_verify(self):
    _seen_tokens.append(self.token)
    return self.token == VALID


hcg_client.HCGClient.verify_token = _fake_verify


def _get(path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    headers = {"Cookie": cookie} if cookie else {}
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    body = r.read().decode("utf-8", "ignore")
    out = (r.status, dict(r.getheaders()), body)
    conn.close()
    return out


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(("[PASS] " if cond else "[FAIL] ") + name + ("  " + extra if extra else ""))

    # 1) 合法 token
    st, h, _ = _get(f"/sso?token={VALID}&username=at0001")
    cookie = h.get("Set-Cookie", "")
    check("合法 token -> 302 且种 Cookie 跳主页",
          st == 302 and h.get("Location") == "/" and "aiqa_sid=" in cookie,
          f"status={st} loc={h.get('Location')}")
    sid = cookie.split(";")[0] if cookie else ""

    # 2) 带 m_ 前缀
    _seen_tokens.clear()
    st2, h2, _ = _get(f"/sso?token=m_{VALID}&username=at0001")
    check("m_ 前缀自动剥离 -> 登录成功",
          st2 == 302 and h2.get("Location") == "/" and _seen_tokens[-1] == VALID,
          f"透传token={_seen_tokens[-1][:6]}...")

    # 3) 无效 token
    st3, h3, _ = _get("/sso?token=" + "b" * 64)
    check("无效 token -> /?sso=invalid",
          st3 == 302 and h3.get("Location") == "/?sso=invalid", f"loc={h3.get('Location')}")

    # 4) 缺 token
    st4, h4, _ = _get("/sso")
    check("缺 token -> /?sso=missing",
          st4 == 302 and h4.get("Location") == "/?sso=missing", f"loc={h4.get('Location')}")

    # 5) 开放重定向拦截
    st5, h5, _ = _get(f"/sso?token={VALID}&redirect=//evil.com")
    check("开放重定向拦截 -> 回退 /",
          st5 == 302 and h5.get("Location") == "/", f"loc={h5.get('Location')}")

    # 6) 会话可用
    _, _, body = _get("/api/me", cookie=sid)
    check("SSO 会话可用 /api/me", '"success": true' in body.replace(" ", " "), body[:80])
    _, _, body2 = _get("/api/me")
    check("无 Cookie 未登录", '"success": false' in body2, body2[:80])

    httpd.shutdown()
    print("\n结果:", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
