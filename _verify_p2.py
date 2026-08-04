# -*- coding: utf-8 -*-
"""P2 回归测试：预警日期口径统一 + aliases 意图召回 + GLOSSARY 去重。

全部用 mock client，不走外网。
"""
import json

import metrics_registry as M
import semantic_tools as S

FAIL = []


def check(name, cond, extra=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + (("  " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


class MockClient:
    """记录所有调用参数的假客户端。"""

    def __init__(self, records=None, stat=None):
        self.calls = []
        self._records = records if records is not None else []
        self._stat = stat if stat is not None else {
            "waitRectifyQty": 5, "rectifiedQty": 3, "ignoreQty": 1, "confirmedQty": 1}

    def page_early_warn_stat(self, params=None):
        self.calls.append(("pageAndStat", dict(params or {})))
        return {"success": True, "data": {
            "records": self._records, "total": len(self._records),
            "waitRectifyQty": 99, "rectifiedQty": 99, "ignoreQty": 99, "confirmedQty": 99}}

    def get_early_warn_stat_item(self, params=None):
        self.calls.append(("getStatItem", dict(params or {})))
        return {"success": True, "data": dict(self._stat)}

    def query_warehouses(self, params=None):
        return {"success": True, "data": {"records": [
            {"warehouseUuid": "wh-1", "warehouseName": "上海奥运餐厅"}], "total": 1}}


RECS = [
    {"category": "fs", "type": "留样不足", "status": 0, "warehouseName": "上海奥运餐厅",
     "content": "留样量不足", "createTime": "2026-08-02 09:00:00",
     "startDate": "2026-07-01", "endDate": "2026-08-10"},
    {"category": "certificate", "type": "证照过期", "status": 1, "warehouseName": "北京店",
     "content": "健康证过期", "createTime": "2026-08-03 10:00:00",
     "startDate": "2026-07-05", "endDate": "2026-08-06"},
]


def t1_date_params():
    """warning_center 日期参数与默认区间口径。"""
    # 1) 显式区间 -> startDate/endDate，绝不出现 beginDate
    c = MockClient(RECS)
    S.warning_center(c, start_date="2026-08-01", end_date="2026-08-04")
    page = [p for k, p in c.calls if k == "pageAndStat"][0]
    check("warning_center 用 startDate/endDate",
          page.get("startDate") == "2026-08-01" and page.get("endDate") == "2026-08-04")
    check("warning_center 绝不传 beginDate（接口会忽略导致全量）",
          "beginDate" not in page, sorted(page.keys()))

    # 2) 不传日期 -> 默认当前自然月（不再无区间全量拉取）
    c2 = MockClient(RECS)
    r2 = S.warning_center(c2)
    page2 = [p for k, p in c2.calls if k == "pageAndStat"][0]
    sd, ed = S._default_month_range()
    check("不传日期默认当前自然月（防全量）",
          page2.get("startDate") == sd and page2.get("endDate") == ed,
          f"{page2.get('startDate')}~{page2.get('endDate')}")
    check("filters 回显 date_type=推送日期",
          r2["filters"].get("date_type", "").startswith("推送日期"), r2["filters"].get("date_type"))

    # 3) 四态取 getStatItem（不取 pageAndStat 顶层的 99）
    check("四态聚合走 getStatItem 全局聚合",
          r2["status_agg"].get("待整改") == 5 and 99 not in r2["status_agg"].values(),
          r2["status_agg"])
    check("total = 四态之和（不受分页截断）", r2["total"] == 10, r2["total"])

    # 4) getStatItem 不带 status（保证返回全局四态）
    c3 = MockClient(RECS)
    S.warning_center(c3, status=0)
    stat = [p for k, p in c3.calls if k == "getStatItem"][0]
    check("getStatItem 不带 status（始终全局四态）", "status" not in stat, sorted(stat.keys()))
    page3 = [p for k, p in c3.calls if k == "pageAndStat"][0]
    check("明细查询仍带 status 过滤", page3.get("status") == 0)

    # 5) 与 food_safety_alert 参数口径一致
    c4 = MockClient(RECS)
    S.food_safety_alert(c4, start_date="2026-08-01", end_date="2026-08-04")
    fpage = [p for k, p in c4.calls if k == "pageAndStat"][0]
    check("两个预警工具日期参数键一致",
          {"startDate", "endDate"} <= set(fpage) and "beginDate" not in fpage)

    # 6) getStatItem 异常时回退 pageAndStat 顶层，不抛错
    class Broken(MockClient):
        def get_early_warn_stat_item(self, params=None):
            raise RuntimeError("boom")
    r6 = S.warning_center(Broken(RECS))
    check("getStatItem 异常时回退顶层字段不崩", r6["status_agg"].get("待整改") == 99, r6["status_agg"])


def t2_registry_consistency():
    """注册表口径一致性。"""
    spec = M.build_caliber_spec()
    check("口径块无自相矛盾旧句", "仍用 startDate/endDate 业务周期" not in spec)
    check("口径块声明两工具已统一", "两个预警工具已完全统一" in spec)
    wc, fs = M.METRICS["warning_center"], M.METRICS["food_safety_alert"]
    check("warning_center 元数据标注推送日期", "推送日期" in wc["fixed_filters"]["date_field"])
    check("两工具 date_field 语义不再互相矛盾",
          ("推送日期" in wc["fixed_filters"]["date_field"]) ==
          ("推送日期" in fs["fixed_filters"]["date_field"]))
    d = [x for x in S.TOOL_SCHEMAS if x["name"] == "warning_center"][0]["description"]
    check("TOOL_SCHEMAS 描述已同步注册表", d == wc["description"])


def t3_aliases_recall():
    """aliases 意图召回增强。"""
    if not hasattr(S, "recall_tools_by_alias"):
        check("recall_tools_by_alias 已实现", False, "未实现")
        return
    cases = [
        ("有哪些证照快到期了", "warning_center"),
        ("库存分类占比", "inventory_by_category"),
        ("哪个供应商供货金额最多", None),   # 只要能召回若干候选即可
    ]
    for q, expect in cases:
        hits = S.recall_tools_by_alias(q)
        names = [h[0] for h in hits]
        if expect:
            check(f"别名召回「{q}」→ {expect}", expect in names, names[:5])
        else:
            check(f"别名召回「{q}」有候选", len(names) > 0, names[:5])
    check("无关问句不硬凑候选", S.recall_tools_by_alias("今天天气怎么样") == [] or
          len(S.recall_tools_by_alias("今天天气怎么样")) <= 2,
          S.recall_tools_by_alias("今天天气怎么样")[:3])
    # 别名索引不应有跨工具冲突导致的空 key
    idx = S.build_alias_index()
    check("别名索引非空且无空键", len(idx) > 30 and all(k.strip() for k in idx), len(idx))


def t4_glossary_dedup():
    import semantic_layer as L
    import metrics_registry as MR
    p = L.build_system_prompt()
    check("system prompt 含口径权威块", "业务口径统一说明" in p)
    # 纯口径表述应只由权威块 + 工具自身描述承载，GLOSSARY 不再重复
    check("GLOSSARY 不再重复越库口径", L.GLOSSARY.count("purchaseCrossIn") == 0,
          L.GLOSSARY.count("purchaseCrossIn"))
    check("GLOSSARY 不再重复金额估算公式",
          "单价(price) × 数量(qty)" not in L.GLOSSARY)
    check("GLOSSARY 不再重复库存零值口径",
          "库存数量为 0 的记录为无效数据" not in L.GLOSSARY)
    check("权威块仍完整承载这些口径",
          all(k in MR.CALIBER_NOTES for k in
              ("purchase_cross_in", "amount_est", "inventory_zero_qty",
               "unit_not_addable", "count_def", "server_side_accurate")))
    # 保留项：工具选择与措辞规范不应被误删
    for keep in ("绝对禁止称为「销售额」", "rank_by_dimension", "warehouse_name",
                 "only_inbound", "period_compare"):
        check(f"保留工具选择/措辞指导：{keep[:14]}", keep in p)
    check("prompt 含当前日期注入", "当前日期" in p)


class FakeLLM:
    """按预设脚本依次返回回复，并记录收到的 system prompt。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.systems = []
        self.users = []

    def chat(self, system, user, history=None):
        self.systems.append(system)
        self.users.append(user)
        r = self.replies.pop(0) if self.replies else "（没有更多脚本）"
        return r, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def t5_agent_integration():
    """召回提示注入 + 未选工具时的兜底纠偏，均不破坏既有链路。"""
    import agent as A

    tool_json = ('{"tool":"warning_center","args":{"start_date":"2026-08-01",'
                 '"end_date":"2026-08-04"}}')

    # 1) 正常链路：模型首轮就选对工具 → 不应触发纠偏，且 system 带候选提示
    llm = FakeLLM([tool_json, "8月1-4日共 10 条预警，其中待整改 5 条。"])
    ev = list(A.run_agent_stream(MockClient(RECS), "有哪些证照快到期了", llm))
    stages = [e.get("stage") for e in ev if e.get("type") == "step"]
    done = [e for e in ev if e.get("type") == "done"][0]
    check("正常链路仍能取数出结论", len(done["tool_results"]) == 1, stages)
    check("首轮 system 注入候选提示", "候选工具提示" in llm.systems[0])
    check("有数据后不再注入候选提示（避免噪声）",
          len(llm.systems) > 1 and "候选工具提示" not in llm.systems[1])
    check("正常链路不触发纠偏", "意图纠偏" not in stages, stages)
    check("过程展示含候选召回", "候选召回" in stages, stages)
    check("问句原文未被提示污染", llm.users[0] == "有哪些证照快到期了", llm.users[0])

    # 2) 模型漏识别取数意图 → 提示候选后重试一次并成功取数
    llm2 = FakeLLM(["抱歉，我不清楚你想查什么。", tool_json, "共 10 条预警，待整改 5 条。"])
    ev2 = list(A.run_agent_stream(MockClient(RECS), "有哪些证照快到期了", llm2))
    stages2 = [e.get("stage") for e in ev2 if e.get("type") == "step"]
    done2 = [e for e in ev2 if e.get("type") == "done"][0]
    check("漏识别时触发意图纠偏", "意图纠偏" in stages2, stages2)
    check("纠偏后成功取到真实数据", len(done2["tool_results"]) == 1)
    check("纠偏只重试一次", stages2.count("意图纠偏") == 1)

    # 3) 闲聊无召回 → 不注入提示、不纠偏、只调一次模型
    llm3 = FakeLLM(["你好，我是后厨管家问数助手。"])
    ev3 = list(A.run_agent_stream(MockClient(RECS), "你好", llm3))
    stages3 = [e.get("stage") for e in ev3 if e.get("type") == "step"]
    check("闲聊不注入候选提示", "候选工具提示" not in llm3.systems[0])
    check("闲聊不触发纠偏", "意图纠偏" not in stages3 and len(llm3.systems) == 1, stages3)


def main():
    print("=" * 30, "T1 预警日期口径")
    t1_date_params()
    print("=" * 30, "T2 注册表一致性")
    t2_registry_consistency()
    print("=" * 30, "T3 aliases 召回")
    t3_aliases_recall()
    print("=" * 30, "T4 GLOSSARY 去重")
    t4_glossary_dedup()
    print("=" * 30, "T5 agent 集成")
    t5_agent_integration()
    print("\n结果:", "全部通过" if not FAIL else f"{len(FAIL)} 项失败: {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
