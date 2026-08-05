#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 联动筛选器 · 回归测试（无网络，纯逻辑层）。

覆盖：
  T1 _derive_filters：日期区间 / 单日 / 仓库 三类筛选器推导正确
  T2 build_sections：每个 section 携带 key/tool/args/filters；同名工具去重 key
  T3 rerun 组合：模拟 call_tool 返回后 build_sections 产出单节（与 /api/ask/rerun 等价路径）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent as agent_mod
from semantic_tools import call_tool, TOOLS

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


print("T1 _derive_filters")
# 采购入库按仓库：start_date+end_date+warehouse_name
f1 = agent_mod._derive_filters({"name": "purchase_inbound_by_warehouse",
                                 "args": {"start_date": "2026-07-01", "end_date": "2026-07-31", "warehouse_name": "总仓"},
                                 "result": {}})
types1 = {x["type"] for x in f1}
check("采购入库按仓库→含 date+warehouse", types1 == {"date", "warehouse"}, str(types1))
date_f = next(x for x in f1 if x["type"] == "date")
wh_f = next(x for x in f1 if x["type"] == "warehouse")
check("date 当前值透传", date_f["start"] == "2026-07-01" and date_f["end"] == "2026-07-31")
check("warehouse 当前值透传", wh_f["value"] == "总仓")
check("param 名正确", date_f["start_param"] == "start_date" and wh_f["param"] == "warehouse_name")

# 库存快照：report_date(单日)+warehouse_name
f2 = agent_mod._derive_filters({"name": "stock_snapshot",
                                 "args": {"report_date": "2026-07-31", "warehouse_name": ""},
                                 "result": {}})
types2 = {x["type"] for x in f2}
check("库存快照→含 date_single+warehouse", types2 == {"date_single", "warehouse"}, str(types2))
single_f = next(x for x in f2 if x["type"] == "date_single")
check("date_single param=report_date", single_f["param"] == "report_date" and single_f["value"] == "2026-07-31")

# 库存按仓库：仅 warehouse_name（无日期参数）
f3 = agent_mod._derive_filters({"name": "inventory_by_warehouse", "args": {}, "result": {}})
types3 = {x["type"] for x in f3}
check("库存按仓库→仅 warehouse", types3 == {"warehouse"}, str(types3))

# 成本利润：date_ 单日（无 warehouse_name，成本利润为组织级口径，故意不含仓库）
f4 = agent_mod._derive_filters({"name": "cost_profit", "args": {}, "result": {}})
types4 = {x["type"] for x in f4}
check("成本利润→仅 date_single(date_，无仓库)", types4 == {"date_single"}, str(types4))
check("成本利润 date_single param=date_", next(x for x in f4 if x["type"] == "date_single")["param"] == "date_")

# 无日期无仓库的工具（如 dashboard_overview 仅 warehouse；food_inspect 有日期+仓库）
f5 = agent_mod._derive_filters({"name": "dashboard_overview", "args": {}, "result": {}})
check("驾驶舱→仅 warehouse", {x["type"] for x in f5} == {"warehouse"}, str(f5))


print("T2 build_sections 元数据")
fake_rank = {
    "tool": "rank_by_dimension", "dimension": "goods", "metric": "amount",
    "range": "2026-07-01~2026-07-31",
    "items": [{"name": "苹果", "amount": 100.0, "qty": 10.0},
              {"name": "香蕉", "amount": 50.0, "qty": 5.0}],
}
secs, warns = agent_mod.build_sections([
    {"name": "rank_by_dimension",
     "args": {"start_date": "2026-07-01", "end_date": "2026-07-31", "dimension": "goods", "metric": "amount"},
     "result": fake_rank}
])
check("产出 1 个 section", len(secs) == 1, f"len={len(secs)}")
if secs:
    s = secs[0]
    check("section 带 key", s.get("key") == "rank_by_dimension")
    check("section 带 tool", s.get("tool") == "rank_by_dimension")
    check("section 带 args", s.get("args", {}).get("dimension") == "goods")
    check("section 带 filters(非空)", bool(s.get("filters")))
    has_table = any(b["type"] == "table" for b in s["blocks"])
    has_chart = any(b["type"] == "chart" for b in s["blocks"])
    check("section 含 表+图", has_table and has_chart, f"table={has_table} chart={has_chart}")

# 错误结果分支也应带 key/tool/filters
secs_err, _ = agent_mod.build_sections([
    {"name": "stock_out_by_warehouse",
     "args": {"start_date": "2026-07-01", "end_date": "2026-07-31"},
     "result": {"error": "连接失败"}}
])
check("错误分支产出 1 section", len(secs_err) == 1)
if secs_err:
    check("错误 section 带 key/tool", secs_err[0].get("tool") == "stock_out_by_warehouse"
          and secs_err[0].get("key") == "stock_out_by_warehouse")

# 同名工具两次调用 → key 去重
secs_dup, _ = agent_mod.build_sections([
    {"name": "rank_by_dimension", "args": {"dimension": "goods"}, "result": fake_rank},
    {"name": "rank_by_dimension", "args": {"dimension": "supplier"}, "result": fake_rank},
])
keys = [s["key"] for s in secs_dup]
check("同名工具 key 去重(#1/#2)", keys == ["rank_by_dimension", "rank_by_dimension#1"], str(keys))


print("T3 rerun 组合（等价 /api/ask/rerun 路径）")
# 用假 call_tool 模拟重查：直接喂入结果，验证 build_sections 单节产出
def fake_call_tool(client, name, args):
    return fake_rank, None
orig = call_tool.__wrapped__ if hasattr(call_tool, "__wrapped__") else None
import semantic_tools as st
st.call_tool = fake_call_tool
try:
    name = "rank_by_dimension"
    args = {"start_date": "2026-06-01", "end_date": "2026-06-30", "dimension": "goods", "metric": "amount"}
    result, err = st.call_tool(None, name, args)
    secs_r, warns_r = agent_mod.build_sections([{"name": name, "args": args, "result": result}])
    check("rerun→产出 1 section", len(secs_r) == 1, f"len={len(secs_r)}")
    if secs_r:
        check("rerun section tool 正确", secs_r[0]["tool"] == name)
        check("rerun section args 为新基线", secs_r[0]["args"]["start_date"] == "2026-06-01")
finally:
    if orig is not None:
        st.call_tool = orig


print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
