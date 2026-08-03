#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大模型适配器 —— 腾讯混元（Hunyuan）ChatCompletions，纯标准库 TC3-HMAC-SHA256 签名。

- 真实模式：提供 SecretId / SecretKey 后调用 hunyuan.tencentcloudapi.com。
- Mock 模式：未提供密钥或设置环境变量 MOCK_LLM=1 时，使用本地启发式「伪 LLM」，
  用于无密钥情况下也能演示「登录 → 问数 → 真实接口数据 → 自然语言结论」全流程。

对外统一接口：llm.chat(system, user, history=None) -> str（模型回复文本）。
"""

import os
import json
import hmac
import hashlib
import datetime
import urllib.request
from urllib.parse import quote

HOST = "hunyuan.tencentcloudapi.com"
SERVICE = "hunyuan"
REGION = "ap-guangzhou"
ACTION = "ChatCompletions"
VERSION = "2023-09-01"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class HunyuanLLM:
    def __init__(self, secret_id: str = "", secret_key: str = "",
                 model: str = "hunyuan-turbo"):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.model = model

    def _sign_request(self, payload: str, timestamp: str):
        date = datetime.datetime.utcfromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
        ct = "application/json; charset=utf-8"
        canonical_headers = (
            f"content-type:{ct}\nhost:{HOST}\nx-tc-action:{ACTION.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        canonical_request = "\n".join([
            "POST", "/", "", canonical_headers, signed_headers, _sha256(payload),
        ])
        credential_scope = f"{date}/{SERVICE}/tc3_request"
        string_to_sign = "\n".join([
            "TC3-HMAC-SHA256", timestamp, credential_scope, _sha256(canonical_request),
        ])
        secret = ("TC3" + self.secret_key).encode("utf-8")
        secret_date = _sign(secret, date)
        secret_service = _sign(secret_date, SERVICE)
        secret_signing = _sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                             hashlib.sha256).hexdigest()
        return credential_scope, signed_headers, signature

    def chat(self, system: str, user: str, history=None) -> str:
        messages = [{"Role": "system", "Content": system}]
        for h in (history or []):
            role = h.get("Role")
            content = h.get("Content", "")
            if role == "tool":
                # 混元不支持 tool 角色，转成 user 并加标记
                messages.append({"Role": "user",
                                 "Content": "【工具返回的真实数据，请据此回答】\n" + content})
            else:
                messages.append({"Role": role, "Content": content})
        messages.append({"Role": "user", "Content": user})
        body = json.dumps({
            "Model": self.model,
            "Messages": messages,
            "Stream": False,
        })
        timestamp = str(int(datetime.datetime.utcnow().timestamp()))
        scope, signed, signature = self._sign_request(body, timestamp)
        auth = (f"TC3-HMAC-SHA256 Credential={self.secret_id}/{scope}, "
                f"SignedHeaders={signed}, Signature={signature}")
        headers = {
            "Authorization": auth,
            "Content-Type": "application/json; charset=utf-8",
            "Host": HOST,
            "X-TC-Action": ACTION,
            "X-TC-Version": VERSION,
            "X-TC-Timestamp": timestamp,
            "X-TC-Region": REGION,
        }
        url = f"https://{HOST}/"
        req = urllib.request.Request(url, data=body.encode("utf-8"),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        resp_body = data.get("Response", {})
        if "Error" in resp_body:
            raise RuntimeError(f"混元接口错误: {resp_body['Error']}")
        choices = resp_body.get("Choices") or []
        content = (choices[0].get("Message", {}) or {}).get("Content", "")
        usage = resp_body.get("Usage") or {}
        prompt_tokens = usage.get("PromptTokens") or usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("CompletedTokens") or usage.get("completed_tokens") or 0
        total_tokens = usage.get("TotalTokens") or usage.get("total_tokens") or 0
        if not total_tokens:
            chars = sum(len(x) for x in [system, user] + [h.get("Content", "") for h in (history or [])]) + len(content)
            total_tokens = (chars + 1) // 2
            prompt_tokens = total_tokens * 2 // 3
            completion_tokens = total_tokens - prompt_tokens
        return content, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}


class OpenAILikeLLM:
    """OpenAI 兼容网关调用（如腾讯 MaaS tokenhub 的混元 hy3），纯标准库 urllib 实现。

    与 HunyuanLLM 的区别：无需 TC3 签名，直接 Bearer Token 调 /chat/completions。
    对外接口与 MockLLM 一致：llm.chat(system, user, history=None) -> str。
    """

    def __init__(self, api_key: str = "",
                 base_url: str = "https://tokenhub.tencentmaas.com/v1",
                 model: str = "hy3"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def chat(self, system: str, user: str, history=None) -> str:
        # 若历史里已有工具返回数据，切到「结论模式」，明确要求自然语言回答、
        # 不再吐 JSON，避免模型在已有数据的情况下再次触发工具调用。
        has_tool = bool(history) and any(h.get("Role") == "tool" for h in history)
        if has_tool:
            system = ("你是「后厨管家」AI 问数助手。下面是工具返回的真实数据，"
                      "请直接用简洁、准确的中文回答用户的问题，并引用关键数字；"
                      "严禁编造或估算。不要输出 JSON，直接给出自然语言结论。"
                      "注意：数据中包含的明细列表会由前端自动渲染成表格，"
                      "你只需在正文给出汇总摘要，不要在正文中重复 Markdown 表格。")
        messages = [{"role": "system", "content": system}]
        for h in (history or []):
            role = h.get("Role")
            content = h.get("Content", "")
            if role == "tool":
                # OpenAI 无 tool 角色，转成 user 并加标记
                messages.append({"role": "user",
                                 "content": "【工具返回的真实数据，请据此回答】\n" + content})
            else:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user})
        body = json.dumps({"model": self.model, "messages": messages, "stream": False})
        url = self.base_url + "/chat/completions"
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
        total_tokens = usage.get("total_tokens") or 0
        if not total_tokens:
            chars = sum(len(x) for x in [system, user] + [h.get("Content", "") for h in (history or [])]) + len(content)
            total_tokens = (chars + 1) // 2
            prompt_tokens = total_tokens * 2 // 3
            completion_tokens = total_tokens - prompt_tokens
        return content, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}


# 默认模型候选（可通过 .env 的 MAAS_MODELS 覆盖，逗号分隔）。
# 顺序即「降级优先级」：靠前模型额度/可用时优先使用，失效后自动向后切换。
DEFAULT_MAAS_MODELS = [
    "kimi-k3",
    "kimi-k2.7-code-highspeed",
    "glm-5.2",
    "kimi-k2.7-code",
    "minimax-m3",
    "deepseek-v4-flash-202605",
    "deepseek-v4-pro-202606",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "hy-mt2-pro",
    "mimo-v2.5-pro",
    "qwen3.5-flash",
    "glm-5.1",
    "glm-5v-turbo",
]


class MaaSChainLLM:
    """OpenAI 兼容网关的多模型链路调用（腾讯 MaaS tokenhub）。

    持有一个模型候选列表，调用时按顺序尝试；若某模型因【额度不足 / 限流 /
    网关不可用 / 本账号无该模型权限（model not found）】等原因失败，则自动
    切换到下一个模型，直到有一个成功或一个都不剩。

    一旦某个模型成功，后续调用优先复用该模型（若其后续也失效，再从它之后
    继续向后探测），避免每次都重试整条链路，减少无效请求与延迟。

    对外接口与 MockLLM 一致：llm.chat(system, user, history=None) -> str。
    """

    def __init__(self, api_key: str,
                 base_url: str = "https://tokenhub.tencentmaas.com/v1",
                 models=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.models = list(models) if models else ["hy3"]
        self._last_ok = 0  # 记录最近一次成功的模型下标

    def _one_call(self, model: str, messages: list) -> tuple:
        body = json.dumps({"model": model, "messages": messages, "stream": False})
        url = self.base_url + "/chat/completions"
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
        total_tokens = usage.get("total_tokens") or 0
        if not total_tokens:
            chars = sum(len(m.get("content", "")) for m in messages) + len(content)
            total_tokens = (chars + 1) // 2
            prompt_tokens = total_tokens * 2 // 3
            completion_tokens = total_tokens - prompt_tokens
        return content, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}

    @staticmethod
    def _is_fallback_error(status: int, body_text: str) -> bool:
        """判断该错误是否应「切换下一个模型」而非直接抛出。

        以下情况视为可降级：额度/限流（402/429）、网关抖动（5xx/504）、
        以及响应体中出现额度/限流/模型不存在/不可用等可识别关键字。
        其余（如 400 请求体错误、401 鉴权失败）直接抛出，避免无意义重试。
        """
        if status in (402, 429, 500, 502, 503, 504):
            return True
        low = (body_text or "").lower()
        keys = ["quota", "额度", "payment", "rate limit", "rate_limit",
                "insufficient", "not exist", "不存在", "model not",
                "unavailable", "suspended", "deactivated", "exceed",
                "forbidden", "无权限", "not found"]
        return any(k in low for k in keys)

    def chat(self, system: str, user: str, history=None) -> str:
        # 若历史里已有工具返回数据，切到「结论模式」，明确要求自然语言回答、
        # 不再吐 JSON，避免模型在已有数据的情况下再次触发工具调用。
        has_tool = bool(history) and any(h.get("Role") == "tool" for h in history)
        if has_tool:
            system = ("你是「后厨管家」AI 问数助手。下面是工具返回的真实数据，"
                      "请直接用简洁、准确的中文回答用户的问题，并引用关键数字；"
                      "严禁编造或估算。不要输出 JSON，直接给出自然语言结论。"
                      "注意：数据中包含的明细列表会由前端自动渲染成表格，"
                      "你只需在正文给出汇总摘要，不要在正文中重复 Markdown 表格。")
        messages = [{"role": "system", "content": system}]
        for h in (history or []):
            role = h.get("Role")
            content = h.get("Content", "")
            if role == "tool":
                # OpenAI 无 tool 角色，转成 user 并加标记
                messages.append({"role": "user",
                                 "content": "【工具返回的真实数据，请据此回答】\n" + content})
            else:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user})

        n = len(self.models)
        start = self._last_ok if 0 <= self._last_ok < n else 0
        tried = 0
        last_err = None
        idx = start
        while tried < n:
            model = self.models[idx]
            try:
                content, usage = self._one_call(model, messages)
                self._last_ok = idx
                return content, usage
            except urllib.error.HTTPError as e:
                try:
                    body_text = e.read().decode("utf-8", "ignore")
                except Exception:
                    body_text = ""
                last_err = f"[{model}] HTTP {e.code}: {body_text[:200]}"
                if not self._is_fallback_error(e.code, body_text):
                    raise RuntimeError(
                        f"MaaS 模型 {model} 调用失败: HTTP {e.code} {body_text[:300]}") from e
            except Exception as e:  # 网络等其他异常也尝试下一个模型
                last_err = f"[{model}] {type(e).__name__}: {e}"
            tried += 1
            idx = (idx + 1) % n
        raise RuntimeError(f"所有 MaaS 模型均不可用（额度/可用性）：{last_err}")


class MockLLM:
    """本地启发式伪 LLM：负责把中文问句映射到语义工具，并基于真实工具结果生成结论。"""

    @staticmethod
    def _parse_range(q: str):
        import re
        from datetime import datetime, date
        today = date.today()
        # 今天 / 今日
        if "今天" in q or "今日" in q:
            return today.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
        # 某月：如 "7月"/"七月"/"2026年7月"
        m = re.search(r"(20\d{2})?\s*年?\s*([0-9]{1,2}|十[一二]|一|二|三|四|五|六|七|八|九|十)\s*月", q)
        if m:
            year = int(m.group(1)) if m.group(1) else today.year
            cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
                  "七": 7, "八": 8, "九": 9, "十": 10}
            mon_s = m.group(2)
            mon = cn.get(mon_s, int(mon_s)) if not mon_s.isdigit() else int(mon_s)
            start = f"{year}-{mon:02d}-01"
            if mon == 12:
                end = f"{year}-12-31"
            else:
                nxt = date(year, mon + 1, 1)
                end = (nxt - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
            return start, end
        # 本月
        if "本月" in q or "这个月" in q:
            start = today.replace(day=1).strftime("%Y-%m-%d")
            return start, today.strftime("%Y-%m-%d")
        # 默认：本月
        start = today.replace(day=1).strftime("%Y-%m-%d")
        return start, today.strftime("%Y-%m-%d")

    @staticmethod
    def _metric(q: str):
        if any(k in q for k in ["金额", "钱", "花费", "总价", "多少", "成本"]):
            return "amount"
        if any(k in q for k in ["数量", "多少斤", "多少只", "多少件", "斤数"]):
            return "qty"
        if any(k in q for k in ["笔数", "次数", "多少单", "多少笔", "单量"]):
            return "count"
        return "amount"

    @staticmethod
    def _parse_warehouse(q: str):
        """从问题中粗略提取仓库名（如"上海奥运餐厅""某仓库"）。找不到返回 None。"""
        import re
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9·\-\.]+?(?:餐厅|仓库|门店|店|仓))", q)
        if m:
            return m.group(1).strip()
        return None

    def chat(self, system: str, user: str, history=None) -> tuple:
        # 若 history 里已有工具结果（Role=="tool" 的消息），则生成最终结论
        zero_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if history:
            for h in history:
                if h.get("Role") == "tool":
                    return self._summarize(user, h.get("Content", "")), zero_usage
        # 否则根据问题规划工具调用（返回 JSON）
        return self._plan(user), zero_usage

    def _plan(self, q: str) -> str:
        start, end = self._parse_range(q)
        wh = self._parse_warehouse(q)

        def args(**kw):
            # 若问题里识别到仓库名，则注入 warehouse_name（可选按仓库筛选）
            if wh:
                kw["warehouse_name"] = wh
            return kw

        # ⚠️ 以下为「无密钥降级」用的极简启发式，仅供本地演示；
        #    生产路径由真实大模型（OpenAILikeLLM）基于语义理解意图，不使用关键字路由。
        #    关键字极易误判（例："库存分类分析"含"分类分析"，若放在通用分支会被误导向采购分类）。
        # 1) 库存预警
        if any(k in q for k in ["预警", "过期", "临期", "快到期"]):
            return json.dumps({"tool": "stock_warning", "args": args()}, ensure_ascii=False)
        # 1.1) 食堂食安管理域（健康证/证照/巡检/留样/晨检/检测/添加剂）—— 与采购/库存区分，优先命中
        if any(k in q for k in ["健康证", "证照", "证件"]):
            return json.dumps({"tool": "health_certificate", "args": args()}, ensure_ascii=False)
        if any(k in q for k in ["巡检", "日管控", "周排查", "月调度"]):
            itype = "month" if "月调度" in q else ("week" if "周排查" in q else "day")
            return json.dumps({"tool": "food_inspect",
                               "args": args(inspect_type=itype, start_date=start, end_date=end)},
                              ensure_ascii=False)
        if any(k in q for k in ["留样"]):
            return json.dumps({"tool": "sample_retention",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        if any(k in q for k in ["晨检", "午检", "晚检", "晨检合格", "体温", "晨检异常"]):
            return json.dumps({"tool": "morning_check",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        if any(k in q for k in ["检测", "农残", "合格率"]) and "库存" not in q:
            return json.dumps({"tool": "detection_report",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        if any(k in q for k in ["添加剂", "食品添", "防腐剂"]):
            return json.dumps({"tool": "food_additive",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 1.15) 食安预警总览（红线，fs 专属；须放在综合预警中心(1.2)之前以优先命中）
        if any(k in q for k in ["食安预警", "食品安全预警", "食安待办", "食安红线", "食安隐患",
                                "食安问题没改", "食安预警总览", "食安预警明细", "食安问题"]):
            return json.dumps({"tool": "food_safety_alert",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 1.2) 综合预警中心（与 stock_warning 的"过期/临期"区分：这里针对"预警看板/待整改/统一预警"）
        if any(k in q for k in ["综合预警", "预警看板", "预警中心", "待整改", "已整改", "预警台账", "预警明细"]):
            return json.dumps({"tool": "warning_center",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 1.3) 环境设备告警指数（温度/湿度/烟雾/燃气/水浸/AI巡检）
        if any(k in q for k in ["环境告警", "温度告警", "湿度告警", "烟雾告警", "燃气告警", "水浸告警",
                                "设备告警指数", "告警指数", "消杀环境", "AI巡检告警"]):
            return json.dumps({"tool": "device_alarm_index", "args": args()}, ensure_ascii=False)
        # 1.4) 环境设备告警明细（含"未处理告警/告警明细/温度超标"）
        if any(k in q for k in ["设备告警明细", "未处理告警", "消杀告警", "环境设备告警", "温度超标", "告警记录", "告警明细"]):
            return json.dumps({"tool": "device_alarm_detail",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 1.5) 经营驾驶舱总览（管理者每日第一眼；须放在具体库存/采购分支之前，避免被"库存""采购"等词拆走）
        if any(k in q for k in ["驾驶舱", "经营总览", "经营驾驶舱", "经营概览", "今日概览", "今日经营",
                                "今日情况", "经营情况", "总览", "概况", "一屏看", "看板", "今日采购多少",
                                "今天经营", "食安概况", "今日概览"]):
            return json.dumps({"tool": "dashboard_overview", "args": args()}, ensure_ascii=False)
        # 2) 当前库存 + 分类占比（必须排在通用"分类分析"之前）
        if "库存" in q and any(k in q for k in ["分类", "占比", "品类", "类目"]):
            return json.dumps({"tool": "inventory_by_category", "args": args()}, ensure_ascii=False)
        # 3) 当前库存 + 按仓库
        if "库存" in q and any(k in q for k in ["仓库", "门店", "店", "各仓", "分布"]):
            return json.dumps({"tool": "inventory_by_warehouse", "args": args()}, ensure_ascii=False)
        # 2.5) 库存月报（月度经营复盘；"库存" + 月维度）
        if "库存" in q and any(k in q for k in ["月报", "月度", "当月库存", "月库存", "期末库存", "各月库存", "当月进销存"]):
            return json.dumps({"tool": "stock_month_report", "args": args()}, ensure_ascii=False)
        # 5.6) P1 报表：排菜成本率 / 出品口碑 / 营养 NRV / 询比价成效
        if any(k in q for k in ["排菜成本", "菜单成本", "成本率", "餐标", "超标准", "超成本", "菜超"]):
            return json.dumps({"tool": "dish_cost_rate", "args": args()}, ensure_ascii=False)
        if any(k in q for k in ["菜品评价", "出品口碑", "口碑", "评分", "评价数", "受欢迎", "差评", "好评"]):
            return json.dumps({"tool": "dish_reputation", "args": args()}, ensure_ascii=False)
        if any(k in q for k in ["营养", "NRV", "热量", "蛋白质", "脂肪", "碳水", "钠", "健康膳食", "膳食"]):
            return json.dumps({"tool": "dish_nutrition", "args": args()}, ensure_ascii=False)
        if any(k in q for k in ["询比价", "比价", "报价率", "中标", "询价", "降本", "报价单", "询比价成效"]):
            return json.dumps({"tool": "inquiry_effect", "args": args()}, ensure_ascii=False)
        # 5.5) 周期对比 / 趋势 / 环比（问数增强，Phase 4）—— 必须放在 4.1 成本利润 / 5)趋势 之前优先命中
        #   命中「跨周期对比」语义：逐月/各月/每月/上半年/下半年/近半年/近一年/环比/同比/对比/
        #   走势/各月对比/每月走势，或正则"X月比Y月"（如"7月比6月多多少"）。
        #   单区间（如"7月采购额""3月每天采购趋势"）不含上述词，不会被误导向本工具。
        import re as _re
        from datetime import date as _d
        _cmp_kw = ["逐月", "各月", "每月", "每个月", "上半年", "下半年", "一季度", "二季度", "三季度", "四季度",
                   "近半年", "近一年", "近三月", "环比", "同比", "对比", "比上月", "和上月比",
                   "与上", "vs", "比上", "各月对比", "每月走势", "每月变化", "各月采购", "各月利润", "每月对比",
                   "逐期", "连续", "时间序列"]
        _multi_month = _re.search(r"\d+\s*月\s*比\s*\d+\s*月", q)
        if any(k in q for k in _cmp_kw) or _multi_month:
            _today = _d.today()
            _ym = _re.search(r"(20\d{2})", q)
            _yr = int(_ym.group(1)) if _ym else _today.year
            # 默认给「最近 6 个自然月」（含当前月）
            months = []
            for i in range(5, -1, -1):
                y = _yr
                m = _today.month - i
                while m <= 0:
                    m += 12
                    y -= 1
                months.append(f"{y}-{m:02d}")
            if "上半年" in q:
                months = [f"{_yr}-{m:02d}" for m in range(1, 7)]
            elif "下半年" in q:
                months = [f"{_yr}-{m:02d}" for m in range(7, 13)]
            # 若成本利润语义（利润/收入/支出 + 对比/跨周期）
            if any(k in q for k in ["利润", "收支", "盈利", "成本利润", "收入", "支出"]) and "采购" not in q:
                _metric = "income" if "收入" in q else ("expense" if "支出" in q else "profit")
                return json.dumps({"tool": "period_compare",
                                   "args": args(base_tool="cost_profit", periods=months, metric=_metric)},
                                  ensure_ascii=False)
            return json.dumps({"tool": "period_compare",
                               "args": args(base_tool="purchase_stat", periods=months)},
                              ensure_ascii=False)
        # 4.1) 成本利润（注意：不要用裸"成本"，以免误伤"采购成本"）
        if any(k in q for k in ["利润", "盈亏", "收支", "盈利", "成本利润", "毛利润"]):
            return json.dumps({"tool": "cost_profit", "args": args()}, ensure_ascii=False)
        # 4.2) 退货 / 退回
        if any(k in q for k in ["退货", "退回", "退供"]):
            return json.dumps({"tool": "purchase_return",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 4.3) 领料 / 领用（专属领料出库统计工具）
        if any(k in q for k in ["领料", "领用", "领出库"]):
            return json.dumps({"tool": "picking_out",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 4.4) 配送 / 物流 / 履约 / 验收差异
        if any(k in q for k in ["配送", "物流", "履约", "验收差异", "发货"]):
            return json.dumps({"tool": "delivery_fulfillment",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 4.5) 供应商结算 / 绩效（"供应商排行"不含这些词，仍走排行分支）
        if any(k in q for k in ["供应商结算", "供应商绩效", "供应商考核", "结算金额", "应付账款"]):
            return json.dumps({"tool": "supplier_settlement",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 4.6) 申购 / 请购
        if any(k in q for k in ["申购", "请购", "申请采购"]):
            return json.dumps({"tool": "requisition_status",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 4.7) 采购价对比（成本把控；逐笔比平台价，找买贵的）
        if any(k in q for k in ["采购价对比", "价比", "比平台价", "超价", "买贵", "采购单价", "市场价",
                                "新发地", "价异常", "价格对比", "高出平台", "比市场"]):
            return json.dumps({"tool": "purchase_price_compare",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 4) 出库 / 发出（领料已拆到 4.3）
        if any(k in q for k in ["出库", "越库出", "发出", "发出去", "出库的"]):
            return json.dumps({"tool": "stock_out_by_warehouse",
                               "args": args(start_date=start, end_date=end)}, ensure_ascii=False)
        # 5) 趋势（单区间按天走势，如"X月每天采购趋势"；跨周期已在上分支命中 period_compare）
        if any(k in q for k in ["趋势", "每天", "每日", "按天", "折线图", "折线", "曲线图", "趋势图"]):
            metric = self._metric(q)
            return json.dumps({"tool": "daily_trend",
                               "args": args(metric=metric, start_date=start, end_date=end)},
                              ensure_ascii=False)
        # 6) 采购/入库 按仓库
        if any(k in q for k in ["采购", "进货", "入库"]) and any(k in q for k in ["仓库", "门店", "店", "各仓", "按仓"]):
            return json.dumps({"tool": "purchase_inbound_by_warehouse",
                               "args": args(start_date=start, end_date=end)},
                              ensure_ascii=False)
        # 7) 排行 / TOP / 柱状图 / 分类分析 / 品类
        if any(k in q for k in ["排行", "最多", "最高", "前几", "top", "排名", "榜", "柱状图", "柱形图", "条形图", "排行图", "TOP图", "分类分析", "品类"]):
            if "供应商" in q:
                dim = "supplier"
            elif any(k in q for k in ["仓库", "门店", "店"]):
                dim = "warehouse"
            elif any(k in q for k in ["分类", "品类", "类目", "分类分析"]):
                dim = "goods_category"
            else:
                dim = "goods"
            metric = self._metric(q)
            return json.dumps({"tool": "rank_by_dimension",
                               "args": args(dimension=dim, metric=metric,
                                            start_date=start, end_date=end, top_n=10)},
                              ensure_ascii=False)
        # 8) 默认：采购入库汇总
        return json.dumps({"tool": "purchase_inbound_summary",
                           "args": args(start_date=start, end_date=end)},
                          ensure_ascii=False)

    @staticmethod
    def _summarize(question: str, tool_result_json: str) -> str:
        try:
            r = json.loads(tool_result_json)
        except Exception:
            return f"已获取工具数据，但解析失败：{tool_result_json[:200]}"
        t = r.get("tool")
        if t == "purchase_inbound_summary":
            return (f"根据后厨管家接口真实返回，{r['filters']['start_date']} 至 "
                    f"{r['filters']['end_date']} 的【采购入库】共 {r['count']} 笔，"
                    f"估算总金额 ¥{r['total_amount_est']:,.2f}，合计数量 {r['total_qty']:,.2f}。"
                    f"（金额=单价×数量估算值；不同计量单位已分别统计。）")
        if t == "rank_by_dimension":
            items = r.get("items", [])[:5]
            lines = "；".join(
                f"{i+1}. {it['name']}（{it.get('unit','')}）"
                f"{'金额¥%s'%format(it['amount'],',.2f') if r['metric']=='amount' else ('数量%s'%it['qty'] if r['metric']=='qty' else '%s笔'%it['count'])}"
                for i, it in enumerate(items))
            dim_cn = {"goods": "商品", "warehouse": "仓库", "supplier": "供应商"}[r["dimension"]]
            return f"{r['range']} 按{dim_cn}的{r['metric']}排行 TOP5：{lines}。"
        if t == "daily_trend":
            pts = r.get("points", [])
            if not pts:
                return f"{r['range']} 暂无采购入库数据。"
            last = pts[-1]
            return (f"{r['range']} 共 {len(pts)} 天有采购入库记录；"
                    f"最新一天（{last['date']}）"
                    f"{'估算金额¥%s'%format(last['amount'],',.2f') if r['metric']=='amount' else ('数量%s'%last['qty'] if r['metric']=='qty' else '%s笔'%last['count'])}。"
                    f"（完整逐日序列已在数据区给出。）")
        if t == "stock_warning":
            return (f"库存预警：已过期 {r['outdated_count']} 条，临期预警中 {r['warning_count']} 条。"
                    f"（过期判定 outdated≤当前时间；临期判定 warnDated≤当前时间且未过期。）")
        if t == "inventory_by_warehouse":
            whs = r.get("warehouses", [])[:5]
            lines = "；".join(f"{w['warehouse']}（{w['goods_count']}种，{w['qty']}）" for w in whs)
            return (f"当前库存按仓库汇总：共 {len(r.get('warehouses', []))} 个仓库，"
                    f"商品种类 {r['total_goods']}，合计数量 {r['total_qty']:,.2f}，"
                    f"估算金额 ¥{r['total_amount_est']:,.2f}。其中：{lines}。"
                    f"（金额=单价×数量估算值；完整明细已在数据区给出。）")
        if t == "inventory_by_category":
            cats = r.get("categories", [])[:5]
            lines = "；".join(f"{c['category']}（{c['qty_ratio']}%）" for c in cats)
            return (f"当前库存按分类占比：共 {len(r.get('categories', []))} 个一级分类，"
                    f"商品种类 {r['total_goods']}，合计数量 {r['total_qty']:,.2f}，"
                    f"估算金额 ¥{r['total_amount_est']:,.2f}。占比前几：{lines}。"
                    f"（仅统计库存数量>0 的有效库存；完整明细已在数据区给出。）")
        if t == "purchase_inbound_by_warehouse":
            whs = r.get("warehouses", [])[:5]
            lines = "；".join(f"{w['warehouse']}（{w['count']}笔，{w['qty']}）" for w in whs)
            return (f"{r['filters']['start_date']} 至 {r['filters']['end_date']} 的【采购入库】按仓库汇总："
                    f"共 {r['total_count']} 笔，估算金额 ¥{r['total_amount_est']:,.2f}，"
                    f"合计数量 {r['total_qty']:,.2f}。其中：{lines}。（采购入库含越库 purchaseCrossIn。）")
        if t == "stock_out_by_warehouse":
            whs = r.get("warehouses", [])[:5]
            lines = "；".join(f"{w['warehouse']}（{w['count']}笔，{w['qty']}）" for w in whs)
            bt = r.get("by_type", {})
            type_line = "；".join(f"{k}（{v['count']}笔）" for k, v in bt.items()) or "无"
            return (f"{r['filters']['start_date']} 至 {r['filters']['end_date']} 的【出库】按仓库汇总："
                    f"共 {r['total_count']} 笔，估算金额 ¥{r['total_amount_est']:,.2f}，"
                    f"合计数量 {r['total_qty']:,.2f}。按类型：{type_line}。其中仓库：{lines}。")
        if t == "supplier_settlement":
            tops = r.get("by_supplier_top", [])[:5]
            lines = "；".join(f"{i+1}.{it['name']}（结算¥{it['settle_amount']:,.2f}）" for i, it in enumerate(tops))
            return (f"{r['filters']['start_date']} 至 {r['filters']['end_date']} 供应商结算统计：共 {r['total_suppliers']} 家，"
                    f"入库总金额 ¥{r['total_purchase_amount']:,.2f}，结算总金额 ¥{r['total_settle_amount']:,.2f}，"
                    f"实退总金额 ¥{r['total_return_amount']:,.2f}。结算金额 TOP：{lines}。")
        if t == "delivery_fulfillment":
            f = r.get("fulfillment", {})
            return (f"配送履约：待分拣 {f.get('notSorting',0)} / 待发货 {f.get('notDelivery',0)} / "
                    f"待验收 {f.get('notStockIn',0)} / 已验收 {f.get('stockIned',0)}；"
                    f"采购金额 ¥{r['total_purchase_amount']:,.2f}，入库金额 ¥{r['total_stock_in_amount']:,.2f}，"
                    f"验收差异金额 ¥{r['total_diff_amount']:,.2f}。")
        if t == "cost_profit":
            parts = []
            if r.get("income"):
                parts.append(f"收入 ¥{r['income']['total_amount']:,.2f}")
            if r.get("expense"):
                parts.append(f"支出 ¥{r['expense']['total_amount']:,.2f}")
            if r.get("profit") is not None:
                parts.append(f"利润 ¥{r['profit']:,.2f}")
            return f"{r['filters']['date']} 成本利润（{r['filters']['metric']}）：" + "，".join(parts) + "。"
        if t == "purchase_return":
            return (f"{r['filters']['start_date']} 至 {r['filters']['end_date']} 退货统计：{r['total_bills']} 单，"
                    f"应退 ¥{r['total_return_amount']:,.2f}，实退 ¥{r['total_actual_return_amount']:,.2f}。")
        if t == "picking_out":
            return (f"{r['filters']['start_date']} 至 {r['filters']['end_date']} 领料出库：{r['total_bills']} 单，"
                    f"计划 ¥{r['total_planned_amount']:,.2f}，实际出库 ¥{r['total_actual_out_amount']:,.2f}"
                    f"（已出库/完成 {r['completed_bills']} 单）。")
        if t == "requisition_status":
            return (f"{r['filters']['start_date']} 至 {r['filters']['end_date']} 申购验收：明细 已采购 {r['line_has_purchase_qty']} / "
                    f"待采购 {r['line_not_purchase_qty']} / 已驳回 {r['line_rejected_qty']}；"
                    f"申购单 {r['total_bills']} 单，金额 ¥{r['total_apply_amount']:,.2f}。")
        # ---- Phase 2 食安管理域（无金额，纯计数/合规）----
        if t == "health_certificate":
            d = r.get("distribution", {})
            return (f"健康证合规：共 {r.get('total',0)} 人，正常 {d.get('normalQty',0)} / 即将到期 {d.get('aboutToExpireQty',0)} / "
                    f"已过期 {d.get('overdueQty',0)} / 已停用 {d.get('disableQty',0)}（临期 {len(r.get('expiring_soon',[]))} 人、过期 {len(r.get('expired',[]))} 人明细已列出）。")
        if t == "food_inspect":
            return (f"食安巡检（{r.get('inspect_type_label','')}）：共 {r.get('total_bills',0)} 单，"
                    f"完成率 {r.get('completion_rate',0)}%，不符合项 {r.get('total_nc_qty',0)} 个（涉及 {r.get('nc_bills',0)} 单）。")
        if t == "sample_retention":
            c = r.get("counts", {})
            return (f"留样管理：待存入 {c.get('待存入',0)} / 待取出 {c.get('待取出',0)} / 留样中 {c.get('留样中',0)} / "
                    f"已取出 {c.get('已取出',0)}（合规留存 {r.get('active_retained',0)}）。")
        if t == "morning_check":
            return (f"晨检记录：合格 {r.get('qualified_yes',0)} / 不合格 {r.get('qualified_no',0)} / 在岗 {r.get('total_qty',0)}，"
                    f"合格率 {r.get('qualified_rate',0)}%。")
        if t == "detection_report":
            return (f"检测报告：共 {r.get('total',0)} 条，合格 {r.get('qualified_yes',0)} / 不合格 {r.get('qualified_no',0)}，"
                    f"合格率 {r.get('qualified_rate',0)}%。")
        if t == "food_additive":
            return (f"食品添加剂：共 {r.get('total',0)} 条记录，超标 {r.get('over_standard_cnt',0)} 条。")
        # ---- Phase 3 综合预警 + 环境告警（无金额，纯计数/状态）----
        if t == "warning_center":
            sa = r.get("status_agg", {})
            return (f"综合预警：共 {r.get('total',0)} 条，待整改 {sa.get('待整改',0)} / 已整改 {sa.get('已整改',0)} / "
                    f"已忽略 {sa.get('已忽略',0)} / 已确认 {sa.get('已确认',0)}（待整改 TOP {len(r.get('pending_top',[]))} 条已列出）。")
        if t == "device_alarm_index":
            return (f"环境设备告警指数：累计 {r.get('total_alarms',0)} 次"
                    f"（温度/湿度/烟雾/燃气/水浸/AI巡检）。")
        if t == "device_alarm_detail":
            bs = r.get("by_status", [])
            unhandled = next((b["count"] for b in bs if b["status"] == "未处理"), 0)
            return (f"环境设备告警明细：共 {r.get('total',0)} 条，未处理 {unhandled} 条"
                    f"（未处理/已处理 TOP {len(r.get('unresolved_top',[]))} 条已列出）。")
        if t == "period_compare":
            sm = r.get("summary", {})
            mm = r.get("main_metric", "")
            ser = r.get("series", [])
            if not ser:
                return "周期对比：未生成有效序列。"
            fv = sm.get("first_value"); lv = sm.get("last_value")
            fv_s = f"¥{fv:,.2f}" if isinstance(fv, (int, float)) else str(fv)
            lv_s = f"¥{lv:,.2f}" if isinstance(lv, (int, float)) else str(lv)
            lines = []
            for s in ser:
                dp = s.get("main_delta_pct")
                if dp is None:
                    tail = "（基期/环比 null）"
                else:
                    tail = f"（环比 {('+' if dp > 0 else '')}{dp}%）"
                lines.append(f"{s['period']}: {s['main_value']:,.2f}{tail}")
            seq = "；".join(lines)
            return (f"周期对比（{r.get('base_tool')}·{mm}）：共 {r.get('period_count')} 期，"
                    f"从 {sm.get('first_period')} 的 {fv_s} 到 {sm.get('last_period')} 的 {lv_s}；"
                    f"环比上升 {sm.get('rising_count')} 期、下降 {sm.get('falling_count')} 期。"
                    f"逐期：{seq}。（金额均来自服务端聚合，准确非估算。）")
        return f"已获取数据：{tool_result_json[:300]}"


def get_llm():
    """按环境返回 LLM 实例。

    设计原则（重要）：意图路由一律交由【大模型】基于语义理解判断，
    绝不在生产路径使用关键字规则路由（关键字极易误判，如"库存分类分析"
    会被"分类分析"关键字误导向采购分类工具）。

    优先级：MaaS 网关(OpenAI 兼容，多模型降级链路) > 原生混元 TC3 > MockLLM(无密钥降级)。
    - 只要存在真实大模型凭证（MAAS_API_KEY 或 混元 SecretId/Key），
      就走 LLM 意图理解，即使设置了 MOCK_LLM=1 也会被真实模型覆盖。
    - MaaS 走「模型降级链路」：MAAS_MODELS(.env 逗号分隔) 列出候选模型，
      按顺序尝试；某模型额度/限流/不可用时自动切到下一个，全部失败才报错。
      未配置 MAAS_MODELS 时回退到 MAAS_MODEL(单模型) 或内置默认模型列表。
    - 仅在完全没有任何凭证时，才降级到 MockLLM 的本地启发式
      （仅用于无密钥演示，非生产路径，路由结果仅供参考）。
    """
    maas_key = os.environ.get("MAAS_API_KEY", "")
    maas_base = os.environ.get("MAAS_BASE_URL", "https://tokenhub.tencentmaas.com/v1")
    models_env = os.environ.get("MAAS_MODELS", "")
    if models_env:
        models = [m.strip() for m in models_env.split(",") if m.strip()]
    else:
        single = os.environ.get("MAAS_MODEL", "")
        models = [single] if single else DEFAULT_MAAS_MODELS
    if maas_key:
        return MaaSChainLLM(maas_key, maas_base, models)
    secret_id = os.environ.get("HUNYUAN_SECRET_ID", "")
    secret_key = os.environ.get("HUNYUAN_SECRET_KEY", "")
    model = os.environ.get("HUNYUAN_MODEL", "hunyuan-turbo")
    if secret_id and secret_key:
        return HunyuanLLM(secret_id, secret_key, model)
    # 无任何真实凭证：降级到 MockLLM（本地启发式，仅无密钥演示用）
    return MockLLM()
