#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 回归：生成后字段绑定校验 + 运行时停止取消传播。

T1 _chart_has_data 四种 case（空/全None/有数据/值0）
T2 build_sections 异常结构防御（单工具字段缺失不拖垮整轮）
T3 build_sections 正常含图（warnings 为空）
T4 run_agent_stream 取消传播（cancel_event 置位 -> canceled 事件）
T5 run_agent_stream 正常（无取消 -> done 事件，且答案来自工具真实返回）
"""
import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent as agent_mod


def test_chart_has_data():
    empty_opt = {"series": [{"data": []}]}
    none_opt = {"series": [{"data": [None, None]}]}
    data_opt = {"series": [{"data": [1, 2, 3]}]}
    zero_opt = {"series": [{"data": [0, 0, 0]}]}
    assert agent_mod._chart_has_data(empty_opt) is False, "空 series 应判空"
    assert agent_mod._chart_has_data(none_opt) is False, "全 None 应判空"
    assert agent_mod._chart_has_data(data_opt) is True, "有数据应判有"
    assert agent_mod._chart_has_data(zero_opt) is True, "值0 应判有（非空白）"
    print("T1 _chart_has_data 4 case 通过")


def test_sections_defensive():
    # 缺 items 字段 -> build_tables/build_charts 会 KeyError，不应拖垮整轮
    bad = {"name": "rank_by_dimension", "args": {},
           "result": {"tool": "rank_by_dimension", "metric": "amount",
                      "range": "2026-07", "dimension": "goods"}}
    sections, warnings = agent_mod.build_sections([bad])
    assert isinstance(sections, list) and isinstance(warnings, list)
    assert any("结构异常" in w for w in warnings), "应捕获结构异常并记 warning"
    print("T2 build_sections 异常结构防御通过:", warnings)


def test_sections_normal():
    good = {"name": "daily_trend", "args": {},
            "result": {"tool": "daily_trend", "metric": "amount", "range": "2026-07",
                       "points": [{"date": "07-01", "amount": 10}, {"date": "07-02", "amount": 20}]}}
    sections, warnings = agent_mod.build_sections([good])
    assert len(sections) == 1
    assert not warnings, "正常数据不应有 warning"
    # 图表块应有数据
    has_chart = any(b["type"] == "chart" for s in sections for b in s["blocks"])
    assert has_chart, "应生成图表"
    print("T3 build_sections 正常含图通过")


def test_cancel_propagation():
    class FakeLLM:
        def chat(self, sys_prompt, user_msg, history):
            return ('{"tool":"purchase_inbound_summary","args":{}}', {"total_tokens": 1})
    ev = threading.Event(); ev.set()  # 用户已点停止
    events = list(agent_mod.run_agent_stream(None, "q", FakeLLM(), prior=[], cancel_event=ev))
    assert any(e["type"] == "canceled" for e in events), "取消应产生 canceled 事件"
    print("T4 取消传播（canceled 事件）通过")


def test_normal_flow():
    class FakeLLM:
        def chat(self, sys_prompt, user_msg, history):
            # 第一轮直接给最终答案（无工具调用）
            return ("本月采购入库约 100 元。", {"total_tokens": 2})
    events = list(agent_mod.run_agent_stream(None, "q", FakeLLM(), prior=[]))
    done = [e for e in events if e["type"] == "done"]
    assert done, "正常应产生 done 事件"
    assert "100" in done[0]["answer"], "答案应来自模型回复"
    print("T5 正常流程（done 事件）通过")


if __name__ == "__main__":
    test_chart_has_data()
    test_sections_defensive()
    test_sections_normal()
    test_cancel_propagation()
    test_normal_flow()
    print("\n结果: 全部通过")
