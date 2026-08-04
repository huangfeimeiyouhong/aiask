# -*- coding: utf-8 -*-
"""实测 data/earlyWarn/pageAndStat 的日期参数语义。

目标：确认 startDate/endDate 与 beginDate/endDate 到底哪个是"推送日期(createTime)"窗口，
避免凭注释/记忆猜测（当前 warning_center 与 food_safety_alert 的注册表描述相互矛盾）。
"""
import json
from collections import Counter

import config
from hcg_client import HCGClient

U, P = "at0001", "at123456@"


def brief(tag, r, win=None):
    if not isinstance(r, dict) or not r.get("success"):
        print(f"[{tag}] 请求失败: {str(r)[:160]}")
        return
    d = r.get("data") or {}
    recs = d.get("records") or []
    total = d.get("total")
    ct = [(x.get("createTime") or "")[:10] for x in recs if x.get("createTime")]
    sd = [(x.get("startDate") or "")[:10] for x in recs if x.get("startDate")]
    ed = [(x.get("endDate") or "")[:10] for x in recs if x.get("endDate")]
    def rng(v):
        return f"{min(v)}~{max(v)}" if v else "—"
    line = f"[{tag}] total={total} 本页={len(recs)} createTime范围={rng(ct)} 记录startDate范围={rng(sd)} 记录endDate范围={rng(ed)}"
    if win and ct:
        a, b = win
        inside = sum(1 for x in ct if a <= x <= b)
        line += f"  → createTime落在窗口[{a}~{b}]内: {inside}/{len(ct)}"
    if win and sd:
        a, b = win
        inside2 = sum(1 for x in sd if a <= x <= b)
        line += f" | 记录startDate落窗内: {inside2}/{len(sd)}"
    print(line)
    if recs:
        s = recs[0]
        print("      样本:", json.dumps({k: s.get(k) for k in
              ("category", "type", "status", "createTime", "startDate", "endDate")},
              ensure_ascii=False))


def main():
    c = HCGClient(base_url=config.SETTINGS["HCG_BASE_URL"])
    r = c.login(U, P)
    print("登录:", r.get("success"), "token长度:", len(c.token or ""))
    if not c.token:
        print(str(r)[:300]); return 1

    base = {"pageNo": 1, "pageSize": 50}

    # 0) 不带任何日期 → 基线（全量）
    brief("A 无日期(基线)", c.page_early_warn_stat(dict(base)))

    # 挑一个窄窗口：最近一周 / 某历史月，观察过滤是否生效
    wins = [("2026-08-01", "2026-08-04"), ("2026-06-01", "2026-06-30")]
    for a, b in wins:
        brief(f"B startDate/endDate {a}~{b}",
              c.page_early_warn_stat(dict(base, startDate=a, endDate=b)), (a, b))
        brief(f"C beginDate/endDate {a}~{b}",
              c.page_early_warn_stat(dict(base, beginDate=a, endDate=b)), (a, b))

    # getStatItem 同样对照
    for a, b in wins[:1]:
        for k in ("startDate", "beginDate"):
            s = c.get_early_warn_stat_item({k: a, "endDate": b})
            print(f"[D getStatItem {k}={a}~{b}]",
                  json.dumps(s.get("data") if s.get("success") else s, ensure_ascii=False)[:200])
        s0 = c.get_early_warn_stat_item({})
        print("[D getStatItem 无日期]",
              json.dumps(s0.get("data") if s0.get("success") else s0, ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
