#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2 验证：6 个食安工具 真实 API 直调 + 真实 hy3 路由。"""
import sys, json, traceback
sys.path.insert(0, ".")
from hcg_client import HCGClient
from semantic_tools import call_tool, TOOLS
from semantic_layer import build_system_prompt
from hunyuan import OpenAILikeLLM

U, P = "at0001", "at123456@"
client = HCGClient()
lg = client.login(U, P)
assert lg.get("success"), f"login failed: {lg}"
print("LOGIN_OK")

# 用真实 hy3 做意图规划（复用服务器配置）
import os
llm = OpenAILikeLLM(
    os.environ.get("MAAS_API_KEY", ""),
    os.environ.get("MAAS_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
    os.environ.get("MAAS_MODEL", "hy3"),
)

QUESTIONS = [
    ("健康证快过期的有几人？", "health_certificate"),
    ("本月食安巡检完成率怎么样？", "food_inspect"),
    ("7月留样情况如何？", "sample_retention"),
    ("本月晨检合格率多少？", "morning_check"),
    ("食材检测合格率多少？", "detection_report"),
    ("这个月食品添加剂有没有超标？", "food_additive"),
]

def plan_with_llm(q):
    sp = build_system_prompt()
    usr = (f"{q}\n只回复一个 JSON：{{\"tool\": \"<工具名>\", \"args\": {{...}}}}，"
           f"不要包含其它文字、不要加 ``` 标记。")
    try:
        from hunyuan import _strip_json
    except Exception:
        _strip_json = None
    raw = llm.chat(system=sp, user=usr)
    return raw

print("\n=== B) 真实 hy3 路由 6 个食安问题 ===")
route_ok = 0
for q, expect in QUESTIONS:
    raw = plan_with_llm(q)
    routed = None
    try:
        # 提取 JSON
        s = raw.find("{"); e = raw.rfind("}")
        obj = json.loads(raw[s:e+1]) if (s != -1 and e != -1) else {}
        routed = obj.get("tool")
    except Exception:
        routed = f"PARSE_FAIL:{raw[:60]}"
    flag = "OK" if routed == expect else "CHECK"
    if routed == expect:
        route_ok += 1
    print(f"  [{flag}] Q='{q}' -> 路由={routed} (期望={expect})")

print(f"\n路由命中: {route_ok}/{len(QUESTIONS)}")

print("\n=== A) 6 工具真实 API 直调（空数据须优雅处理）===")
call_args = {
    "health_certificate": {},
    "food_inspect": {"inspect_type": "day"},
    "sample_retention": {},
    "morning_check": {},
    "detection_report": {},
    "food_additive": {},
}
a_ok = True
for name, extra in call_args.items():
    try:
        res, err = call_tool(client, name, dict(extra))
        if err:
            print(f"  [{name}] ERROR: {err}")
            a_ok = False
            continue
        # 打印关键计数，验证字段真实返回/结构正确
        if name == "health_certificate":
            print(f"  [{name}] OK 分布={res.get('distribution')} total={res.get('total')}")
        elif name == "food_inspect":
            print(f"  [{name}] OK bills={res.get('total_bills')} rate={res.get('completion_rate')} nc={res.get('total_nc_qty')}")
        elif name == "sample_retention":
            print(f"  [{name}] OK counts={res.get('counts')} active={res.get('active_retained')}")
        elif name == "morning_check":
            print(f"  [{name}] OK yes={res.get('qualified_yes')} no={res.get('qualified_no')} rate={res.get('qualified_rate')}")
        elif name == "detection_report":
            print(f"  [{name}] OK total={res.get('total')} rate={res.get('qualified_rate')}")
        elif name == "food_additive":
            print(f"  [{name}] OK total={res.get('total')} over={res.get('over_standard_cnt')}")
    except Exception as e:
        print(f"  [{name}] EXCEPTION: {e}")
        traceback.print_exc()
        a_ok = False

print(f"\nA_OK: {a_ok}")
print("VERIFY_DONE")
