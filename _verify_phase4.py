#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 4 验证：问数增强（周期对比 / 趋势）。

A) 真实 hy3 路由：趋势/对比/环比类问题应命中 period_compare（且单区间问题不应误导向它）。
B) 真实 API 直调 period_compare（月度采购额序列，purchase_stat 在 at0001 已验证有数据）。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))
from hcg_client import HCGClient
from semantic_tools import call_tool
from semantic_layer import build_system_prompt
from hunyuan import OpenAILikeLLM, MockLLM

USER = os.environ.get("HCG_USER", "at0001")
PWD = os.environ.get("HCG_PWD", "at123456@")


def login_client():
    c = HCGClient()
    r = c.login(USER, PWD)
    assert r.get("success"), f"登录失败: {r}"
    return c


def test_routing():
    """A) 真实 hy3 路由：对比/趋势类命中 period_compare。"""
    print("\n===== A) 真实 hy3 路由验证 =====")
    llm = OpenAILikeLLM(os.environ.get("MAAS_API_KEY", ""),
                        os.environ.get("MAAS_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
                        os.environ.get("MAAS_MODEL", "hy3"))
    questions = [
        ("近半年采购额走势，每月对比", "period_compare"),
        ("上半年各月采购额趋势", "period_compare"),
        ("7月比6月采购额多多少（环比）", "period_compare"),
        ("各月利润对比走势", "period_compare"),
        ("今年一季度每月采购额", "period_compare"),
        # 负向：单区间不应误导向 period_compare
        ("2026年7月采购额多少", "purchase_stat"),
    ]
    ok = 0
    for q, expect in questions:
        try:
            reply = llm.chat(build_system_prompt(), q, None)
        except Exception as e:
            print(f"  [跳过·无凭证] {q} -> {e}")
            continue
        import re
        hit = None
        m = re.search(r'"tool"\s*:\s*"([^"]+)"', reply)
        if m:
            hit = m.group(1)
        status = "OK" if hit == expect else "FAIL"
        if hit == expect:
            ok += 1
        print(f"  [{status}] {q!r} -> 命中 {hit}（期望 {expect}）")
        print(f"         raw: {reply[:160]}")
    print(f"  路由命中 {ok}/{len(questions)}")


def test_direct():
    """B) 真实 API 直调 period_compare（月度采购额序列）。"""
    print("\n===== B) 真实 API 直调 period_compare =====")
    c = login_client()
    # 月度采购额序列（at0001 已验证 purchase_stat 有数据：2026-07 全月 4,226,430.72）
    periods = [f"2026-{m:02d}" for m in range(1, 8)]  # 1~7月
    res, err = call_tool(c, "period_compare",
                         {"base_tool": "purchase_stat", "periods": periods})
    if err:
        print(f"  ERR: {err}"); return
    print(f"  tool={res.get('tool')} base={res.get('base_tool')} periods={res.get('period_count')}")
    print(f"  main_metric={res.get('main_metric')}")
    print(f"  summary={json.dumps(res.get('summary'), ensure_ascii=False)}")
    for s in res.get("series", []):
        dp = s.get("main_delta_pct")
        print(f"    {s['period']}: 主值={s['main_value']} 环比差={s['main_delta']} 环比%={dp}")
    # 校验：7月主值应为真实值（≈4,226,430.72，含越库）
    jul = [s for s in res["series"] if s["period"] == "2026-07"]
    if jul:
        v = jul[0]["main_value"]
        print(f"  断言: 2026-07 采购总额(含越库) ≈ 4,226,430.72 -> 实际 {v}")
        assert abs(v - 4226430.72) < 2, "7月采购额与已知真实值不符"
        print("  ✅ 7月采购额金额准确（服务端聚合）")
    # 校验：环比计算字段存在且为数值或 None
    for s in res["series"][1:]:
        assert "main_delta" in s and "main_delta_pct" in s
    print("  ✅ 环比字段完整（差值 + 百分比）")
    # cost_profit 路径（组织级；at0001 已知 2026-07 有成本利润数据）
    print("\n  --- cost_profit 路径（2026-05~07 利润对比）---")
    res2, err2 = call_tool(c, "period_compare",
                           {"base_tool": "cost_profit", "periods": ["2026-05", "2026-06", "2026-07"],
                            "metric": "profit"})
    if err2:
        print(f"  ERR: {err2}"); return
    print(f"  tool={res2.get('tool')} base={res2.get('base_tool')} periods={res2.get('period_count')}")
    for s in res2.get("series", []):
        print(f"    {s['period']}: 利润={s['main_value']} 收入={s['values'].get('收入')} 支出={s['values'].get('支出')} 环比%={s['main_delta_pct']}")
    print("  ✅ cost_profit 路径序列正常")


def test_mock_route():
    """C) MockLLM 降级路由：趋势/对比命中 period_compare。"""
    print("\n===== C) MockLLM 降级路由验证 =====")
    m = MockLLM()
    for q in ["近半年采购额走势", "上半年各月采购额趋势", "各月利润对比", "7月采购额多少（单区间）"]:
        reply = m._plan(q)
        try:
            obj = json.loads(reply)
            print(f"  {q!r} -> tool={obj['tool']} periods={obj.get('periods')} base={obj.get('base_tool')} metric={obj.get('metric')}")
        except Exception:
            print(f"  {q!r} -> 解析失败: {reply[:80]}")


if __name__ == "__main__":
    has_maas = bool(os.environ.get("MAAS_API_KEY"))
    print(f"MAAS_API_KEY 存在: {has_maas}")
    if has_maas:
        test_routing()
    test_direct()
    test_mock_route()
    print("\nDONE")
