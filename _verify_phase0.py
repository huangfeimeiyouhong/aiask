#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 0 验证：用真实账号登录并调用 3 个新聚合工具，确认接口与字段。"""
import sys, json
from datetime import date, timedelta

BASE = "/Users/phil/WorkBuddy/2026-07-16-11-31-47/ai_qa_system"
sys.path.insert(0, BASE)

from hcg_client import HCGClient
import semantic_tools as st
import config

c = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"])
r = c.login("at0001", "at123456@")
print("login success:", r.get("success"), "| message:", r.get("message"))
assert r.get("success"), "登录失败"

t = date.today()
sd = t.replace(day=1).strftime("%Y-%m-%d")
ed = t.strftime("%Y-%m-%d")

print("\n=== purchase_stat (本月 %s~%s) ===" % (sd, ed))
res, err = st.call_tool(c, "purchase_stat", {"start_date": sd, "end_date": ed})
print("err:", err)
if res:
    for k in ("in_amount_total", "in_qty_total", "cross_amount_total", "cross_qty_total",
              "purchase_amount_incl_cross", "purchase_qty_incl_cross",
              "out_amount_total", "out_qty_total", "sub_amount", "sub_qty", "sub_amount_raw"):
        print(f"  {k} = {res.get(k)}")

print("\n=== purchase_ledger (本月前5天, 防响应过大) ===")
ed5 = (t.replace(day=1) + timedelta(days=4)).strftime("%Y-%m-%d")
res, err = st.call_tool(c, "purchase_ledger", {"start_date": sd, "end_date": ed5})
print("err:", err)
if res:
    print("  summary:", res.get("summary"))
    print("  total_details:", res.get("total_details"), "| processed:", res.get("processed"),
          "| truncated:", res.get("truncated"))
    print("  by_category_top[:3]:", res.get("by_category_top")[:3])
    print("  by_supplier_top[:3]:", res.get("by_supplier_top")[:3])
    print("  details_sample[:2]:", res.get("details_sample")[:2])

print("\n=== stock_snapshot (今天 %s) ===" % ed)
res, err = st.call_tool(c, "stock_snapshot", {"report_date": ed})
print("err:", err)
if res:
    print("  summary:", res.get("summary"))
    print("  by_category count:", len(res.get("by_category", [])),
          "| by_warehouse count:", len(res.get("by_warehouse", [])),
          "| by_goods_top count:", len(res.get("by_goods_top", [])))
    print("  fetched_records:", res.get("fetched_records"), "| truncated:", res.get("truncated"))
    print("  by_category[:3]:", res.get("by_category")[:3])

print("\nALL_DONE")
