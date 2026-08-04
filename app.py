#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后厨管家 AI 问数 · 独立部署服务（单层登录）

特性：
- 直接用「后厨管家账号」登录：输入后厨管家用户名/密码 → 调用后厨管家登录接口校验，
  校验通过后建立会话（服务端保存 token / dataVersion）。无需额外的应用账号体系。
- 天然多用户：每个后厨管家账号各自登录、各自取其组织/仓库权限下的数据，互不越权。
- 安全会话：服务端内存会话 + HttpOnly Cookie；多线程并发（ThreadingHTTPServer）。
- 问数底层复用 semantic_tools / agent / hunyuan（混元或本地 Mock）。
- 结果缓存：同一用户相同维度的聚合结果缓存 10 分钟，缓解接口翻页压力。
- 零外部依赖（纯标准库），`python app.py` 即可运行。

启动：python app.py   （可用 PORT / HOST 环境变量覆盖）
"""

import os
import sys
import json
import time
import threading
import secrets
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from hcg_client import HCGClient
from hunyuan import get_llm
import semantic_tools as st
import agent as agent_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
COOKIE_NAME = "aiqa_sid"

# ---------------------------------------------------------------------------
# 会话（内存）与缓存
# ---------------------------------------------------------------------------
_sessions = {}               # sid -> {username, token, dataVersion, expire}
_sess_lock = threading.Lock()
SESSION_TTL = 8 * 3600       # 8 小时


def _create_session(username, token, data_version):
    sid = secrets.token_hex(16)
    with _sess_lock:
        _sessions[sid] = {"username": username, "token": token,
                          "dataVersion": data_version,
                          "history": [],            # 多轮对话历史：[{q, a}, ...]
                          "expire": time.time() + SESSION_TTL}
    return sid


def _get_session(sid):
    if not sid:
        return None
    with _sess_lock:
        s = _sessions.get(sid)
        if not s:
            return None
        if s["expire"] < time.time():
            _sessions.pop(sid, None)
            return None
        # 续期
        s["expire"] = time.time() + SESSION_TTL
        return s


def _destroy_session(sid):
    if not sid:
        return
    with _sess_lock:
        _sessions.pop(sid, None)


_result_cache = {}           # key -> (expire_ts, result)
_result_lock = threading.Lock()
RESULT_TTL = 600             # 10 分钟


def _cached_call_tool(client, name, args):
    """包装 semantic_tools.call_tool，按 (用户, 工具, 参数) 缓存聚合结果。"""
    username = getattr(client, "username", "?")
    key = (username, name, tuple(sorted((k, str(v)) for k, v in args.items())))
    now = time.time()
    with _result_lock:
        hit = _result_cache.get(key)
        if hit and hit[0] > now:
            return hit[1], None
    res, err = st.call_tool(client, name, args)
    with _result_lock:
        _result_cache[key] = (now + RESULT_TTL, res)
    return res, err


# 让 agent 走带缓存的执行器
agent_mod.call_tool = _cached_call_tool


def get_client(sess):
    """从会话重建 HCGClient（直接用会话里的 token，无需重新登录）。"""
    client = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"],
                       token=sess["token"], data_version=sess.get("dataVersion"))
    client.username = sess["username"]
    return client


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "HCG-AI-QA/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, ctype="text/plain; charset=utf-8", cache=None):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if cache:
            self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def _cookie_sid(self):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            part = part.strip()
            if part.startswith(COOKIE_NAME + "="):
                return part.split("=", 1)[1]
        return None

    def _set_cookie(self, sid):
        flags = "HttpOnly; Path=/; SameSite=Lax"
        if config.SETTINGS["SESSION_SECURE"]:
            flags += "; Secure"
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={sid}; {flags}")

    def _clear_cookie(self):
        flags = "HttpOnly; Path=/; SameSite=Lax; Max-Age=0"
        if config.SETTINGS["SESSION_SECURE"]:
            flags += "; Secure"
        self.send_header("Set-Cookie", f"{COOKIE_NAME}=; {flags}")

    def _redirect(self, loc: str):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    # ---- 静态资源（本地化 ECharts 等，避免依赖外网 CDN）----
    def _serve_static(self, path):
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(BASE_DIR, rel))
        # 防目录穿越：必须在 BASE_DIR 内且为真实文件
        if not full.startswith(BASE_DIR) or not os.path.isfile(full):
            self._send_text("not found", 404)
            return
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        try:
            with open(full, "rb") as f:
                # 静态资源（ECharts 等）长期缓存，减少重复下载
                cache = "max-age=86400" if full.endswith((".js", ".css")) else None
                self._send_text(f.read(), ctype=ctype, cache=cache)
        except Exception:
            self._send_text("not found", 404)

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send_text(f.read(), ctype="text/html; charset=utf-8",
                                    cache="no-store, no-cache, must-revalidate, max-age=0")
            except Exception:
                self._send_text("index.html not found", 500)
            return
        if path == "/sso":
            self._api_sso()
            return
        if path == "/api/me":
            self._api_me()
            return
        if path == "/api/alerts":
            self._api_alerts()
            return
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if path.startswith("/libs/") or path.startswith("/static/"):
            self._serve_static(path)
            return
        self._send_json({"error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            self._api_login()
        elif path == "/api/logout":
            self._api_logout()
        elif path == "/api/ask":
            self._api_ask()
        elif path == "/api/clear":
            self._api_clear()
        else:
            self._send_json({"error": "not found"}, 404)

    # ---- 路由实现 ----
    def _api_sso(self):
        """单点登录回调：后厨管家登录后跳转过来，URL 带 token（及 username / dataVersion）。

        校验 token 有效性后建立会话并种 HttpOnly Cookie，再 302 跳到问数主页（已登录态）。
        校验失败或缺少 token 则跳回登录页，并带 sso=invalid / sso=missing 标记供前端提示。

        安全要点：
          - token 不直接写入会话之外的任何存储；仅在服务端用其向后厨管家接口发请求。
          - 通过 verify_token() 实测接口成功来确认 token 有效，杜绝伪造会话。
          - redirect 仅允许同域相对路径，防开放重定向。
          - 成功后 302 到主页，token 不再停留在地址栏。
        """
        q = parse_qs(urlparse(self.path).query)
        token = (q.get("token") or [None])[0]
        username = (q.get("username") or [None])[0] or ""
        data_version = (q.get("dataVersion") or [None])[0]
        target = (q.get("redirect") or ["/"])[0] or "/"
        # 防开放重定向：仅允许同域相对路径（不以 // 或 scheme:// 开头）
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        if not token:
            self._redirect("/?sso=missing")
            return
        # 容错：后厨管家前端可能直接把请求头形式的 "m_<token>" 传过来，
        # 而 HCGClient 内部会自行拼接 "m_" 前缀，此处剥掉避免双重前缀。
        if token.startswith("m_"):
            token = token[2:]
        client = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"],
                           token=token, data_version=data_version)
        if not client.verify_token():
            self._redirect("/?sso=invalid")
            return
        sid = _create_session(username or "sso-user", token, data_version)
        self.send_response(302)
        self._set_cookie(sid)
        self.send_header("Location", target)
        self.end_headers()

    def _api_login(self):
        d = self._read_json()
        uname = (d.get("username") or "").strip()
        pwd = d.get("password") or ""
        if not uname or not pwd:
            self._send_json({"success": False, "message": "请输入账号和密码"}, 400)
            return
        # 直接用后厨管家账号登录校验
        client = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"])
        try:
            r = client.login(uname, pwd)
        except Exception as e:
            self._send_json({"success": False, "message": f"连接后厨管家失败: {e}"}, 502)
            return
        if not r.get("success"):
            self._send_json({"success": False,
                             "message": r.get("message") or "账号或密码错误"}, 401)
            return
        data = r.get("data", {})
        sid = _create_session(uname, data.get("token"), data.get("dataVersion"))
        self.send_response(200)
        self._set_cookie(sid)
        self._send_json({"success": True, "username": uname})

    def _api_logout(self):
        _destroy_session(self._cookie_sid())
        self.send_response(200)
        self._clear_cookie()
        self._send_json({"success": True})

    def _api_me(self):
        s = _get_session(self._cookie_sid())
        if not s:
            self._send_json({"success": False, "message": "未登录"}, 401)
            return
        self._send_json({"success": True, "username": s["username"]})

    def _api_alerts(self):
        """主动推送：聚合『待整改 / 过期』类预警，供前端轮询弹窗。

        聚合三源（任意一源失败不影响其他源）：
          - warning_center：综合预警待整改数（status=0）
          - stock_warning：库存已过期 / 临期预警数
          - health_certificate：健康证已过期 / 即将到期数
        复用带缓存的执行器（10 分钟 TTL），与前端 10 分钟轮询频率对齐。
        """
        s = _get_session(self._cookie_sid())
        if not s:
            self._send_json({"success": False, "message": "未登录"}, 401)
            return
        client = get_client(s)
        alerts = []
        total_urgent = 0   # 待整改 + 已过期 视为紧急
        total_warn = 0     # 临期/即将到期 视为提醒

        # 1) 综合预警中心 · 待整改
        try:
            wc, err = _cached_call_tool(client, "warning_center", {})
            if not err and wc:
                n = (wc.get("status_agg") or {}).get("待整改", 0)
                if n:
                    total_urgent += n
                    alerts.append({"type": "warning_center", "label": "综合预警·待整改",
                                   "count": n, "severity": "urgent",
                                   "detail": "证照到期/库存过期/巡检不符合项等待整改事项",
                                   "question": "现在有哪些待整改预警？按分类汇总并列出明细"})
        except Exception:
            pass

        # 2) 库存预警 · 过期 / 临期
        try:
            sw, err = _cached_call_tool(client, "stock_warning", {})
            if not err and sw:
                out = sw.get("outdated_count", 0) or 0
                war = sw.get("warning_count", 0) or 0
                if out:
                    total_urgent += out
                    alerts.append({"type": "stock_outdated", "label": "库存已过期",
                                   "count": out, "severity": "urgent",
                                   "detail": "已过期库存商品，须立即下架处理",
                                   "question": "库存有哪些已过期商品？列出明细"})
                if war:
                    total_warn += war
                    alerts.append({"type": "stock_warn", "label": "库存临期预警",
                                   "count": war, "severity": "warn",
                                   "detail": "临近保质期，建议关注周转",
                                   "question": "库存有哪些临期预警商品？"})
        except Exception:
            pass

        # 3) 健康证合规预警 · 过期 / 即将到期
        try:
            hc, err = _cached_call_tool(client, "health_certificate", {})
            if not err and hc:
                dist = hc.get("distribution", {})
                od = dist.get("overdueQty", 0) or 0
                ex = dist.get("aboutToExpireQty", 0) or 0
                if od:
                    total_urgent += od
                    alerts.append({"type": "hc_overdue", "label": "健康证已过期",
                                   "count": od, "severity": "urgent",
                                   "detail": "员工健康证已过期，须立即补办",
                                   "question": "哪些员工健康证已过期？列出名单"})
                if ex:
                    total_warn += ex
                    alerts.append({"type": "hc_expiring", "label": "健康证即将到期",
                                   "count": ex, "severity": "warn",
                                   "detail": "近期到期，建议提前安排体检换证",
                                   "question": "哪些员工健康证即将到期？"})
        except Exception:
            pass

        if alerts:
            parts = [f"{a['label']} {a['count']} 条" for a in alerts]
            summary = "、".join(parts) + "。"
            if total_urgent:
                summary += f"其中紧急（待整改/已过期）{total_urgent} 条，请尽快处理。"
        else:
            summary = "暂无待整改或过期预警，合规状态良好。"
        self._send_json({
            "success": True,
            "total_urgent": total_urgent,
            "total_warn": total_warn,
            "alerts": alerts,
            "summary": summary,
            "checked_at": int(time.time()),
        })

    def _api_ask(self):
        s = _get_session(self._cookie_sid())
        if not s:
            self._send_json({"success": False,
                             "message": "未登录或登录已过期，请重新登录"}, 401)
            return
        client = get_client(s)
        d = self._read_json()
        question = (d.get("question") or "").strip()
        if not question:
            self._send_json({"success": False, "message": "请输入问题"}, 400)
            return
        prior = list(s.get("history", []))
        try:
            # 流式 NDJSON 响应：逐条推送执行步骤，最后推送 done
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            answer = ""
            llm = get_llm()
            for ev in agent_mod.run_agent_stream(client, question, llm, prior=prior):
                line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(line)
                self.wfile.flush()
                if ev.get("type") == "done":
                    answer = ev.get("answer", "")
            # 多轮历史入会话（保留最近 20 轮）
            with _sess_lock:
                h = s.setdefault("history", [])
                h.append({"q": question, "a": answer})
                if len(h) > 20:
                    s["history"] = h[-20:]
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开，忽略
        except Exception as e:
            try:
                self.wfile.write((json.dumps({"type": "error", "message": f"问数失败: {e}"},
                                              ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def _api_clear(self):
        s = _get_session(self._cookie_sid())
        if not s:
            self._send_json({"success": False,
                             "message": "未登录或登录已过期，请重新登录"}, 401)
            return
        with _sess_lock:
            s["history"] = []
        self._send_json({"success": True})

    # 静默默认日志
    def log_message(self, fmt, *args):
        sys.stderr.write("[aiqa] " + (fmt % args) + "\n")


def main():
    port = config.SETTINGS["PORT"]
    host = config.SETTINGS["HOST"]
    httpd = ThreadingHTTPServer((host, port), Handler)
    _llm = get_llm()
    _mode_map = {
        "MockLLM": "MockLLM(本地演示)",
        "OpenAILikeLLM": "MaaS(真实模型)",
        "MaaSChainLLM": f"MaaS-多模型降级链路({len(_llm.models)}个: {', '.join(_llm.models[:3])}...)",
        "HunyuanLLM": f"Hunyuan({config.SETTINGS['HUNYUAN_MODEL']})",
    }
    mode = _mode_map.get(type(_llm).__name__, type(_llm).__name__)
    print(f"后厨管家 AI 问数服务已启动: http://{host}:{port}")
    print(f"LLM 模式: {mode}")
    print(f"登录方式: 直接使用后厨管家账号登录（无需单独的应用账号）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
