#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 编排 —— 「意图 → 工具调用 → 真实数据 → 自然语言回答」循环。

- 第一轮：把用户问题交给 LLM，若返回 {"tool":..,"args":..} 则执行语义工具。
- 第二轮：把工具真实返回数据回灌 LLM，生成最终自然语言结论。
- 强制只基于接口真实返回，无数据不编造。
"""

import json
import re
from semantic_layer import build_system_prompt
from semantic_tools import (call_tool, TOOLS, TOOL_LABELS,
                            build_recall_hint, recall_tools_by_alias)


def _extract_tool_call(text: str):
    """从 LLM 回复中解析 {"tool":..,"args":..}。容错：去除 ``` 与多余文字。"""
    if not text:
        return None
    s = text.strip()
    # 去 markdown 代码围栏
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # 尝试整段解析
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "tool" in obj:
            return {"name": obj["tool"], "args": obj.get("args", {}) or {}}
    except Exception:
        pass
    # 尝试截取第一个 {...}
    m = re.search(r"\{[^{}]*\"tool\"[^{}]*\}", s, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return {"name": obj["tool"], "args": obj.get("args", {}) or {}}
        except Exception:
            pass
    return None


def _summarize_result(tool, result):
    """生成工具返回结果的单行摘要，用于执行过程展示。"""
    if result.get("error"):
        return "工具返回错误：" + result["error"]
    if result.get("too_large"):
        est = result.get("estimated")
        return f"区间数据量过大（约 {est} 条），已返回友好提示，未拉取全量"
    if tool == "purchase_inbound_summary":
        return (f"采购入库 {result['count']} 笔，估算金额 ¥{result['total_amount_est']:.2f}，"
                f"合计数量 {result['total_qty']}；时间范围 {result['filters']['start_date']}~{result['filters']['end_date']}")
    if tool == "rank_by_dimension":
        n = len(result.get("items", []))
        top = ""
        if result.get("items"):
            top = f"榜首：{result['items'][0]['name']}（{result['items'][0].get(result['metric'])}）"
        return f"按 {result['dimension']} 维度、指标 {result['metric']} 排行，共 {n} 条；{top}"
    if tool == "daily_trend":
        pts = result.get("points", [])
        return f"按日趋势共 {len(pts)} 个数据点，时间范围 {result['range']}"
    if tool == "stock_warning":
        return (f"库存预警：已过期 {result['outdated_count']} 条，临期预警中 {result['warning_count']} 条")
    if tool == "inventory_by_warehouse":
        n = len(result.get("warehouses", []))
        return (f"库存按仓库汇总：共 {n} 个仓库，商品种类 {result['total_goods']}，"
                f"合计数量 {result['total_qty']}，估算金额 ¥{result['total_amount_est']:.2f}")
    if tool == "inventory_by_category":
        n = len(result.get("categories", []))
        top = ""
        if result.get("categories"):
            top = f"占比最高：{result['categories'][0]['category']}（{result['categories'][0]['qty_ratio']}%）"
        return (f"库存分类占比：共 {n} 个一级分类，商品种类 {result['total_goods']}，"
                f"合计数量 {result['total_qty']}，估算金额 ¥{result['total_amount_est']:.2f}。{top}")
    if tool == "purchase_inbound_by_warehouse":
        n = len(result.get("warehouses", []))
        return (f"采购入库按仓库汇总：{n} 个仓库，{result['total_count']} 笔，"
                f"估算金额 ¥{result['total_amount_est']:.2f}，合计数量 {result['total_qty']}")
    if tool == "stock_out_by_warehouse":
        n = len(result.get("warehouses", []))
        return (f"出库按仓库汇总：{n} 个仓库，{result['total_count']} 笔，"
                f"估算金额 ¥{result['total_amount_est']:.2f}，合计数量 {result['total_qty']}")
    if tool == "purchase_stat":
        return (f"采购统计：采购总额(含越库) ¥{result['purchase_amount_incl_cross']:.2f}，"
                f"其中入库 ¥{result['in_amount_total']:.2f}、越库 ¥{result['cross_amount_total']:.2f}；"
                f"出库 ¥{result['out_amount_total']:.2f}，结余 ¥{result['sub_amount']:.2f}；"
                f"范围 {result['filters']['start_date']}~{result['filters']['end_date']}")
    if tool == "purchase_ledger":
        s = result.get("summary", {})
        return (f"采购台账：采购总额 ¥{s.get('pur_amount', 0):.2f}，采购次数 {s.get('pur_count')}，"
                f"入库项数 {s.get('stock_in_count')}，供应商数 {s.get('supplier_count')}；"
                f"商品/供应商/分类 TOP{result['filters']['top_n']} 已聚合")
    if tool == "stock_snapshot":
        sm = result.get("summary", {})
        return (f"进销存库存快照({result['filters']['report_date']})：期末库存金额 ¥{sm.get('stock_amount',0):.2f}，"
                f"期末库存数量 {sm.get('stock_qty',0)}；采购入库 ¥{sm.get('purchase_in_amount',0):.2f}，"
                f"领料出库 ¥{sm.get('stock_out_amount',0):.2f}")
    if tool == "supplier_settlement":
        return (f"供应商结算统计：{result['total_suppliers']} 家供应商，"
                f"入库总金额 ¥{result['total_purchase_amount']:.2f}，结算总金额 ¥{result['total_settle_amount']:.2f}，"
                f"实退总金额 ¥{result['total_return_amount']:.2f}；范围 {result['filters']['start_date']}~{result['filters']['end_date']}")
    if tool == "delivery_fulfillment":
        f = result.get("fulfillment", {})
        return (f"配送履约：待分拣 {f.get('notSorting',0)} / 待发货 {f.get('notDelivery',0)} / "
                f"待验收 {f.get('notStockIn',0)} / 已验收 {f.get('stockIned',0)}；"
                f"采购金额 ¥{result['total_purchase_amount']:.2f}，入库金额 ¥{result['total_stock_in_amount']:.2f}，"
                f"验收差异金额 ¥{result['total_diff_amount']:.2f}；范围 {result['filters']['start_date']}~{result['filters']['end_date']}")
    if tool == "cost_profit":
        m = result["filters"]["metric"]
        parts = []
        if result.get("income"):
            parts.append(f"收入 ¥{result['income']['total_amount']:.2f}")
        if result.get("expense"):
            parts.append(f"支出 ¥{result['expense']['total_amount']:.2f}")
        if result.get("profit") is not None:
            parts.append(f"利润 ¥{result['profit']:.2f}")
        return (f"成本利润（{m}，{result['filters']['date']}，dateType={result['filters']['date_type']}）："
                + "，".join(parts))
    if tool == "purchase_return":
        return (f"退货统计：{result['total_bills']} 单，应退 ¥{result['total_return_amount']:.2f}，"
                f"实退 ¥{result['total_actual_return_amount']:.2f}；范围 {result['filters']['start_date']}~{result['filters']['end_date']}")
    if tool == "picking_out":
        return (f"领料出库：{result['total_bills']} 单，计划 ¥{result['total_planned_amount']:.2f}，"
                f"实际出库 ¥{result['total_actual_out_amount']:.2f}（已出库/完成 {result['completed_bills']} 单）；"
                f"范围 {result['filters']['start_date']}~{result['filters']['end_date']}")
    if tool == "requisition_status":
        return (f"申购验收：明细 已采购 {result['line_has_purchase_qty']} / 待采购 {result['line_not_purchase_qty']} / "
                f"已驳回 {result['line_rejected_qty']}；申购单 {result['total_bills']} 单，金额 ¥{result['total_apply_amount']:.2f}")
    # ---- Phase 2 食安管理域 ----
    if tool == "health_certificate":
        status = result.get("filters", {}).get("status")
        if status is not None:
            label = {0: "已停用", 1: "正常", 2: "即将到期", 3: "已过期"}.get(status, f"状态{status}")
            return f"健康证合规（已按「{label}」筛选）：共 {result.get('total',0)} 人，明细如下。"
        d = result.get("distribution", {})
        return (f"健康证合规：共 {result.get('total',0)} 人，正常 {d.get('normalQty',0)} / 即将到期 {d.get('aboutToExpireQty',0)} / "
                f"已过期 {d.get('overdueQty',0)} / 已停用 {d.get('disableQty',0)}")
    if tool == "food_inspect":
        return (f"食安巡检（{result.get('inspect_type_label','')}）：共 {result.get('total_bills',0)} 单，"
                f"完成率 {result.get('completion_rate',0)}%，不符合项 {result.get('total_nc_qty',0)} 个（涉及 {result.get('nc_bills',0)} 单）")
    if tool == "sample_retention":
        c = result.get("counts", {})
        return (f"留样管理：待存入 {c.get('待存入',0)} / 待取出 {c.get('待取出',0)} / 留样中 {c.get('留样中',0)} / "
                f"已取出 {c.get('已取出',0)}（合规留存 {result.get('active_retained',0)}）")
    if tool == "morning_check":
        return (f"晨检记录：合格 {result.get('qualified_yes',0)} / 不合格 {result.get('qualified_no',0)} / 在岗 {result.get('total_qty',0)}，"
                f"合格率 {result.get('qualified_rate',0)}%")
    if tool == "detection_report":
        return (f"检测报告：共 {result.get('total',0)} 条，合格 {result.get('qualified_yes',0)} / 不合格 {result.get('qualified_no',0)}，"
                f"合格率 {result.get('qualified_rate',0)}%")
    if tool == "food_additive":
        return (f"食品添加剂：共 {result.get('total',0)} 条记录，超标 {result.get('over_standard_cnt',0)} 条")
    if tool == "warning_center":
        sa = result.get("status_agg", {})
        return (f"综合预警：共 {result.get('total',0)} 条；待整改 {sa.get('待整改',0)} / 已整改 {sa.get('已整改',0)} / "
                f"已忽略 {sa.get('已忽略',0)} / 已确认 {sa.get('已确认',0)}。待整改 TOP {len(result.get('pending_top',[]))} 条。")
    if tool == "device_alarm_index":
        return (f"环境设备告警指数：累计 {result.get('total_alarms',0)} 次"
                f"（温度/湿度/烟雾/燃气/水浸/AI巡检）。")
    if tool == "device_alarm_detail":
        bs = result.get("by_status", [])
        unhandled = next((b["count"] for b in bs if b["status"] == "未处理"), 0)
        return (f"环境设备告警明细：共 {result.get('total',0)} 条；未处理 {unhandled} 条；"
                f"未处理/已处理 TOP {len(result.get('unresolved_top',[]))} 条。")
    if tool == "period_compare":
        s = result.get("summary", {})
        mm = result.get("main_metric", "")
        fv = s.get("first_value"); lv = s.get("last_value")
        fv_s = f"¥{fv:,.2f}" if isinstance(fv, (int, float)) else str(fv)
        lv_s = f"¥{lv:,.2f}" if isinstance(lv, (int, float)) else str(lv)
        return (f"周期对比（{result.get('base_tool')}·{mm}）：{result.get('period_count')} 个周期，"
                f"从 {s.get('first_period')}（{fv_s}）到 {s.get('last_period')}（{lv_s}）；"
                f"环比上升 {s.get('rising_count')} 期、下降 {s.get('falling_count')} 期")
    if tool == "dashboard_overview":
        tm = result.get("today_metrics", {})
        pa = tm.get("purchase_amount", {})
        mc = tm.get("morning_check", {})
        wait = result.get("wait_processed", {})
        return (f"经营驾驶舱：今日采购金额 ¥{pa.get('today',0):.2f}（日同比 {pa.get('day_ratio',0)}%），"
                f"验收金额 ¥{tm.get('stock_in_amount',{}).get('today',0):.2f}，"
                f"留样 {tm.get('sample_count',{}).get('today',0)} 项，晨检 {mc.get('today',0)} 人；"
                f"本月待处理 采购 {wait.get('pur_count',0)} 单/退货 {wait.get('pur_return_count',0)} 单，"
                f"食安概况 {len(result.get('fs_overview',[]))} 项。")
    if tool == "purchase_price_compare":
        return (f"采购价对比（{result['filters']['start_date']}~{result['filters']['end_date']}）："
                f"共 {result.get('total',0)} 条，超平台价 {result.get('over_count',0)} 条，"
                f"超价采购额估算 ¥{result.get('over_amount_est',0):.2f}；"
                f"已按超出比例降序列出 TOP {len(result.get('rows',[]))}。")
    if tool == "stock_month_report":
        s = result.get("summary", {})
        return (f"库存月报（{result['filters']['report_date']}）：期末金额 ¥{s.get('stock_amount',0):.2f}，"
                f"期末数量 {s.get('stock_qty',0)}；入库 ¥{s.get('stock_in_amount',0):.2f}、"
                f"出库 ¥{s.get('stock_out_amount',0):.2f}；商品明细 TOP {len(result.get('rows',[]))}。金额服务端聚合，准确非估算。")
    if tool == "food_safety_alert":
        sa = result.get("status_agg", {})
        fl = result.get("filters", {})
        total = result.get("total", 0)
        wait = sa.get("待整改", 0)
        comp = round((sa.get("已忽略", 0) + sa.get("已确认", 0)) / total * 100, 1) if total else 0
        types = result.get("by_type", [])
        tinfo = f"；预警类型 {len(types)} 类，最高「{types[0]['type']}」({types[0]['type_count']}条)" if types else ""
        return (f"预警中心（{fl.get('start_date')}~{fl.get('end_date')}，按推送日期，范围：{fl.get('category','全部')}）：共 {total} 条，处置完成率 {comp}%；"
                f"待整改 {wait} / 已整改 {sa.get('已整改',0)} / 已忽略 {sa.get('已忽略',0)} / 已确认 {sa.get('已确认',0)}"
                f"{tinfo}。待整改 TOP {len(result.get('pending_top',[]))} 条。")
    if tool == "dish_cost_rate":
        fl = result.get("filters", {})
        r = result.get("overall_cost_rate")
        head = (f"排菜成本率（{fl.get('start_date')}~{fl.get('end_date')}）：整体成本率 {r}% "
                if r is not None else "排菜成本率：")
        return head + (f"总成本 ¥{result.get('total_cost')} / 标准伙食费 ¥{result.get('total_std')}；"
                       f"超成本 TOP {len(result.get('over_budget_top', []))} 项。")
    if tool == "dish_reputation":
        fl = result.get("filters", {})
        return (f"出品口碑（{fl.get('start_date')}~{fl.get('end_date')}）：覆盖 {result.get('dish_count',0)} 个菜品，"
                f"评价共 {result.get('total_comments',0)} 条，平均评分 {result.get('avg_score')}；"
                f"评价最多 TOP {len(result.get('top_commented', []))}，评分偏低 TOP {len(result.get('low_score_top', []))}。")
    if tool == "dish_nutrition":
        fl = result.get("filters", {})
        return (f"营养 NRV（{fl.get('start_date')}~{fl.get('end_date')}）：分析 {result.get('menu_count',0)} 个菜单；"
                f"能量/蛋白质/脂肪/钠等占比见下表（跨菜单平均仅供参考）。")
    if tool == "inquiry_effect":
        fl = result.get("filters", {})
        qr = result.get("quote_rate")
        head = (f"询比价成效（{fl.get('start_date')}~{fl.get('end_date')}）：报价单 {result.get('total',0)} 笔，"
                f"报价率 {qr}%" if qr is not None else f"询比价成效：报价单 {result.get('total',0)} 笔")
        return head + (f"；已截止 {result.get('closed',0)} 笔，涉及金额 ¥{result.get('sum_amount')}；"
                       f"按询价单分组 {len(result.get('by_inquiry', []))} 组。")
    return "工具执行完成"


def run_agent(client, question: str, llm, prior=None, max_iter: int = 3):
    """非流式聚合版本（保留兼容）。逐步事件请用 run_agent_stream。"""
    events = list(run_agent_stream(client, question, llm, prior=prior, max_iter=max_iter))
    done = [e for e in events if e.get("type") == "done"]
    if done:
        return done[0]
    return {"answer": "", "tool_results": [], "trace": [], "tables": []}


def run_agent_stream(client, question: str, llm, prior=None, max_iter: int = 3, cancel_event=None):
    """流式编排：逐个 yield 执行步骤事件，最后 yield 一个 done 事件。

    事件格式：
      {"type": "step", "stage": str, "detail": str}   # 分析执行过程每一步
      {"type": "done", "answer": str, "tables": [...], "trace": [...], "tool_results": [...]}
      {"type": "error", "message": str}
    """
    history = []
    user_msg = question
    tool_results = []
    trace = [{"stage": "接收问题", "detail": question}]
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    yield {"type": "step", "stage": "接收问题", "detail": question}

    # 多轮对话上下文：把最近几轮答案摘要塞进首轮 prompt，帮助模型理解指代。
    # 注意：不要放入上一轮的问题原文，避免"折线图"等关键词污染当前意图识别。
    context_note = ""
    if prior:
        recent = prior[-4:]
        ctx = "；".join(f"已掌握：{p.get('a','')[:90]}" for p in recent)
        context_note = f"\n【对话上下文，用于理解省略与指代】{ctx}\n"

    # 意图召回增强（P2）：按口径注册表 aliases 召回候选工具，作为参考提示注入
    # system prompt（不进 user 问句，避免污染意图识别）。命中为空时不注入任何内容。
    recall_hint = build_recall_hint(question)
    recall_top = recall_tools_by_alias(question, top_k=3)
    if recall_top:
        _names = "、".join(f"{TOOL_LABELS.get(n, n)}" for n, _, _ in recall_top)
        trace.append({"stage": "候选召回", "detail": f"按业务说法召回候选：{_names}（仅供参考）"})
        yield {"type": "step", "stage": "候选召回",
               "detail": f"按业务说法召回候选：{_names}（仅供参考）"}
    retried_with_hint = False

    for i in range(max_iter):
        # 运行时契约（对应 Omega "可取消"）：用户点停止后置位的 cancel_event 一旦触发，
        # 立即收尾并保留已生成内容（trace / 已读取的工具结果），不继续浪费 LLM/查询。
        if cancel_event and cancel_event.is_set():
            yield {"type": "canceled", "message": "已停止",
                   "trace": trace, "tool_results": tool_results}
            return
        # 多轮上下文只在「基于工具结果生成最终结论」时注入，避免上下文中的
        # "排行/TOP/柱状图"等关键词污染当前问题的意图识别（对 MockLLM 尤其重要）。
        if i > 0 and context_note and tool_results:
            prompt_user = context_note + user_msg
        else:
            prompt_user = user_msg
        # 候选提示只在「尚未取到任何数据」的选工具阶段注入；一旦有工具结果，
        # 后续轮次是基于真实数据写结论，注入候选反而是噪声。
        sys_prompt = build_system_prompt(recall_hint if not tool_results else "")
        reply, usage = llm.chat(sys_prompt, prompt_user, history)
        for k in total_usage:
            total_usage[k] += (usage or {}).get(k, 0)
        call = _extract_tool_call(reply)
        if call and call["name"] in TOOLS:
            # 避免重复调用同一个工具（防呆）
            if any(t["name"] == call["name"] and t["args"] == call["args"] for t in tool_results):
                user_msg = "这个工具已经调用过了，请基于已有真实数据直接用中文回答。"
                continue
            label = TOOL_LABELS.get(call["name"], call["name"])
            trace.append({"stage": "分析意图",
                          "detail": f"识别为「{label}」查询，参数：{json.dumps(call['args'], ensure_ascii=False)}"})
            yield {"type": "step", "stage": "分析意图",
                   "detail": f"识别为「{label}」查询，参数：{json.dumps(call['args'], ensure_ascii=False)}"}
            # 先抛出「调用中」步骤，让用户感知正在执行
            trace.append({"stage": "调用接口", "detail": f"正在调用 {label}（{call['name']}），向后厨管家真实接口逐页拉取数据…"})
            yield {"type": "step", "stage": "调用接口",
                   "detail": f"正在调用 {label}（{call['name']}），向后厨管家真实接口逐页拉取数据…"}
            result, err = call_tool(client, call["name"], call["args"])
            if cancel_event and cancel_event.is_set():
                yield {"type": "canceled", "message": "已停止",
                       "trace": trace, "tool_results": tool_results}
                return
            if err:
                history.append({"Role": "tool",
                                "Content": json.dumps({"error": err}, ensure_ascii=False)})
                trace.append({"stage": "执行结果", "detail": f"调用 {call['name']} 失败：{err}"})
                yield {"type": "step", "stage": "执行结果", "detail": f"调用 {call['name']} 失败：{err}"}
                user_msg = "工具调用出错，请基于已有信息直接回答，或换一个合适的工具。"
                continue
            tool_results.append({"name": call["name"], "args": call["args"], "result": result})
            history.append({"Role": "tool", "Content": json.dumps(result, ensure_ascii=False)})
            summary = _summarize_result(call["name"], result)
            trace.append({"stage": "执行结果", "detail": f"调用 {call['name']} → {summary}"})
            yield {"type": "step", "stage": "执行结果", "detail": f"调用 {call['name']} → {summary}"}
            if result.get("too_large"):
                # 数据量过大：直接以友好提示作为结论，不再让模型编造数字
                answer = result["message"] + "\n\n" + result["suggestion"]
                trace.append({"stage": "生成结论", "detail": "区间数据量过大，返回友好提示而非全量结果"})
                yield {"type": "step", "stage": "生成结论", "detail": "区间数据量过大，返回友好提示而非全量结果"}
                yield {"type": "done", "answer": answer, "tables": [], "charts": [],
                       "trace": trace, "tool_results": tool_results, "usage": total_usage,
                       "chart_warnings": []}
                return
            user_msg = ("请严格基于上面的工具真实返回数据，用简洁中文回答用户的问题，"
                        "并引用关键数字；若数据不足请如实说明。不要编造。")
            continue
        # 没有工具调用 → 视为最终答案。
        # 兜底纠偏（P2）：问句明确命中了已登记的业务说法（高分召回），模型却一个工具都没选，
        # 大概率是意图漏识别，会导致"我无法回答/凭空作答"。此时提示一次候选让它重选，
        # 仅重试一次，且仅在还没取到任何真实数据时进行，避免打断正常闲聊与已成功的链路。
        if (not tool_results and not retried_with_hint and recall_top
                and recall_top[0][1] >= 6.0):
            retried_with_hint = True
            cand = "、".join(f"{n}（{TOOL_LABELS.get(n, n)}）" for n, _, _ in recall_top)
            trace.append({"stage": "意图纠偏", "detail": f"未识别到取数意图，提示候选后重试：{cand}"})
            yield {"type": "step", "stage": "意图纠偏",
                   "detail": f"未识别到取数意图，提示候选后重试：{cand}"}
            user_msg = (f"{question}\n\n（提示：这个问题应当调用数据工具取真实数据后回答，"
                        f"最可能的候选是 {cand}。请只输出 JSON 形式的工具调用；"
                        f"若确实都不适用，再用中文说明原因。）")
            continue
        answer = reply
        break
    else:
        answer = reply  # 达到迭代上限仍未给最终答案

    trace.append({"stage": "生成结论", "detail": "基于上述接口真实返回数据，整理自然语言结论"})
    yield {"type": "step", "stage": "生成结论", "detail": "基于上述接口真实返回数据，整理自然语言结论"}
    # 按模块分节：每节内表格与图表交错（zip），搭配展示，适合领导查看
    # 生成后确定性校验（Harness 思想）：空图降级为提示、单工具异常不拖垮整轮
    sections, chart_warnings = build_sections(tool_results)
    if chart_warnings:
        trace.append({"stage": "图表校验", "detail": "；".join(chart_warnings)})
        yield {"type": "step", "stage": "图表校验", "detail": "；".join(chart_warnings)}
    tables = []
    charts = []
    for s in sections:
        for b in s["blocks"]:
            d = {k: v for k, v in b.items() if k != "type"}
            (tables if b["type"] == "table" else charts).append(d)
    yield {"type": "done", "answer": answer, "tables": tables, "charts": charts,
           "sections": sections, "trace": trace, "tool_results": tool_results,
           "usage": total_usage, "chart_warnings": chart_warnings}


def build_tables(tool_results):
    """把工具结果转成前端可渲染的表格列表（title/columns/rows）。"""
    tables = []
    for t in tool_results:
        r = t["result"]
        if r.get("error") or r.get("too_large"):
            continue
        tool = r.get("tool")
        if tool == "rank_by_dimension":
            dim_label = {"goods": "商品", "goods_category": "商品分类", "warehouse": "仓库", "supplier": "供应商"}[r.get("dimension", "goods")]
            cols = ["排名", dim_label] + (["单位"] if "unit" in (r["items"][0] if r["items"] else {}) else [])
            cols += [{"amount": "估算采购金额(元)", "qty": "数量", "count": "笔数"}[r["metric"]]]
            rows = []
            for i, it in enumerate(r["items"], 1):
                row = [i, it["name"]]
                if "unit" in it:
                    row.append(it.get("unit", ""))
                row.append(it.get(r["metric"]))
                rows.append(row)
            tables.append({"title": f"{dim_label}排行 · {r['metric']}（{r['range']}）",
                           "columns": cols, "rows": rows})
        elif tool == "daily_trend":
            cols = ["日期", {"amount": "估算采购金额(元)", "qty": "数量", "count": "笔数"}[r["metric"]], "笔数"]
            rows = [[p["date"], p.get(r["metric"]), p.get("count")] for p in r["points"]]
            tables.append({"title": f"按日趋势 · {r['metric']}（{r['range']}）",
                           "columns": cols, "rows": rows})
        elif tool == "purchase_inbound_summary":
            rows = [
                ["笔数", r["count"]],
                ["估算采购总金额(元)", r["total_amount_est"]],
                ["合计数量", r["total_qty"]],
            ]
            for u, q in r.get("unit_breakdown", {}).items():
                rows.append([f"数量·{u}", q])
            tables.append({"title": f"采购入库汇总（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
        elif tool == "stock_warning":
            rows = [["已过期", r["outdated_count"]], ["临期预警中", r["warning_count"]]]
            tables.append({"title": "库存预警", "columns": ["状态", "数量"], "rows": rows})
            # 已过期明细：单独成表，增加仓库/规格/批次/来源单号/供应商
            out_items = r.get("outdated_items", []) or []
            if out_items:
                tables.append({
                    "title": "库存已过期明细",
                    "columns": ["商品", "仓库", "规格", "批次", "来源单号", "供应商", "数量", "过期时间"],
                    "rows": [
                        [it.get("goodsName", "—"), it.get("warehouseName", "—"),
                         it.get("spec", "—"), it.get("batchNo", "—"),
                         it.get("sourceBillNumber", "—"), it.get("supplierName", "—"),
                         it.get("qty"), it.get("outdated", "—")[:10]]
                        for it in out_items
                    ],
                })
            # 临期预警明细
            warn_items = r.get("warning_items", []) or []
            if warn_items:
                tables.append({
                    "title": "库存临期预警明细",
                    "columns": ["商品", "仓库", "规格", "批次", "来源单号", "供应商", "数量", "临期时间"],
                    "rows": [
                        [it.get("goodsName", "—"), it.get("warehouseName", "—"),
                         it.get("spec", "—"), it.get("batchNo", "—"),
                         it.get("sourceBillNumber", "—"), it.get("supplierName", "—"),
                         it.get("qty"), it.get("warnDated", "—")[:10]]
                        for it in warn_items
                    ],
                })
        elif tool == "inventory_by_warehouse":
            cols = ["仓库", "商品种类数", "合计数量", "估算金额(元)"]
            rows = [[w["warehouse"], w["goods_count"], w["qty"], w["amount_est"]]
                    for w in r.get("warehouses", [])]
            tables.append({"title": f"库存商品按仓库汇总（{r['filters'].get('warehouse_name') or '全部仓库'}）",
                           "columns": cols, "rows": rows})
        elif tool == "inventory_by_category":
            cols = ["一级分类", "商品种类数", "合计数量", "估算金额(元)", "数量占比(%)"]
            rows = [[c["category"], c["goods_count"], c["qty"], c["amount_est"], c["qty_ratio"]]
                    for c in r.get("categories", [])]
            tables.append({"title": f"库存商品按分类占比（{r['filters'].get('warehouse_name') or '全部仓库'}）",
                           "columns": cols, "rows": rows})
        elif tool == "purchase_inbound_by_warehouse":
            cols = ["仓库", "笔数", "合计数量", "估算采购金额(元)"]
            rows = [[w["warehouse"], w["count"], w["qty"], w["amount_est"]]
                    for w in r.get("warehouses", [])]
            tables.append({"title": f"采购入库按仓库汇总（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": cols, "rows": rows})
        elif tool == "stock_out_by_warehouse":
            cols = ["仓库", "笔数", "合计数量", "估算出库金额(元)"]
            rows = [[w["warehouse"], w["count"], w["qty"], w["amount_est"]]
                    for w in r.get("warehouses", [])]
            tables.append({"title": f"出库按仓库汇总（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": cols, "rows": rows})
            bt = r.get("by_type", {})
            if bt:
                tcols = ["出库类型", "笔数", "合计数量", "估算金额(元)"]
                trows = [[t, v["count"], v["qty"], v["amount_est"]] for t, v in bt.items()]
                tables.append({"title": "出库按类型拆分", "columns": tcols, "rows": trows})
        elif tool == "purchase_stat":
            rows = [
                ["采购总额(含越库·元)", r["purchase_amount_incl_cross"]],
                ["  其中 采购入库(元)", r["in_amount_total"]],
                ["  其中 采购越库(元)", r["cross_amount_total"]],
                ["出库金额(元)", r["out_amount_total"]],
                ["结余金额(采购含越库-出库·元)", r["sub_amount"]],
                ["采购总数量(含越库)", r["purchase_qty_incl_cross"]],
                ["出库数量", r["out_qty_total"]],
                ["结余数量", r["sub_qty"]],
            ]
            tables.append({"title": f"采购统计汇总（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
        elif tool == "purchase_ledger":
            s = r["summary"]
            rows = [
                ["采购总额(元)", s["pur_amount"]],
                ["采购次数", s["pur_count"]],
                ["入库记录总项数", s["stock_in_count"]],
                ["供应商数量", s["supplier_count"]],
            ]
            tables.append({"title": f"采购台账总览（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
            for dim, key in (("按商品采购额 TOP", "by_goods_top"), ("按供应商采购额 TOP", "by_supplier_top"),
                             ("按分类采购额 TOP", "by_category_top")):
                items = r.get(key, [])
                if not items:
                    continue
                cols = ["排名", "名称"] + (["单位"] if "unit" in (items[0] if items else {}) else [])
                cols += ["采购金额(元)", "数量", "笔数"]
                rws = []
                for i, it in enumerate(items, 1):
                    row = [i, it["name"]]
                    if "unit" in it:
                        row.append(it.get("unit", ""))
                    row += [it["amount"], it["qty"], it["count"]]
                    rws.append(row)
                tables.append({"title": f"{dim}（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                               "columns": cols, "rows": rws})
        elif tool == "stock_snapshot":
            sm = r["summary"]
            rows = [
                ["期末库存金额(元)", sm["stock_amount"]],
                ["期末库存数量", sm["stock_qty"]],
                ["期初库存金额(元)", sm["begin_stock_amount"]],
                ["采购入库金额(元)", sm["purchase_in_amount"]],
                ["采购入库数量", sm["purchase_in_qty"]],
                ["领料出库金额(元)", sm["stock_out_amount"]],
                ["领料出库数量", sm["stock_out_qty"]],
                ["采购退货金额(元)", sm["return_out_amount"]],
                ["盘盈金额(元)", sm["inventory_in_amount"]],
                ["盘亏金额(元)", sm["inventory_out_amount"]],
            ]
            tables.append({"title": f"进销存库存快照（{r['filters']['report_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
            cats = r.get("by_category", [])
            if cats:
                tables.append({"title": "库存按分类金额", "columns": ["一级分类", "数量", "库存金额(元)"],
                               "rows": [[c["category"], c["qty"], c["amount"]] for c in cats]})
            whs = r.get("by_warehouse", [])
            if whs:
                tables.append({"title": "库存按仓库金额", "columns": ["仓库", "数量", "库存金额(元)"],
                               "rows": [[w["warehouse"], w["qty"], w["amount"]] for w in whs]})
        elif tool == "supplier_settlement":
            rows = [
                ["供应商数", r["total_suppliers"]],
                ["入库总金额(元)", r["total_purchase_amount"]],
                ["结算总金额(元)", r["total_settle_amount"]],
                ["实退总金额(元)", r["total_return_amount"]],
            ]
            tables.append({"title": f"供应商结算统计（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
            items = r.get("by_supplier_top", [])
            if items:
                rws = [[i, it["name"], it["purchase_amount"], it["settle_amount"], it["return_amount"]]
                       for i, it in enumerate(items, 1)]
                tables.append({"title": f"供应商结算金额 TOP（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                               "columns": ["排名", "供应商", "入库金额(元)", "结算金额(元)", "实退金额(元)"], "rows": rws})
        elif tool == "delivery_fulfillment":
            f = r.get("fulfillment", {})
            tables.append({"title": "配送履约状态", "columns": ["状态", "单数"],
                           "rows": [["待分拣", f.get("notSorting", 0)], ["待发货", f.get("notDelivery", 0)],
                                    ["待验收", f.get("notStockIn", 0)], ["已验收", f.get("stockIned", 0)]]})
            kpi = [
                ["采购金额(元)", r["total_purchase_amount"]],
                ["入库金额(元)", r["total_stock_in_amount"]],
                ["验收差异金额(元)", r["total_diff_amount"]],
                ["验收差异数量", r["total_diff_qty"]],
                ["报废金额(元)", r["total_scrap_amount"]],
            ]
            tables.append({"title": f"配送金额汇总（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": kpi})
            for dim, key, label in (("供应商", "by_supplier_top", "采购金额(元)"),
                                    ("分类", "by_category_top", "采购金额(元)"),
                                    ("仓库", "by_warehouse", "采购金额(元)")):
                items = r.get(key, [])
                if not items:
                    continue
                rws = [[i, it["name"], it["purchase_amount"], it["stock_in_amount"], it["diff_amount"]]
                       for i, it in enumerate(items, 1)]
                tables.append({"title": f"配送按{label} TOP（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                               "columns": ["排名", dim, "采购金额(元)", "入库金额(元)", "差异金额(元)"], "rows": rws})
            acc = r.get("acceptance_status", {})
            if acc:
                tables.append({"title": "验收状态分布", "columns": ["状态", "单数", "采购金额(元)"],
                               "rows": [[k, v["cnt"], v["purchase_amount"]] for k, v in acc.items()]})
        elif tool == "cost_profit":
            row = [["查询口径", r["filters"]["metric"]],
                   ["周期代表日", r["filters"]["date"]],
                   ["时间类型(dateType)", r["filters"]["date_type"]]]
            if r.get("income"):
                row.append(["收入总额(元)", r["income"]["total_amount"]])
                row.append(["收入均值(元)", r["income"]["avg_amount"]])
            if r.get("expense"):
                row.append(["支出总额(元)", r["expense"]["total_amount"]])
                row.append(["支出均值(元)", r["expense"]["avg_amount"]])
            if r.get("profit") is not None:
                row.append(["利润(收入-支出·元)", r["profit"]])
            tables.append({"title": "成本利润汇总", "columns": ["指标", "值"], "rows": row})
            for label, key in (("收入", "income"), ("支出", "expense")):
                sub = r.get(key)
                if not sub:
                    continue
                rk = sub.get("rankings") or []
                if rk:
                    tables.append({"title": f"{label}收支项排行", "columns": ["收支项", "金额(元)", "占比"],
                                   "rows": [[p["item_name"], p["bill_amount"], f"{p['prop']*100:.2f}%"] for p in rk]})
        elif tool == "purchase_return":
            rows = [
                ["退货单数", r["total_bills"]],
                ["应退总金额(元)", r["total_return_amount"]],
                ["实退总金额(元)", r["total_actual_return_amount"]],
                ["实退总数量", r["total_return_qty"]],
            ]
            tables.append({"title": f"退货统计（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
            for dim, key, label in (("供应商", "by_supplier_top", "应退金额(元)"),
                                    ("分类", "by_category_top", "应退金额(元)")):
                items = r.get(key, [])
                if not items:
                    continue
                rws = [[i, it["name"], it["return_amount"], (it.get("actual_return_amount") or it.get("qty") or ""), it.get("bills")]
                       for i, it in enumerate(items, 1)]
                tables.append({"title": f"退货按{label} TOP（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                               "columns": ["排名", dim, "应退金额(元)", ("实退金额(元)" if dim == "供应商" else "数量"), "单数"], "rows": rws})
            bt = r.get("by_return_type", {})
            if bt:
                tables.append({"title": "按退货类型", "columns": ["类型", "单数", "应退金额(元)", "实退金额(元)"],
                               "rows": [[k, v["bills"], v["return_amount"], v["actual_return_amount"]] for k, v in bt.items()]})
            fs = r.get("fin_status", {})
            if fs:
                tables.append({"title": "按财务状态", "columns": ["财务状态", "单数", "应退金额(元)"],
                               "rows": [[k, v["bills"], v["return_amount"]] for k, v in fs.items()]})
        elif tool == "picking_out":
            rows = [
                ["领料单数", r["total_bills"]],
                ["计划出库总金额(元)", r["total_planned_amount"]],
                ["实际出库总金额(元)", r["total_actual_out_amount"]],
                ["计划总数量", r["total_planned_qty"]],
                ["实际出库总数量", r["total_actual_out_qty"]],
                ["已出库/完成单数", r["completed_bills"]],
            ]
            tables.append({"title": f"领料出库统计（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
            whs = r.get("by_warehouse", [])
            if whs:
                tables.append({"title": "领料按仓库", "columns": ["仓库", "单数", "计划金额(元)", "实际出库金额(元)"],
                               "rows": [[w["name"], w["bills"], w["planned_amount"], w["actual_out_amount"]] for w in whs]})
            dests = r.get("by_dest_type", [])
            if dests:
                tables.append({"title": "领料按去向", "columns": ["去向", "单数", "实际出库金额(元)"],
                               "rows": [[d["name"], d["bills"], d["actual_out_amount"]] for d in dests]})
            st = r.get("by_status", {})
            if st:
                tables.append({"title": "领料按状态", "columns": ["状态", "单数", "实际出库金额(元)"],
                               "rows": [[k, v["bills"], v["actual_out_amount"]] for k, v in st.items()]})
        elif tool == "requisition_status":
            rows = [
                ["明细·已采购数量", r["line_has_purchase_qty"]],
                ["明细·待采购数量", r["line_not_purchase_qty"]],
                ["明细·已驳回数量", r["line_rejected_qty"]],
                ["申购单总数", r["total_bills"]],
                ["申购总金额(元)", r["total_apply_amount"]],
            ]
            tables.append({"title": f"申购验收状态（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["指标", "值"], "rows": rows})
            whs = r.get("by_warehouse", [])
            if whs:
                tables.append({"title": "申购按仓库", "columns": ["仓库", "单数", "金额(元)"],
                               "rows": [[w["name"], w["bills"], w["amount"]] for w in whs]})
            sups = r.get("by_supplier_top", [])
            if sups:
                rws = [[i, it["name"], it["bills"], it["amount"]] for i, it in enumerate(sups, 1)]
                tables.append({"title": "申购按供应商 TOP", "columns": ["排名", "供应商", "单数", "金额(元)"], "rows": rws})
        # ---- Phase 2 食安管理域 ----
        elif tool == "health_certificate":
            status = r.get("filters", {}).get("status")
            if status is None:
                d = r.get("distribution", {})
                tables.append({"title": "健康证状态分布", "columns": ["状态", "人数"],
                               "rows": [["正常", d.get("normalQty", 0)], ["即将到期", d.get("aboutToExpireQty", 0)],
                                        ["已过期", d.get("overdueQty", 0)], ["已停用", d.get("disableQty", 0)]]})
            exp = r.get("expiring_soon", [])
            if exp:
                tables.append({"title": "健康证即将到期明细（按到期日）", "columns": ["姓名", "岗位", "健康证号", "到期日", "仓库"],
                               "rows": [[e["full_name"], e["post"], e["hc_no"], e["due_date"], e["warehouse_names"]] for e in exp]})
            expd = r.get("expired", [])
            if expd:
                tables.append({"title": "健康证已过期明细（按到期日）", "columns": ["姓名", "岗位", "健康证号", "到期日", "仓库"],
                               "rows": [[e["full_name"], e["post"], e["hc_no"], e["due_date"], e["warehouse_names"]] for e in expd]})
        elif tool == "food_inspect":
            tables.append({"title": f"食安巡检（{r.get('inspect_type_label','')}）完成与不符合项",
                           "columns": ["指标", "值"],
                           "rows": [["巡检总单数", r.get("total_bills", 0)], ["已完成(已审核)", r.get("audited_qty", 0)],
                                    ["待审核", r.get("initial_qty", 0)], ["完成率(%)", r.get("completion_rate", 0)],
                                    ["不符合项总数", r.get("total_nc_qty", 0)], ["检查项总数", r.get("total_item_qty", 0)],
                                    ["含不符合项单数", r.get("nc_bills", 0)]]})
            whs = r.get("by_warehouse", [])
            if whs:
                tables.append({"title": "巡检按仓库（不符合项领先）", "columns": ["仓库", "已审核", "待审核", "不符合项数", "检查项数"],
                               "rows": [[w["warehouse_name"], w.get("audited", 0), w.get("initial", 0),
                                         w.get("nc_qty", 0), w.get("item_qty", 0)] for w in whs]})
        elif tool == "sample_retention":
            c = r.get("counts", {})
            tables.append({"title": "留样状态计数", "columns": ["状态", "数量"],
                           "rows": [["待存入", c.get("待存入", 0)], ["待取出", c.get("待取出", 0)],
                                    ["留样中", c.get("留样中", 0)], ["已取出", c.get("已取出", 0)],
                                    ["合规留存(留样中+已取出)", r.get("active_retained", 0)]]})
        elif tool == "morning_check":
            tables.append({"title": "晨检合格情况", "columns": ["指标", "值"],
                           "rows": [["合格人数", r.get("qualified_yes", 0)], ["不合格人数", r.get("qualified_no", 0)],
                                    ["在岗人数", r.get("total_qty", 0)], ["已检人数", r.get("checked", 0)],
                                    ["合格率(%)", r.get("qualified_rate", 0)]]})
            sd = r.get("sick_distribution", {})
            if sd:
                tables.append({"title": "晨检不合格原因分布", "columns": ["原因", "人次"],
                               "rows": [[k, v] for k, v in sd.items()]})
            whs = r.get("by_warehouse", [])
            if whs:
                tables.append({"title": "晨检按仓库（不合格领先）", "columns": ["仓库", "合格", "不合格"],
                               "rows": [[w["warehouse_name"], w.get("yes", 0), w.get("no", 0)] for w in whs]})
            bt = r.get("by_type", [])
            if bt:
                tables.append({"title": "晨检按班次", "columns": ["班次", "合格", "不合格"],
                               "rows": [[t["check_type"], t.get("yes", 0), t.get("no", 0)] for t in bt]})
        elif tool == "detection_report":
            tables.append({"title": "检测报告合格情况", "columns": ["指标", "值"],
                           "rows": [["检测总数", r.get("total", 0)], ["合格数", r.get("qualified_yes", 0)],
                                    ["不合格数", r.get("qualified_no", 0)], ["合格率(%)", r.get("qualified_rate", 0)]]})
            sups = r.get("by_supplier_nc_top", [])
            if sups:
                tables.append({"title": "检测不合格按供应商 TOP", "columns": ["供应商", "不合格数"],
                               "rows": [[s["supplier_name"], s["nc_qty"]] for s in sups]})
            goods = r.get("by_goods_nc_top", [])
            if goods:
                tables.append({"title": "检测不合格按商品 TOP", "columns": ["商品", "不合格数"],
                               "rows": [[g["goods_names"], g["nc_qty"]] for g in goods]})
        elif tool == "food_additive":
            tables.append({"title": "食品添加剂概览", "columns": ["指标", "值"],
                           "rows": [["记录总数", r.get("total", 0)], ["超标记录数", r.get("over_standard_cnt", 0)]]})
            items = r.get("by_additive_top", [])
            if items:
                tables.append({"title": "添加剂使用 TOP（按超标次数）", "columns": ["添加剂", "记录数", "平均用量(g/kg)", "平均标准(g/kg)", "超标次数", "面粉用量(kg)"],
                               "rows": [[it["additive_name"], it["cnt"], it["avg_usage_per_kg"], it["avg_standard_per_kg"],
                                         it["over_standard_cnt"], it["total_flour_kg"]] for it in items]})
            whs = r.get("by_warehouse", [])
            if whs:
                tables.append({"title": "添加剂按仓库", "columns": ["仓库", "记录数"],
                               "rows": [[w["warehouse_name"], w["cnt"]] for w in whs]})
        elif tool == "warning_center":
            sa = r.get("status_agg", {})
            tables.append({"title": "预警状态分布", "columns": ["状态", "数量"],
                           "rows": [[k, v] for k, v in sa.items()]})
            cats = r.get("by_category", [])
            if cats:
                tables.append({"title": "预警按分类", "columns": ["分类", "数量"],
                               "rows": [[c["category"], c["count"]] for c in cats]})
            pend = r.get("pending_top", [])
            if pend:
                tables.append({"title": "待整改明细 TOP", "columns": ["分类", "内容", "仓库", "创建时间", "到期日"],
                               "rows": [[p["category"], p["content"], p["warehouse"], p["create_time"], p["end_date"]] for p in pend]})
        elif tool == "device_alarm_index":
            items = r.get("items", [])
            tables.append({"title": "环境设备告警指数", "columns": ["告警类型", "累计次数"],
                           "rows": [[it["type"], it["value"]] for it in items]})
        elif tool == "device_alarm_detail":
            bs = r.get("by_status", [])
            if bs:
                tables.append({"title": "告警状态分布", "columns": ["状态", "数量"],
                               "rows": [[b["status"], b["count"]] for b in bs]})
            bt = r.get("by_type", [])
            if bt:
                tables.append({"title": "告警按类型", "columns": ["类型", "数量"],
                               "rows": [[t["type"], t["count"]] for t in bt]})
            un = r.get("unresolved_top", [])
            if un:
                tables.append({"title": "未处理/已处理明细 TOP", "columns": ["类型", "内容", "数值", "仓库", "告警时间", "状态"],
                               "rows": [[u["type_text"], u["content"], u["value"], u["warehouse"], u["warn_time"], u["status"]] for u in un]})
        elif tool == "period_compare":
            mm = r.get("main_metric", "")
            cols = ["周期", mm, "环比(差值)", "环比(%)"]
            rows = []
            for s in r.get("series", []):
                d = s.get("main_delta")
                dp = s.get("main_delta_pct")
                rows.append([
                    s["period"],
                    s["main_value"],
                    ("" if d is None else f"{'+' if d > 0 else ''}{d:,.2f}"),
                    ("" if dp is None else f"{'+' if dp > 0 else ''}{dp}%"),
                ])
            tables.append({"title": f"周期对比（{r.get('base_tool')}·{mm}）环比表",
                           "columns": cols, "rows": rows})
            # 附：全部指标明细（采购统计时给出入库/越库/出库/结余，成本利润时给出收入/支出/利润）
            keys = set()
            for s in r.get("series", []):
                keys.update(s.get("values", {}).keys())
            if len(keys) > 1:
                mcols = ["周期"] + [k for k in keys]
                mrows = [[s["period"]] + [s.get("values", {}).get(k) for k in keys]
                         for s in r.get("series", [])]
                tables.append({"title": f"周期对比·全指标明细（{r.get('base_tool')}）",
                               "columns": mcols, "rows": mrows})
        elif tool == "dashboard_overview":
            tm = r.get("today_metrics", {})
            def _m(d):
                return [d.get("today", 0), d.get("yesterday", 0), f"{d.get('day_ratio', 0)}%"]
            rows = [
                ["今日采购金额(元)", *_m(tm.get("purchase_amount", {}))],
                ["今日验收金额(元)", *_m(tm.get("stock_in_amount", {}))],
                ["今日留样项数", *_m(tm.get("sample_count", {}))],
                ["今日晨检人数", *_m(tm.get("morning_check", {}))],
            ]
            tables.append({"title": "今日关键指标（含日同比）",
                           "columns": ["指标", "今日值", "昨日值", "日同比(%)"], "rows": rows})
            wait = r.get("wait_processed", {})
            wrows = [
                ["调整单数量", wait.get("adjust_bill_count", 0)],
                ["申购数量", wait.get("apply_count", 0)],
                ["申购金额(元)", wait.get("apply_total_amount", 0)],
                ["二级审核单数量", wait.get("flow_bill_count", 0)],
                ["采购数量", wait.get("pur_count", 0)],
                ["退货数量", wait.get("pur_return_count", 0)],
                ["退货金额(元)", wait.get("pur_return_total_amount", 0)],
                ["采购金额(元)", wait.get("pur_total_amount", 0)],
            ]
            mr = r.get("filters", {}).get("month_range", ["", ""])
            tables.append({"title": f"待处理单据汇总（{mr[0]}~{mr[1]}）",
                           "columns": ["单据类型", "数量/金额"], "rows": wrows})
            ov = r.get("fs_overview", [])
            if ov:
                tables.append({"title": "食安各模块概况", "columns": ["模块", "状态"],
                               "rows": [[o.get("name"), o.get("status")] for o in ov]})
        elif tool == "purchase_price_compare":
            rows = [[i, x["goods"], x["spec"], x["unit"], x["warehouse"], x["supplier"],
                     x["price"], x["high_price"], f"{x['out_of_prop']}%", x["qty"], x["delivery_time"]]
                    for i, x in enumerate(r.get("rows", []), 1)]
            tables.append({"title": f"采购价对比 TOP（超价最严重，{r['filters']['start_date']}~{r['filters']['end_date']}）",
                           "columns": ["排名", "商品", "规格", "单位", "仓库", "供应商", "采购单价", "平台价", "超出比例", "入库数量", "发货时间"],
                           "rows": rows})
        elif tool == "stock_month_report":
            s = r.get("summary", {})
            srows = [
                ["期初金额(元)", s.get("begin_amount", 0)],
                ["采购入库金额(元)", s.get("purchase_in_amount", 0)],
                ["采购越库金额(元)", s.get("purchase_cross_in_amount", 0)],
                ["领料出库金额(元)", s.get("picking_out_amount", 0)],
                ["入库金额合计(元)", s.get("stock_in_amount", 0)],
                ["出库金额合计(元)", s.get("stock_out_amount", 0)],
                ["期末金额(元)", s.get("stock_amount", 0)],
                ["期末数量", s.get("stock_qty", 0)],
            ]
            tables.append({"title": f"库存月报汇总（{r['filters']['report_date']}）",
                           "columns": ["指标", "值"], "rows": srows})
            grows = [[i, x["goods"], x["category"], x["warehouse"], x["unit"],
                      x["stock_in_amount"], x["stock_out_amount"], x["stock_amount"], x["stock_qty"]]
                     for i, x in enumerate(r.get("rows", []), 1)]
            tables.append({"title": f"商品库存明细 TOP（按期末金额，{r['filters']['report_date']}）",
                           "columns": ["排名", "商品", "一级分类", "仓库", "单位", "入库金额(元)", "出库金额(元)", "期末金额(元)", "期末数量"],
                           "rows": grows})
        elif tool == "food_safety_alert":
            sa = r.get("status_agg", {})
            whs = r.get("by_warehouse_state", [])
            if whs:
                tables.append({"title": "预警中心按仓库状态分布（含处置完成率）",
                               "columns": ["仓库", "预警总数", "待整改", "已整改", "已忽略", "已确认", "处置完成率(%)"],
                               "rows": [[w["warehouse"], w["total"], w["wait_rectify"], w["rectified"],
                                         w["ignored"], w["confirmed"], w["completion_rate"]] for w in whs]})
            types = r.get("by_type", [])
            if types:
                tables.append({"title": "预警中心类型汇总", "columns": ["预警类型", "数量"],
                               "rows": [[t["type"], t["type_count"]] for t in types]})
            pend = r.get("pending_top", [])
            if pend:
                tables.append({"title": "待整改明细 TOP", "columns": ["内容", "仓库", "创建时间", "到期日"],
                               "rows": [[p["content"], p["warehouse"], p["create_time"], p["end_date"]] for p in pend]})
        elif tool == "dish_cost_rate":
            fl = r.get("filters", {})
            tables.append({"title": f"排菜成本率汇总（{fl.get('start_date')}~{fl.get('end_date')}）",
                           "columns": ["指标", "数值"],
                           "rows": [["整体成本率(%)", r.get("overall_cost_rate")],
                                    ["总成本(元)", r.get("total_cost")],
                                    ["标准伙食费(元)", r.get("total_std")],
                                    ["餐标合计(元)", r.get("meal_total")],
                                    ["菜品数", r.get("dish_count")]]})
            over = r.get("over_budget_top", [])
            if over:
                tables.append({"title": "超成本 TOP（按成本率降序）",
                               "columns": ["菜品", "分类", "餐次", "成本", "标准伙食费", "成本率(%)"],
                               "rows": [[d["dish"], d["category"], d["meals"], d["cost"], d["std"], d["cost_rate"]] for d in over]})
            daily = r.get("daily", [])
            if daily:
                tables.append({"title": "每日成本率",
                               "columns": ["日期", "成本", "标准伙食费", "成本率(%)"],
                               "rows": [[d["date"], d["cost"], d["std"], d["cost_rate"]] for d in daily]})
        elif tool == "dish_reputation":
            fl = r.get("filters", {})
            tables.append({"title": f"出品口碑汇总（{fl.get('start_date')}~{fl.get('end_date')}）",
                           "columns": ["指标", "数值"],
                           "rows": [["覆盖菜品数", r.get("dish_count")],
                                    ["评价总数", r.get("total_comments")],
                                    ["平均评分", r.get("avg_score")]]})
            tc = r.get("top_commented", [])
            if tc:
                tables.append({"title": "评价数 TOP",
                               "columns": ["菜品", "分类", "评价数", "评分", "餐次"],
                               "rows": [[d["dish"], d["category"], d["comment_count"], d["score"], d["meals"]] for d in tc]})
            ls = r.get("low_score_top", [])
            if ls:
                tables.append({"title": "评分偏低 TOP",
                               "columns": ["菜品", "分类", "评分", "评价数", "餐次"],
                               "rows": [[d["dish"], d["category"], d["score"], d["comment_count"], d["meals"]] for d in ls]})
        elif tool == "dish_nutrition":
            fl = r.get("filters", {})
            nr = r.get("nutrition_rows", [])
            if nr:
                labels = ["能量", "蛋白质", "脂肪", "碳水", "钠", "钙", "铁", "锌"]
                tables.append({"title": f"营养 NRV 占比(%)（{fl.get('start_date')}~{fl.get('end_date')}）",
                               "columns": ["菜单", "日期"] + labels,
                               "rows": [[d["menu"], d["date"]] + [d.get(l) for l in labels] for d in nr]})
            avg = r.get("avg_rate", {})
            if avg:
                tables.append({"title": "跨菜单平均 NRV 占比(%)（示意）",
                               "columns": ["营养素", "平均占比(%)"],
                               "rows": [[l, avg.get(l)] for l in labels]})
        elif tool == "inquiry_effect":
            fl = r.get("filters", {})
            tables.append({"title": f"询比价成效汇总（{fl.get('start_date')}~{fl.get('end_date')}）",
                           "columns": ["指标", "数值"],
                           "rows": [["报价单数", r.get("total")],
                                    ["已报价", r.get("quoted")],
                                    ["待报价", r.get("unquoted")],
                                    ["报价率(%)", r.get("quote_rate")],
                                    ["已截止", r.get("closed")],
                                    ["涉及金额(元)", r.get("sum_amount")],
                                    ["报价品项率(%)", r.get("mat_rate")]]})
            bi = r.get("by_inquiry", [])
            if bi:
                tables.append({"title": "按询价单分组报价率",
                               "columns": ["询价单号", "报价单数", "已报价", "报价率(%)"],
                               "rows": [[x["inquiry_no"], x["total"], x["quoted"], x["quote_rate"]] for x in bi]})
            recs = r.get("records_sample", [])
            if recs:
                tables.append({"title": "报价单明细样本",
                               "columns": ["报价单号", "询价单号", "供应商", "状态", "是否截止", "品项", "已报价品项", "金额", "类型"],
                               "rows": [[x["bill_no"], x["inquiry_no"], x["supplier"], x["status"],
                                         "是" if x["is_close"] else "否", x["mat_count"], x["quote_mat_count"],
                                         x["amount"], x["type"]] for x in recs]})
    return tables


def build_charts(tool_results):
    """把工具结果转成前端 ECharts 可渲染的图表配置列表（title/option）。"""
    charts = []

    def _bar_option(cats, data, yname):
        return {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
            "xAxis": {"type": "category", "data": cats, "axisLabel": {"rotate": 30}},
            "yAxis": {"type": "value", "name": yname},
            "series": [{
                "name": yname,
                "type": "bar",
                "data": data,
                "itemStyle": {"color": "#3b82f6", "borderRadius": [4, 4, 0, 0]},
            }]
        }
    for t in tool_results:
        r = t["result"]
        if r.get("error") or r.get("too_large"):
            continue
        tool = r.get("tool")
        if tool == "daily_trend":
            pts = r.get("points", [])
            if not pts:
                continue
            metric = r.get("metric", "amount")
            metric_label = {"amount": "估算采购金额(元)", "qty": "数量", "count": "笔数"}[metric]
            option = {
                "tooltip": {"trigger": "axis"},
                "grid": {"left": "3%", "right": "4%", "bottom": "3%", "containLabel": True},
                "xAxis": {"type": "category", "boundaryGap": False, "data": [p["date"] for p in pts]},
                "yAxis": {"type": "value", "name": metric_label},
                "series": [{
                    "name": metric_label,
                    "type": "line",
                    "smooth": True,
                    "data": [p.get(metric) for p in pts],
                    "itemStyle": {"color": "#2563eb"},
                    "areaStyle": {"color": {"type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
                                              "colorStops": [{"offset": 0, "color": "rgba(37,99,235,0.3)"},
                                                             {"offset": 1, "color": "rgba(37,99,235,0.05)"}]}},
                }]
            }
            charts.append({"title": f"按日趋势 · {metric_label}折线图（{r['range']}）", "option": option})
        elif tool == "rank_by_dimension":
            items = r.get("items", [])
            if not items:
                continue
            metric = r.get("metric", "amount")
            metric_label = {"amount": "估算采购金额(元)", "qty": "数量", "count": "笔数"}[metric]
            dim_label = {"goods": "商品", "goods_category": "商品分类", "warehouse": "仓库", "supplier": "供应商"}[r.get("dimension", "goods")]
            option = {
                "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                "xAxis": {"type": "category", "data": [it["name"] for it in items], "axisLabel": {"rotate": 30}},
                "yAxis": {"type": "value", "name": metric_label},
                "series": [{
                    "name": metric_label,
                    "type": "bar",
                    "data": [it.get(metric) for it in items],
                    "itemStyle": {"color": "#3b82f6", "borderRadius": [4, 4, 0, 0]},
                }]
            }
            charts.append({"title": f"{dim_label}排行 · {metric_label}柱状图（{r['range']}）", "option": option})
        elif tool == "stock_warning":
            data = [
                {"value": r["outdated_count"], "name": "已过期", "itemStyle": {"color": "#ef4444"}},
                {"value": r["warning_count"], "name": "临期预警中", "itemStyle": {"color": "#f59e0b"}},
            ]
            if sum(d["value"] for d in data) == 0:
                continue
            option = {
                "tooltip": {"trigger": "item"},
                "legend": {"bottom": "0%"},
                "series": [{
                    "name": "库存预警",
                    "type": "pie",
                    "radius": ["40%", "70%"],
                    "avoidLabelOverlap": False,
                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                    "label": {"show": True, "formatter": "{b}: {c}"},
                    "data": data,
                }]
            }
            charts.append({"title": "库存预警 · 饼图", "option": option})
        elif tool == "food_safety_alert":
            sa = r.get("status_agg", {})
            if sa:
                pie_colors = {"待整改": "#ef4444", "已整改": "#22c55e", "已忽略": "#94a3b8", "已确认": "#3b82f6"}
                pie_data = [{"name": k, "value": v, "itemStyle": {"color": pie_colors.get(k, "#64748b")}}
                            for k, v in sa.items() if v > 0]
                if pie_data:
                    option = {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"top": 0, "icon": "circle"},
                        "series": [{
                            "name": "预警中心状态",
                            "type": "pie",
                            "radius": "65%",
                            "center": ["50%", "55%"],
                            "avoidLabelOverlap": True,
                            "minAngle": 5,
                            "itemStyle": {"borderRadius": 6, "borderColor": "#fff", "borderWidth": 2},
                            "label": {"show": True, "formatter": "{b}: {c}"},
                            "data": pie_data,
                        }]
                    }
                    charts.append({"title": "预警中心状态分布 · 饼图", "option": option})
            # 预警类型排行榜（横向柱）
            types = r.get("by_type", [])
            if types:
                t_cats = [t["type"] for t in types]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": 150, "right": "8%", "bottom": "3%", "top": 10, "containLabel": True},
                    "xAxis": {"type": "value", "name": "预警数"},
                    "yAxis": {"type": "category", "data": t_cats, "axisLabel": {"interval": 0, "overflow": "truncate", "width": 135}},
                    "series": [{
                        "name": "预警数", "type": "bar",
                        "data": [t["type_count"] for t in types],
                        "itemStyle": {"color": "#8b5cf6", "borderRadius": [0, 4, 4, 0]},
                    }],
                }
                charts.append({"title": "预警中心类型汇总数量 · 排行榜", "option": option})
        elif tool == "inventory_by_warehouse":
            whs = r.get("warehouses", [])
            if not whs:
                continue
            charts.append({
                "title": "库存商品按仓库汇总（数量）·柱状图",
                "option": _bar_option([w["warehouse"] for w in whs],
                                      [w["qty"] for w in whs], "合计数量"),
            })
        elif tool == "inventory_by_category":
            cats = r.get("categories", [])
            if not cats:
                continue
            # 取占比前 10 的分类做饼图，其余归入「其他」
            top = cats[:10]
            pie_data = [{"name": c["category"], "value": c["qty"]} for c in top]
            if len(cats) > 10:
                other_qty = sum(c["qty"] for c in cats[10:])
                pie_data.append({"name": "其他", "value": round(other_qty, 2)})
            option = {
                "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                "legend": {"type": "scroll", "bottom": "0%"},
                "series": [{
                    "name": "库存分类占比",
                    "type": "pie",
                    "radius": ["35%", "68%"],
                    "avoidLabelOverlap": True,
                    "itemStyle": {"borderRadius": 6, "borderColor": "#fff", "borderWidth": 2},
                    "label": {"show": True, "formatter": "{b}\n{d}%"},
                    "data": pie_data,
                }]
            }
            charts.append({"title": "库存分类占比（按数量）·饼图", "option": option})
        elif tool in ("purchase_inbound_by_warehouse", "stock_out_by_warehouse"):
            whs = r.get("warehouses", [])
            if not whs:
                continue
            if tool == "purchase_inbound_by_warehouse":
                label, amount_label = "采购入库", "估算采购金额(元)"
            else:
                label, amount_label = "出库", "估算出库金额(元)"
            charts.append({
                "title": f"{label}按仓库汇总（{amount_label}）·柱状图",
                "option": _bar_option([w["warehouse"] for w in whs],
                                      [w["amount_est"] for w in whs], amount_label),
            })
        elif tool == "purchase_stat":
            cats = ["采购入库", "采购越库", "出库", "结余"]
            vals = [r["in_amount_total"], r["cross_amount_total"],
                    r["out_amount_total"], r["sub_amount"]]
            charts.append({
                "title": f"采购统计（金额·元）·柱状图（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                "option": _bar_option(cats, vals, "金额(元)"),
            })
        elif tool == "purchase_ledger":
            for dim, key, label in (("商品", "by_goods_top", "商品"), ("供应商", "by_supplier_top", "供应商"),
                                    ("分类", "by_category_top", "分类")):
                items = r.get(key, [])
                if not items:
                    continue
                charts.append({
                    "title": f"按{label}采购额 TOP·柱状图（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                    "option": _bar_option([it["name"] for it in items],
                                          [it["amount"] for it in items], "采购金额(元)"),
                })
        elif tool == "stock_snapshot":
            cats = r.get("by_category", [])
            if cats:
                pie_data = [{"name": c["category"], "value": c["amount"]} for c in cats[:10]]
                if len(cats) > 10:
                    pie_data.append({"name": "其他", "value": round(sum(c["amount"] for c in cats[10:]), 2)})
                option = {
                    "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                    "legend": {"type": "scroll", "bottom": "0%"},
                    "series": [{
                        "name": "库存分类金额",
                        "type": "pie",
                        "radius": ["35%", "68%"],
                        "avoidLabelOverlap": True,
                        "itemStyle": {"borderRadius": 6, "borderColor": "#fff", "borderWidth": 2},
                        "label": {"show": True, "formatter": "{b}\n{d}%"},
                        "data": pie_data,
                    }]
                }
                charts.append({"title": f"库存分类金额占比（{r['filters']['report_date']}）·饼图", "option": option})
        elif tool == "supplier_settlement":
            items = r.get("by_supplier_top", [])
            if items:
                charts.append({
                    "title": f"供应商结算金额 TOP·柱状图（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                    "option": _bar_option([it["name"] for it in items],
                                          [it["settle_amount"] for it in items], "结算金额(元)"),
                })
        elif tool == "delivery_fulfillment":
            f = r.get("fulfillment", {})
            pie_data = [
                {"value": f.get("notSorting", 0), "name": "待分拣", "itemStyle": {"color": "#f59e0b"}},
                {"value": f.get("notDelivery", 0), "name": "待发货", "itemStyle": {"color": "#3b82f6"}},
                {"value": f.get("notStockIn", 0), "name": "待验收", "itemStyle": {"color": "#8b5cf6"}},
                {"value": f.get("stockIned", 0), "name": "已验收", "itemStyle": {"color": "#10b981"}},
            ]
            if sum(d["value"] for d in pie_data) > 0:
                charts.append({
                    "title": "配送履约状态·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "配送履约", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
            sups = r.get("by_supplier_top", [])
            if sups:
                charts.append({
                    "title": f"配送按供应商采购金额 TOP·柱状图（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                    "option": _bar_option([it["name"] for it in sups],
                                          [it["purchase_amount"] for it in sups], "采购金额(元)"),
                })
        elif tool == "cost_profit":
            inc = r.get("income") or {}
            exp = r.get("expense") or {}
            days = sorted(set(list((inc.get("bar") or {}).keys()) + list((exp.get("bar") or {}).keys())))
            if days:
                inc_vals = [(inc.get("bar") or {}).get(d, 0) for d in days]
                exp_vals = [(exp.get("bar") or {}).get(d, 0) for d in days]
                charts.append({
                    "title": f"成本利润（收入 vs 支出）·柱状图（{r['filters']['date']}）",
                    "option": {
                        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                        "legend": {"data": ["收入", "支出"], "bottom": "0%"},
                        "grid": {"left": "3%", "right": "6%", "bottom": "8%", "containLabel": True},
                        "xAxis": {"type": "category", "data": days, "axisLabel": {"rotate": 30}},
                        "yAxis": {"type": "value", "name": "金额(元)"},
                        "series": [
                            {"name": "收入", "type": "bar", "data": inc_vals, "itemStyle": {"color": "#10b981"}},
                            {"name": "支出", "type": "bar", "data": exp_vals, "itemStyle": {"color": "#ef4444"}},
                        ],
                    },
                })
            for label, sub in (("收入", inc), ("支出", exp)):
                pie = sub.get("pie") if sub else None
                if pie:
                    pdata = [{"name": p["item_name"], "value": p["bill_amount"]} for p in pie[:10]]
                    charts.append({
                        "title": f"{label}收支项构成·饼图",
                        "option": {
                            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                            "legend": {"type": "scroll", "bottom": "0%"},
                            "series": [{"name": label, "type": "pie", "radius": ["35%", "68%"],
                                        "itemStyle": {"borderRadius": 6, "borderColor": "#fff", "borderWidth": 2},
                                        "label": {"show": True, "formatter": "{b}\n{d}%"}, "data": pdata}],
                        },
                    })
        elif tool == "purchase_return":
            sups = r.get("by_supplier_top", [])
            if sups:
                charts.append({
                    "title": f"退货按供应商应退金额 TOP·柱状图（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                    "option": _bar_option([it["name"] for it in sups],
                                          [it["return_amount"] for it in sups], "应退金额(元)"),
                })
        elif tool == "picking_out":
            whs = r.get("by_warehouse", [])
            if whs:
                charts.append({
                    "title": f"领料按仓库实际出库金额·柱状图（{r['filters']['start_date']}~{r['filters']['end_date']}）",
                    "option": _bar_option([w["name"] for w in whs],
                                          [w["actual_out_amount"] for w in whs], "实际出库金额(元)"),
                })
        elif tool == "requisition_status":
            pie_data = [
                {"value": r["line_has_purchase_qty"], "name": "已采购", "itemStyle": {"color": "#10b981"}},
                {"value": r["line_not_purchase_qty"], "name": "待采购", "itemStyle": {"color": "#f59e0b"}},
                {"value": r["line_rejected_qty"], "name": "已驳回", "itemStyle": {"color": "#ef4444"}},
            ]
            if sum(d["value"] for d in pie_data) > 0:
                charts.append({
                    "title": "申购明细状态·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "申购明细状态", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        # ---- Phase 2 食安管理域 ----
        elif tool == "health_certificate":
            if r.get("filters", {}).get("status") is not None:
                continue
            d = r.get("distribution", {})
            pie_data = [
                {"value": d.get("normalQty", 0), "name": "正常", "itemStyle": {"color": "#10b981"}},
                {"value": d.get("aboutToExpireQty", 0), "name": "即将到期", "itemStyle": {"color": "#f59e0b"}},
                {"value": d.get("overdueQty", 0), "name": "已过期", "itemStyle": {"color": "#ef4444"}},
                {"value": d.get("disableQty", 0), "name": "已停用", "itemStyle": {"color": "#6b7280"}},
            ]
            if sum(x["value"] for x in pie_data) > 0:
                charts.append({
                    "title": "健康证状态分布·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "健康证状态", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        elif tool == "food_inspect":
            pie_data = [
                {"value": r.get("audited_qty", 0), "name": "已审核", "itemStyle": {"color": "#10b981"}},
                {"value": r.get("initial_qty", 0), "name": "待审核", "itemStyle": {"color": "#f59e0b"}},
            ]
            if sum(x["value"] for x in pie_data) > 0:
                charts.append({
                    "title": f"食安巡检（{r.get('inspect_type_label','')}）完成状态·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "巡检完成", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        elif tool == "morning_check":
            pie_data = [
                {"value": r.get("qualified_yes", 0), "name": "合格", "itemStyle": {"color": "#10b981"}},
                {"value": r.get("qualified_no", 0), "name": "不合格", "itemStyle": {"color": "#ef4444"}},
            ]
            if sum(x["value"] for x in pie_data) > 0:
                charts.append({
                    "title": "晨检合格情况·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "晨检", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        elif tool == "detection_report":
            pie_data = [
                {"value": r.get("qualified_yes", 0), "name": "合格", "itemStyle": {"color": "#10b981"}},
                {"value": r.get("qualified_no", 0), "name": "不合格", "itemStyle": {"color": "#ef4444"}},
            ]
            if sum(x["value"] for x in pie_data) > 0:
                charts.append({
                    "title": "检测报告合格情况·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "检测", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        elif tool == "food_additive":
            items = r.get("by_additive_top", [])
            if items:
                charts.append({
                    "title": "添加剂使用记录数 TOP·柱状图",
                    "option": _bar_option([it["additive_name"] for it in items],
                                          [it["cnt"] for it in items], "记录数"),
                })
        elif tool == "warning_center":
            sa = r.get("status_agg", {})
            pie_data = [{"name": k, "value": v} for k, v in sa.items() if v > 0]
            if pie_data:
                charts.append({
                    "title": "综合预警状态·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "预警状态", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        elif tool == "device_alarm_index":
            items = r.get("items", [])
            charts.append({
                "title": "环境设备告警指数·柱状图",
                "option": _bar_option([it["type"] for it in items], [it["value"] for it in items], "累计次数"),
            })
        elif tool == "device_alarm_detail":
            bs = r.get("by_status", [])
            pie_data = [{"name": b["status"], "value": b["count"]} for b in bs if b["count"] > 0]
            if pie_data:
                charts.append({
                    "title": "设备告警状态·饼图",
                    "option": {
                        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                        "legend": {"bottom": "0%"},
                        "series": [{"name": "告警状态", "type": "pie", "radius": ["40%", "70%"],
                                    "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                    "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                    },
                })
        elif tool == "period_compare":
            series = r.get("series", [])
            if not series:
                continue
            mm = r.get("main_metric", "")
            cats = [s["period"] for s in series]
            main_vals = [s.get("main_value") if s.get("main_value") is not None else 0 for s in series]
            # 主指标走势折线图（趋势可视化）
            charts.append({
                "title": f"周期对比·{mm}走势折线图",
                "option": {
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": "3%", "right": "4%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "boundaryGap": False, "data": cats, "axisLabel": {"rotate": 30}},
                    "yAxis": {"type": "value", "name": mm},
                    "series": [{
                        "name": mm,
                        "type": "line",
                        "smooth": True,
                        "data": main_vals,
                        "itemStyle": {"color": "#2563eb"},
                        "areaStyle": {"color": {"type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
                                                  "colorStops": [{"offset": 0, "color": "rgba(37,99,235,0.3)"},
                                                                 {"offset": 1, "color": "rgba(37,99,235,0.05)"}]}},
                        "label": {"show": True, "formatter": "{c}"},
                    }],
                },
            })
            # 多指标对比柱状图（采购统计时：采购总额/入库/越库/出库/结余；成本利润时：收入/支出/利润）
            metric_keys = []
            for s in series:
                for k in s.get("values", {}).keys():
                    if k not in metric_keys:
                        metric_keys.append(k)
            # 只取与主指标不同的其余指标做并列柱（避免与主折线重复过多）
            extra = [k for k in metric_keys if k != mm][:5]
            if extra:
                charts.append({
                    "title": f"周期对比·多指标柱状图（{r.get('base_tool')}）",
                    "option": {
                        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                        "legend": {"data": extra, "bottom": "0%"},
                        "grid": {"left": "3%", "right": "6%", "bottom": "8%", "containLabel": True},
                        "xAxis": {"type": "category", "data": cats, "axisLabel": {"rotate": 30}},
                        "yAxis": {"type": "value", "name": "金额(元)"},
                        "series": [{
                            "name": k,
                            "type": "bar",
                            "data": [s.get("values", {}).get(k) or 0 for s in series],
                        } for k in extra],
                    },
                })
        elif tool == "dish_cost_rate":
            ob = r.get("over_budget_top", [])
            if ob:
                top = sorted(ob, key=lambda x: (x.get("cost_rate") or 0), reverse=True)[:15]
                cats = [x["dish"] for x in top]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": cats, "axisLabel": {"rotate": 30, "interval": 0}},
                    "yAxis": {"type": "value", "name": "成本率(%)"},
                    "series": [{
                        "name": "成本率(%)", "type": "bar",
                        "data": [{"value": x.get("cost_rate") or 0,
                                  "itemStyle": {"color": "#ef4444" if (x.get("cost_rate") or 0) > 100 else "#f59e0b"}}
                                 for x in top],
                        "barMaxWidth": 26, "label": {"show": True, "formatter": "{c}%", "position": "top"},
                    }],
                }
                charts.append({"title": "排菜成本率·超标准TOP（按成本率降序）", "option": option})
            daily = r.get("daily", [])
            if daily:
                option = {
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": "3%", "right": "4%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "boundaryGap": False, "data": [d["date"] for d in daily], "axisLabel": {"rotate": 30}},
                    "yAxis": {"type": "value", "name": "成本率(%)"},
                    "series": [{"name": "每日成本率(%)", "type": "line", "smooth": True,
                                "data": [d.get("cost_rate") for d in daily],
                                "itemStyle": {"color": "#2563eb"},
                                "areaStyle": {"color": {"type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
                                        "colorStops": [{"offset": 0, "color": "rgba(37,99,235,0.3)"},
                                                       {"offset": 1, "color": "rgba(37,99,235,0.05)"}]}},
                                "label": {"show": True, "formatter": "{c}%"}}],
                }
                charts.append({"title": "排菜成本率·每日走势折线图", "option": option})
        elif tool == "dish_reputation":
            tc = r.get("top_commented", [])
            if tc:
                top = tc[:15]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "legend": {"data": ["评价数", "评分"], "bottom": "0%"},
                    "grid": {"left": "3%", "right": "6%", "bottom": "8%", "containLabel": True},
                    "xAxis": {"type": "category", "data": [x["dish"] for x in top], "axisLabel": {"rotate": 30, "interval": 0}},
                    "yAxis": [{"type": "value", "name": "评价数"},
                              {"type": "value", "name": "评分", "position": "right", "splitLine": {"show": False}}],
                    "series": [
                        {"name": "评价数", "type": "bar", "data": [x["comment_count"] for x in top],
                         "itemStyle": {"color": "#3b82f6"}, "barMaxWidth": 22},
                        {"name": "评分", "type": "line", "yAxisIndex": 1, "data": [x["score"] for x in top],
                         "itemStyle": {"color": "#f59e0b"}, "symbol": "circle", "symbolSize": 7},
                    ],
                }
                charts.append({"title": "出品口碑·评价数TOP（双轴：评价数/评分）", "option": option})
            ls = r.get("low_score_top", [])
            if ls:
                top = ls[:15]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": [x["dish"] for x in top], "axisLabel": {"rotate": 30, "interval": 0}},
                    "yAxis": {"type": "value", "name": "评分"},
                    "series": [{"name": "评分", "type": "bar",
                                "data": [{"value": x["score"], "itemStyle": {"color": "#ef4444"}} for x in top],
                                "barMaxWidth": 24, "label": {"show": True, "formatter": "{c}", "position": "top"}}],
                }
                charts.append({"title": "出品口碑·评分偏低TOP（按评分升序）", "option": option})
        elif tool == "dish_nutrition":
            avg = r.get("avg_rate", {})
            if avg:
                labels = [k for k, v in avg.items() if v is not None]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": labels, "axisLabel": {"rotate": 30}},
                    "yAxis": {"type": "value", "name": "平均NRV占比(%)", "max": 100},
                    "series": [{"name": "平均NRV占比(%)", "type": "bar", "data": [avg[k] for k in labels],
                                "itemStyle": {"color": "#10b981"}, "barMaxWidth": 30,
                                "label": {"show": True, "formatter": "{c}%", "position": "top"}}],
                }
                charts.append({"title": "营养NRV·跨菜单平均占比柱状图", "option": option})
            nr = r.get("nutrition_rows", [])
            if nr:
                menus = [x["menu"] for x in nr[:8]]
                nutrients = ["能量", "蛋白质", "脂肪", "碳水", "钠", "钙", "铁", "锌"]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "legend": {"data": nutrients, "bottom": "0%"},
                    "grid": {"left": "3%", "right": "6%", "bottom": "10%", "containLabel": True},
                    "xAxis": {"type": "category", "data": menus, "axisLabel": {"rotate": 20}},
                    "yAxis": {"type": "value", "name": "NRV占比(%)", "max": 100},
                    "series": [{"name": n, "type": "bar", "data": [x.get(n) or 0 for x in nr[:8]]} for n in nutrients],
                }
                charts.append({"title": "营养NRV·各菜单营养素对比（TOP8）", "option": option})
        elif tool == "inquiry_effect":
            bi = r.get("by_inquiry", [])
            if bi:
                items = sorted(bi, key=lambda x: (x.get("quote_rate") or 0))[:20]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": [x["inquiry_no"] for x in items], "axisLabel": {"rotate": 30, "interval": 0}},
                    "yAxis": {"type": "value", "name": "报价率(%)", "max": 100},
                    "series": [{"name": "报价率(%)", "type": "bar",
                                "data": [{"value": x.get("quote_rate") or 0,
                                          "itemStyle": {"color": "#22c55e" if (x.get("quote_rate") or 0) >= 80
                                                        else ("#f59e0b" if (x.get("quote_rate") or 0) >= 50 else "#ef4444")}}
                                         for x in items],
                                "barMaxWidth": 24, "label": {"show": True, "formatter": "{c}%", "position": "top"}}],
                }
                charts.append({"title": "询比价成效·按询价单报价率（%）", "option": option})
        elif tool == "stock_month_report":
            s = r.get("summary", {})
            flow = [("期初金额", s.get("begin_amount", 0)), ("入库金额", s.get("stock_in_amount", 0)),
                    ("出库金额", s.get("stock_out_amount", 0)), ("期末金额", s.get("stock_amount", 0))]
            if any(v for _, v in flow):
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": [k for k, _ in flow]},
                    "yAxis": {"type": "value", "name": "金额(元)"},
                    "series": [{"name": "金额(元)", "type": "bar",
                                "data": [{"value": v, "itemStyle": {"color": c}}
                                         for (_, v), c in zip(flow, ["#94a3b8", "#3b82f6", "#f59e0b", "#22c55e"])],
                                "barMaxWidth": 40, "label": {"show": True, "formatter": "{c}", "position": "top"}}],
                }
                charts.append({"title": "库存月报·进销存金额概览（元）", "option": option})
            rows = r.get("rows", [])
            if rows:
                top = rows[:15]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": [x["goods"] for x in top], "axisLabel": {"rotate": 30, "interval": 0}},
                    "yAxis": {"type": "value", "name": "期末金额(元)"},
                    "series": [{"name": "期末金额(元)", "type": "bar", "data": [x["stock_amount"] for x in top],
                                "itemStyle": {"color": "#3b82f6"}, "barMaxWidth": 26,
                                "label": {"show": True, "formatter": "{c}", "position": "top"}}],
                }
                charts.append({"title": "库存月报·期末金额TOP商品", "option": option})
        elif tool == "purchase_price_compare":
            rows = r.get("rows", [])
            if rows:
                top = sorted(rows, key=lambda x: (x.get("out_of_prop") or 0), reverse=True)[:15]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": [x["goods"] for x in top], "axisLabel": {"rotate": 30, "interval": 0}},
                    "yAxis": {"type": "value", "name": "超出比例(%)"},
                    "series": [{"name": "超出比例(%)", "type": "bar",
                                "data": [{"value": x.get("out_of_prop") or 0, "itemStyle": {"color": "#ef4444"}} for x in top],
                                "barMaxWidth": 24, "label": {"show": True, "formatter": "{c}%", "position": "top"}}],
                }
                charts.append({"title": "采购价对比·超价比例TOP（%）", "option": option})
        elif tool == "sample_retention":
            c = r.get("counts", {})
            pie_data = [
                {"value": c.get("待存入", 0), "name": "待存入", "itemStyle": {"color": "#94a3b8"}},
                {"value": c.get("待取出", 0), "name": "待取出", "itemStyle": {"color": "#f59e0b"}},
                {"value": c.get("留样中", 0), "name": "留样中", "itemStyle": {"color": "#3b82f6"}},
                {"value": c.get("已取出", 0), "name": "已取出", "itemStyle": {"color": "#22c55e"}},
            ]
            if sum(x["value"] for x in pie_data) > 0:
                charts.append({"title": "留样管理·状态分布环形图", "option": {
                    "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                    "legend": {"bottom": "0%"},
                    "series": [{"name": "留样状态", "type": "pie", "radius": ["40%", "70%"],
                                "itemStyle": {"borderRadius": 8, "borderColor": "#fff", "borderWidth": 2},
                                "label": {"show": True, "formatter": "{b}: {c}"}, "data": pie_data}],
                }})
        elif tool == "dashboard_overview":
            tm = r.get("today_metrics", {})
            items = [("采购金额", tm.get("purchase_amount", {}).get("day_ratio")),
                     ("验收金额", tm.get("stock_in_amount", {}).get("day_ratio")),
                     ("留样项数", tm.get("sample_count", {}).get("day_ratio")),
                     ("晨检人数", tm.get("morning_check", {}).get("day_ratio"))]
            if any(v is not None for _, v in items):
                cats = [k for k, v in items]
                vals = [v if v is not None else 0 for _, v in items]
                option = {
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "grid": {"left": "3%", "right": "8%", "bottom": "3%", "containLabel": True},
                    "xAxis": {"type": "category", "data": cats},
                    "yAxis": {"type": "value", "name": "日同比(%)"},
                    "series": [{"name": "日同比(%)", "type": "bar",
                                "data": [{"value": v, "itemStyle": {"color": "#22c55e" if v >= 0 else "#ef4444"}} for v in vals],
                                "barMaxWidth": 40, "label": {"show": True, "formatter": "{c}%", "position": "top"}}],
                }
                charts.append({"title": "经营驾驶舱·今日指标日同比(%)", "option": option})
    return charts


def _chart_has_data(option):
    """Harness 校验：图表 option 是否真的含有可绘制数据。

    对应 Omega 的"图表读取字段能否与返回对齐"校验——防止生成空白图
    （如 series.data 全为 None）。值 0 视为有数据，只有全 None/空才判空。
    """
    if not option or not isinstance(option, dict):
        return False
    series = option.get("series") or []
    if not series:
        return False
    for s in series:
        d = s.get("data") if isinstance(s, dict) else None
        if isinstance(d, list):
            if any(x is not None for x in d):
                return True
        elif d is not None:
            return True
    return False


def build_sections(tool_results):
    """把工具结果按模块（调用）分节，每节内表格与图表交错（zip）搭配展示。

    返回 (sections, warnings)：
      - sections：见下结构；空图会降级为 note 提示，绝不输出空白图。
      - warnings：图表层校验告警列表（如某图因数据为空未生成、某工具结构异常降级）。
    这是生成后的确定性校验层（Harness 思想），不依赖模型重写。
        [{"module": <模块中文名>, "summary": <单行结论>, "blocks": [
            {"type": "table", "title":..., "columns":..., "rows":...},
            {"type": "chart", "title":..., "option":...},
            {"type": "note", "text":...},
            ...
        ]}, ...]
    blocks 的顺序为该模块「表→图→表→图」交替，使明细表与其可视化紧邻，
    便于领导阅读。前端优先用 sections 渲染；无 sections 时回退 tables/charts。
    """
    sections = []
    warnings = []
    for t in tool_results:
        name = t["name"]
        r = t["result"]
        label = TOOL_LABELS.get(name, name)
        summary = _summarize_result(name, r)
        if r.get("error") or r.get("too_large"):
            sections.append({"module": label, "summary": summary, "blocks": []})
            continue
        # 防御：单工具结果结构异常（如字段缺失 KeyError）不应拖垮整轮，
        # 降级为告警并跳过该模块的图表（对应 Omega "规则能修的不让模型重写"）。
        try:
            tb = build_tables([t])
            ch = build_charts([t])
        except Exception as e:
            warnings.append(f"{label}：结果结构异常，图表已降级（{e}）")
            sections.append({"module": label, "summary": summary, "blocks": []})
            continue
        if not tb and not ch:
            continue
        blocks = []
        for i in range(max(len(tb), len(ch))):
            if i < len(tb):
                blocks.append({"type": "table", **tb[i]})
            if i < len(ch):
                blocks.append({"type": "chart", **ch[i]})
        # 图表层校验：空图降级为 note，避免静默空白图（字段绑定/数据存在性校验）
        clean = []
        for b in blocks:
            if b["type"] == "chart" and not _chart_has_data(b.get("option")):
                title = b.get("title", "图表")
                warnings.append(f"{title}：数据为空，未生成可视化（避免空白图）")
                clean.append({"type": "note", "text": f"📊 {title}：当前查询无数据，未生成图表"})
            else:
                clean.append(b)
        blocks = clean
        if not blocks:
            continue
        sections.append({"module": label, "summary": summary, "blocks": blocks})
    return sections, warnings
