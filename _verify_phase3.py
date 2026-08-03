"""Phase 3 验证：综合预警 + 环境设备告警（真实账号 at0001 + 真实 hy3 路由）。"""
import json, sys
from datetime import date, timedelta
from hcg_client import HCGClient
from semantic_tools import TOOLS, call_tool, _default_month_range
from semantic_layer import build_system_prompt, SYSTEM_PROMPT
from agent import run_agent_stream
from hunyuan import get_llm

SD, ED = "2026-07-01", "2026-07-31"

def section(t):
    print("\n" + "=" * 70 + "\n" + t + "\n" + "=" * 70)

def main():
    c = HCGClient()
    ok = c.login("at0001", "at123456@")
    if not ok.get("success"):
        print("LOGIN_FAIL", ok); sys.exit(1)
    print("LOGIN_OK")

    section("A. 工具直调（真实接口）")
    a_results = {}
    direct = {
        "warning_center": {"start_date": SD, "end_date": ED},
        "device_alarm_index": {},
        "device_alarm_detail": {"start_date": SD, "end_date": ED},
    }
    for name, kw in direct.items():
        try:
            res, err = call_tool(c, name, dict(kw))
            if err:
                a_results[name] = f"ERROR: {err}"
                print(f"  [FAIL] {name}: {err}")
                continue
            err = res.get("error")
            if err:
                a_results[name] = f"ERROR: {err}"
                print(f"  [FAIL] {name}: {err}")
                continue
            # 摘要关键字段
            if name == "warning_center":
                sa = res.get("status_agg", {})
                a_results[name] = (f"total={res.get('total')} 待整改={sa.get('待整改')} "
                                    f"已整改={sa.get('已整改')} 已忽略={sa.get('已忽略')} "
                                    f"已确认={sa.get('已确认')} cat={len(res.get('by_category',[]))} "
                                    f"pending={len(res.get('pending_top',[]))}")
            elif name == "device_alarm_index":
                a_results[name] = (f"total_alarms={res.get('total_alarms')} "
                                    f"items={[(i['type'],i['value']) for i in res.get('items',[])]}")
            else:
                bs = res.get("by_status", [])
                a_results[name] = (f"total={res.get('total')} status={bs} "
                                    f"type={len(res.get('by_type',[]))} "
                                    f"unresolved={len(res.get('unresolved_top',[]))}")
            print(f"  [OK] {name}: {a_results[name]}")
        except Exception as e:
            a_results[name] = f"EXC: {e}"
            print(f"  [EXC] {name}: {e}")

    section("B. 真实 hy3 路由（6 个易混问题）")
    llm = get_llm()
    print("LLM class:", type(llm).__name__)
    questions = [
        ("综合预警看板，现在有哪些待整改？", "warning_center"),
        ("证照快到期和库存过期预警分别有多少？", "warning_center"),
        ("厨房环境告警指数，温度燃气烟雾告警多少？", "device_alarm_index"),
        ("这个月有哪些设备告警没处理？", "device_alarm_detail"),
        ("7月巡检不符合项预警", "warning_center"),  # 巡检不符合→综合预警 category=fs
        ("消杀环境设备告警记录", "device_alarm_detail"),
    ]
    routed = {}
    for q, expect in questions:
        sp = build_system_prompt()
        try:
            raw = llm.chat(system=sp, user=q)
            plan = json.loads(raw)
            tool = plan.get("tool")
            good = (tool == expect)
            routed[q] = (tool, good)
            print(f"  [{'OK' if good else 'CHECK'}] {q}  →  {tool}  (期望 {expect})")
        except Exception as e:
            routed[q] = (f"ERR:{e}", False)
            print(f"  [ERR] {q}: {e}")

    section("C. 端到端 run_agent_stream（2 个问题）")
    for q in [questions[0][0], questions[2][0]]:
        print(f"\n--- Q: {q} ---")
        try:
            for ev in run_agent_stream(c, q, llm):
                t = ev.get("type")
                if t == "step":
                    print("  [step]", ev.get("stage"), "::", str(ev.get("detail", ""))[:60])
                elif t == "done":
                    print("  [done] answer:", ev.get("answer", "")[:160])
                    print("  [done] tables:", len(ev.get("tables", [])), "charts:", len(ev.get("charts", [])))
                elif t == "error":
                    print("  [ERROR]", ev.get("message"))
        except Exception as e:
            print("  [STREAM_EXC]", e)

    # 汇总
    section("SUMMARY")
    routed_ok = sum(1 for _, g in routed.values() if g)
    print(f"A 直调: {sum(1 for v in a_results.values() if not v.startswith(('ERROR','EXC')))}/{len(a_results)} 成功")
    print(f"B 路由: {routed_ok}/{len(routed)} 命中期望工具")
    print("PHASE3_VERIFY_DONE")

if __name__ == "__main__":
    main()
