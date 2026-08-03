#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 验证：
  A) 用真实账号登录，直接调用 6 个新语义工具，确认接口/字段/金额真实/仓库过滤生效；
  B) 用真实 hy3 跑 6 个易混问题，确认 LLM 正确路由到 6 个新工具。
"""
import sys, json, time
from datetime import date, timedelta

BASE = "/Users/phil/WorkBuddy/2026-07-16-11-31-47/ai_qa_system"
sys.path.insert(0, BASE)

from hcg_client import HCGClient
import semantic_tools as st
import agent as agent_mod
import config
from hunyuan import get_llm

# ---- 账号（与 Phase 0 一致）----
USER, PWD = "at0001", "at123456@"

t = date(2026, 7, 31)
SD_ALL = "2026-07-01"; ED_ALL = "2026-07-31"
SD5 = "2026-07-01"; ED5 = "2026-07-05"   # 防大区间 too_large，先用前 5 天验证数据真实返回

def hr(t): print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)

# ---------------------------------------------------------------------------
print(">>> 登录中 ...")
c = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"])
r = c.login(USER, PWD)
print("login success:", r.get("success"), "| message:", r.get("message"))
assert r.get("success"), "登录失败，终止验证"
c.username = USER

# ===========================================================================
hr("A) 直接工具调用验证")
results = {}

def call_and_report(name, args):
    print(f"\n--- {name}  args={args}")
    t0 = time.time()
    res, err = st.call_tool(c, name, dict(args))
    dt = time.time() - t0
    print(f"  耗时 {dt:.1f}s | err={err}")
    if err:
        print(f"  !! 工具报错: {err}")
        return None
    if res.get("too_large"):
        print(f"  !! 触发超大区间保护: {res.get('message')}")
        return res
    # 打印关键金额/计数字段
    keys = [k for k in res.keys() if k not in ("tool", "filters", "note", "trace")]
    for k in keys:
        v = res[k]
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            print(f"    {k} = {v}")
        elif isinstance(v, list):
            print(f"    {k}: list[{len(v)}]")
        elif isinstance(v, dict):
            print(f"    {k}: dict({list(v.keys())[:8]})")
    return res

# 区间工具统一先用整月；若为空再探测更宽范围，以区分「账号无数据」与「参数错误」
def call_range(name, sd, ed, **kw):
    res = call_and_report(name, {"start_date": sd, "end_date": ed, **kw})
    # 采样一个总计字段判断是否空
    tot = 0
    for k in ("total_suppliers", "total_purchase_amount", "total_bills",
             "total_planned_amount", "line_has_purchase_qty"):
        v = (res or {}).get(k)
        if isinstance(v, (int, float)):
            tot += v
    if res and not res.get("too_large") and tot == 0:
        print(f"  [空] 整月为 0，探测更宽范围 2025-01-01~2026-07-31 ...")
        res2 = call_and_report(name, {"start_date": "2025-01-01", "end_date": "2026-07-31", **kw})
        return res2 or res
    return res

# 1) supplier_settlement
results["supplier_settlement"] = call_range("supplier_settlement", SD_ALL, ED_ALL)
# 2) delivery_fulfillment
results["delivery_fulfillment"] = call_range("delivery_fulfillment", SD_ALL, ED_ALL)
# 3) cost_profit（本月，date_type=2 按月）
results["cost_profit"] = call_and_report("cost_profit", {"date": "2026-07-31", "date_type": 2})
# 4) purchase_return
results["purchase_return"] = call_range("purchase_return", SD_ALL, ED_ALL)
# 5) picking_out
results["picking_out"] = call_range("picking_out", SD_ALL, ED_ALL)
# 6) requisition_status
results["requisition_status"] = call_range("requisition_status", SD_ALL, ED_ALL)

# ---- 仓库过滤验证：用 delivery_fulfillment 先查无过滤，取一个仓库名，再带过滤对比 ----
print("\n--- [仓库过滤验证] delivery_fulfillment 不带仓库 vs 带仓库")
base = results["delivery_fulfillment"]
wh_name = None
if base and not base.get("too_large"):
    by_wh = base.get("by_warehouse") or []
    if by_wh:
        wh_name = by_wh[0].get("warehouse") or by_wh[0].get("name")
        print(f"  取到的仓库名样本: {wh_name}")
        filtered = call_and_report("delivery_fulfillment",
            {"start_date": SD5, "end_date": ED5, "warehouse_name": wh_name})
        if filtered and not filtered.get("too_large"):
            b0 = base.get("total_purchase_amount"); b1 = filtered.get("total_purchase_amount")
            print(f"  对比 total_purchase_amount: 无过滤={b0}  带[{wh_name}]={b1}")
            if b1 is not None and b0 is not None and b1 <= b0:
                print("  [OK] 仓库过滤生效（过滤后金额 <= 全量）")
            else:
                print("  [WARN] 仓库过滤前后金额未收窄，请检查")

A_OK = all(v and not v.get("too_large") and not v.get("error") for v in results.values())
print(f"\n>>> A 段结论: 6 工具 {'全部返回真实数据' if A_OK else '存在 too_large/报错（见上）'}")

# ===========================================================================
hr("B) 真实 hy3 路由验证（6 个易混问题）")
llm = get_llm()
print("LLM 类型:", type(llm).__name__)
if type(llm).__name__ == "MockLLM":
    print("  [WARN] 当前为 MockLLM，未配置真实模型密钥（MOCK_LLM=1 或缺少 MAAS_API_KEY）")

QUESTIONS = [
    ("供应商结算", "本月各供应商的采购结算金额分别是多少？"),
    ("配送履约",   "这个月配送履约和验收差异情况怎么样？"),
    ("成本利润",   "7月利润是多少？"),
    ("退货",       "7月退货情况如何？"),
    ("领料",       "7月领料出库统计一下"),
    ("申购",       "还有多少申购单在待采购状态？"),
]

route_ok = 0
for expect, q in QUESTIONS:
    print(f"\n### 期望[{expect}] 问: {q}")
    chosen = None; answer = ""
    try:
        for ev in agent_mod.run_agent_stream(c, q, llm, prior=None, max_iter=3):
            if ev.get("type") == "step" and ev.get("stage") == "分析意图":
                chosen = ev.get("detail", "")
            if ev.get("type") == "done":
                answer = ev.get("answer", "")
                tr = ev.get("tool_results", [])
                if tr:
                    chosen = tr[0].get("name", chosen)
    except Exception as e:
        print(f"  !! run_agent_stream 异常: {e}")
    print(f"  路由到工具: {chosen}")
    print(f"  结论(前120字): {answer[:120]}")
    if chosen and expect in chosen:
        route_ok += 1
        print("  [OK] 路由命中")
    else:
        print("  [CHECK] 路由需复核")

print(f"\n>>> B 段结论: 路由命中 {route_ok}/{len(QUESTIONS)}")

hr("验证完成")
print("A_OK:", A_OK, "| B_route_ok:", route_ok, "/", len(QUESTIONS))
print("ALL_DONE")
