#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语义工具层 —— 把底层分页接口包装成「业务语义级」聚合工具。

设计原则：
1. LLM 只调用这些高层工具，不在对话里翻几十页；翻页/聚合在 Python 侧完成。
2. 金额统一为「估算金额 = 单价(price) × 数量(qty)」（接口无金额字段）。
3. 口径字典：越库也是一种采购，统计「采购数据」默认同时计入 purchaseIn(采购入库)
   与 purchaseCrossIn(采购越库)，即 stockInTypeList=["purchaseIn","purchaseCrossIn"]。
   仅当用户明确说"只要进库的/不含越库/仅入库"时，才只取 purchaseIn。
4. 所有结果 100% 来自接口真实返回，无任何编造。
"""

import config
from metrics_registry import (
    PURCHASE_INBOUND_TYPES, ONLY_INBOUND_TYPES,
    INVENTORY_ZERO_QTY, INVENTORY_VALID_QTY_MIN,
    METRICS, normalize_out_type,
)
import sys
from hcg_client import ResponseTooLarge
from collections import defaultdict
from datetime import datetime, date, timedelta

PAGE_SIZE = 100
MAX_PAGES = 400  # 安全阀
INV_PAGE_SIZE = 2000  # 库存快照翻页用较大页（减少请求数，全量约 68293 条/34 页）

# 重查询保护：库存月报/入库记录/出库记录必须指定具体仓库，且查询区间最长 1 个月，
# 避免对后厨管家接口发起跨全部仓库的全量拉取把数据库打爆。
MAX_QUERY_SPAN_DAYS = 31  # 最长查询区间 = 1 个月


def _require_warehouse(warehouse_name, tool):
    """强制具体仓库：未指定则返回友好错误 dict（不查库），由 LLM 追问用户。"""
    if not warehouse_name or not str(warehouse_name).strip():
        return {
            "tool": tool,
            "error": "必须指定具体仓库（warehouse_name）才能查询，不允许跨全部仓库全量拉取（保护后端数据库）。",
            "hint": "请向用户确认要查哪个仓库（例如\"一食堂仓库\"\"中心仓\"），拿到具体仓库名后再重新调用本工具。",
        }
    return None


def _check_max_span(start_date, end_date, tool):
    """查询区间最长 1 个月（31 天），超出返回友好错误 dict。"""
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except Exception:
        return None  # 日期解析失败交给下游既有校验
    if (ed - sd).days > MAX_QUERY_SPAN_DAYS:
        return {
            "tool": tool,
            "error": f"查询区间最长只能为 1 个月（≤{MAX_QUERY_SPAN_DAYS} 天），当前 {start_date}~{end_date} 超过限制。",
            "hint": "请将 start_date/end_date 收敛到 1 个月内（例如近 30 天），或按月分别查询。",
        }
    return None


class TooLargeError(Exception):
    """单次区间数据量超过 MAX_RECORDS 时抛出，由工具层转为友好提示。"""
    def __init__(self, total, max_records, begin, end):
        self.total = total
        self.max_records = max_records
        self.begin = begin
        self.end = end
        super().__init__(
            f"区间 {begin}~{end} 数据量约 {total} 条，超过单区间安全上限 {max_records} 条")


def _month_ranges(begin, end):
    """把 [begin,end] 切成自然月子区间列表 [(b1,e1),(b2,e2),...]，字符串 yyyy-MM-dd。"""
    bd = datetime.strptime(begin, "%Y-%m-%d").date()
    ed = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    cur = bd
    while cur <= ed:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1)
        seg_end = min(nxt - timedelta(days=1), ed)
        out.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = nxt
    return out


def _num(v):
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _agg_init():
    """初始化聚合累加器。内存占用仅与「唯一商品/仓库/日期」数量相关，与记录总数无关。"""
    return {
        "count": 0,
        "total_amount": 0.0,
        "total_qty": 0.0,
        "unit_breakdown": defaultdict(float),
        "by_goods": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0, "unit": ""}),
        "by_wh": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
        "by_sup": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
        "by_date": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
        "by_category": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
        "by_goods_uuid": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
    }


def _agg_add(acc, recs):
    """把一批记录累加进 acc；调用后即可丢弃原始记录，避免内存膨胀。"""
    acc["count"] += len(recs)
    for r in recs:
        q = _num(r.get("qty"))
        p = _num(r.get("price"))
        amt = round(q * p, 2)
        acc["total_amount"] += amt
        acc["total_qty"] += q
        acc["unit_breakdown"][r.get("unit") or "未知"] += q
        g = acc["by_goods"][(r.get("goodsName") or "", r.get("unit") or "")]
        g["qty"] += q; g["amount"] += amt; g["cnt"] += 1; g["unit"] = r.get("unit") or ""
        w = acc["by_wh"][r.get("warehouseName") or ""]
        w["qty"] += q; w["amount"] += amt; w["cnt"] += 1
        s = acc["by_sup"][r.get("supplierName") or ""]
        s["qty"] += q; s["amount"] += amt; s["cnt"] += 1
        dt = acc["by_date"][(r.get("inDate") or "")[:10]]
        dt["qty"] += q; dt["amount"] += amt; dt["cnt"] += 1
        cat = acc["by_category"][r.get("firstCategoryName") or "未分类"]
        cat["qty"] += q; cat["amount"] += amt; cat["cnt"] += 1
        gu = r.get("goodsUuid")
        if gu:
            g_u = acc["by_goods_uuid"][gu]
            g_u["qty"] += q; g_u["amount"] += amt; g_u["cnt"] += 1
    return acc


def _aggregate(recs):
    """（兼容）一次性聚合，等价于 init + add。"""
    return _agg_add(_agg_init(), recs)


def _fetch_one_range(client, begin, end, stock_in_types,
                      warehouse_name=None, supplier_name=None, max_records=200000):
    """拉取【单个区间】并流式聚合；首查 total，超 max_records 抛 TooLargeError。

    仅处理单月或任意单段区间；跨月切片由 _fetch_stock_in 负责分发与合并。
    """
    params = {
        "beginDate": begin, "endDate": end, "pageNo": 1,
        "pageSize": PAGE_SIZE, "stockInTypeList": list(stock_in_types),
    }
    # 仓库服务端过滤：page_stock_in 用 wareHouseUuid（大写 H，不同于库存接口的小写 h）
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    if wh_filtered:
        if len(wh_uuids) == 1:
            params["wareHouseUuid"] = wh_uuids[0]
        else:
            params["wareHouseUuidList"] = wh_uuids
    acc = _agg_init()
    page = 1
    first = True
    while True:
        params["pageNo"] = page
        d = client.page_stock_in(params)
        if not d.get("success"):
            raise RuntimeError(f"接口返回失败: {d.get('message')}")
        data = d.get("data") or {}
        if first:
            total = data.get("total")
            if isinstance(total, int) and total > max_records:
                raise TooLargeError(total, max_records, begin, end)
            first = False
        rows = data.get("records") or []
        if not rows:
            break
        kept = []
        for r in rows:
            wn = r.get("warehouseName") or ""
            sn = r.get("supplierName") or ""
            if warehouse_name and warehouse_name not in wn:
                continue
            if supplier_name and supplier_name not in sn:
                continue
            kept.append(r)
        _agg_add(acc, kept)          # 聚合后 kept 即可被 GC 回收
        total_pages = data.get("pages", 1)
        if page >= total_pages or page >= MAX_PAGES:
            break
        page += 1
    return acc


def _merge_acc(dst, src):
    """把 src 累加器合并进 dst（用于跨月切片汇总）。"""
    dst["count"] += src["count"]
    dst["total_amount"] += src["total_amount"]
    dst["total_qty"] += src["total_qty"]
    for k, v in src["unit_breakdown"].items():
        dst["unit_breakdown"][k] += v
    for k, v in src["by_goods"].items():
        g = dst["by_goods"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]; g["unit"] = v["unit"]
    for k, v in src["by_wh"].items():
        g = dst["by_wh"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]
    for k, v in src["by_sup"].items():
        g = dst["by_sup"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]
    for k, v in src["by_date"].items():
        g = dst["by_date"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]
    for k, v in src["by_category"].items():
        g = dst["by_category"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]
    for k, v in src["by_goods_uuid"].items():
        g = dst["by_goods_uuid"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]
    return dst


def _fetch_stock_in(client, begin, end, stock_in_types=None,
                    warehouse_name=None, supplier_name=None):
    """采购入库拉取 + 流式聚合（边拉边聚合，逐页丢弃原始记录，内存与记录总数解耦）。

    超大区间保护（两级自适应）：
    1. 跨多月区间 → 自动按月切片，逐月拉取聚合后合并，内存峰值 = 单月，整年也能跑；
       任一片单月 total 超过 MAX_RECORDS 则抛 TooLargeError（由工具层转友好提示）。
    2. 单月区间 → 直接拉取，首查 total 超 MAX_RECORDS 即抛错，避免全量拉取导致 OOM。

    stock_in_types 默认 ["purchaseIn","purchaseCrossIn"]（越库也是采购，默认计入）。
    """
    if stock_in_types is None:
        stock_in_types = list(PURCHASE_INBOUND_TYPES)
    max_records = config.MAX_RECORDS
    months = _month_ranges(begin, end)
    if len(months) == 1:
        return _fetch_one_range(client, months[0][0], months[0][1], stock_in_types,
                                 warehouse_name, supplier_name, max_records)
    # 跨多月 → 切片聚合
    acc = _agg_init()
    acc["_sliced"] = True
    acc["_months"] = len(months)
    for (mb, me) in months:
        macc = _fetch_one_range(client, mb, me, stock_in_types,
                                 warehouse_name, supplier_name, max_records)
        _merge_acc(acc, macc)
    return acc


def _too_large_result(tool, filters, begin, end, total, max_records):
    """构造「数据量过大」的友好提示结果，供 agent 直接展示，避免崩溃。"""
    msg = (f"您查询的区间（{begin}~{end}）采购数据量较大，约 {total} 条记录"
           f"（系统单区间安全上限 {max_records} 条），直接全量拉取可能超时或内存不足，"
           f"因此本次未返回结果。")
    sug = ("建议：① 缩小到「单月内」查询（如 2026-07-01~2026-07-31）；"
           "② 指定具体维度 / 仓库 / 供应商缩小范围（如「7月 TOP10 供应商」）；"
           "③ 在内存更充足的服务器环境中运行以获取完整结果。")
    return {
        "tool": tool,
        "too_large": True,
        "estimated": total,
        "max_records": max_records,
        "filters": filters,
        "range": f"{begin}~{end}",
        "message": msg,
        "suggestion": sug,
    }


# ---------------------------------------------------------------------------
# 工具 1：采购入库汇总
# ---------------------------------------------------------------------------
def purchase_inbound_summary(client, start_date, end_date,
                             warehouse_name=None, supplier_name=None,
                             only_inbound=False):
    """统计采购数据汇总。

    only_inbound=False（默认）：采购含越库，stockInTypeList=["purchaseIn","purchaseCrossIn"]。
    only_inbound=True：仅采购入库，stockInTypeList=["purchaseIn"]。
    """
    types = list(ONLY_INBOUND_TYPES) if only_inbound else list(PURCHASE_INBOUND_TYPES)
    try:
        agg = _fetch_stock_in(client, start_date, end_date,
                               stock_in_types=types,
                               warehouse_name=warehouse_name,
                               supplier_name=supplier_name)
    except TooLargeError as e:
        return _too_large_result("purchase_inbound_summary",
            {"type": ("purchaseIn+purchaseCrossIn(采购含越库)" if not only_inbound else "purchaseIn(仅采购入库)"),
             "start_date": start_date, "end_date": end_date,
             "warehouse_name": warehouse_name, "supplier_name": supplier_name},
            start_date, end_date, e.total, e.max_records)
    type_label = "purchaseIn+ purchaseCrossIn(采购含越库)" if not only_inbound else "purchaseIn(仅采购入库)"
    return {
        "tool": "purchase_inbound_summary",
        "filters": {
            "type": type_label,
            "start_date": start_date, "end_date": end_date,
            "warehouse_name": warehouse_name, "supplier_name": supplier_name,
        },
        "count": agg["count"],
        "total_amount_est": agg["total_amount"],
        "total_qty": agg["total_qty"],
        "unit_breakdown": agg["unit_breakdown"],
        "note": "total_amount_est = 单价×数量 的估算值；total_qty 跨单位不可直接相加。"
                + (f"；已自动按月切片汇总（共 {agg['_months']} 个月）。" if agg.get("_sliced") else ""),
    }


# ---------------------------------------------------------------------------
# 工具 2：按维度排行
# ---------------------------------------------------------------------------
def rank_by_dimension(client, dimension, metric, start_date, end_date, top_n=10,
                       only_inbound=False, warehouse_name=None):
    """按维度对采购数据排行。默认采购含越库；only_inbound=True 仅采购入库。"""
    types = list(ONLY_INBOUND_TYPES) if only_inbound else list(PURCHASE_INBOUND_TYPES)
    try:
        agg = _fetch_stock_in(client, start_date, end_date, stock_in_types=types,
                              warehouse_name=warehouse_name)
    except TooLargeError as e:
        return _too_large_result("rank_by_dimension",
            {"dimension": dimension, "metric": metric, "top_n": top_n,
             "start_date": start_date, "end_date": end_date, "only_inbound": only_inbound,
             "warehouse_name": warehouse_name},
            start_date, end_date, e.total, e.max_records)
    if dimension == "goods":
        src = agg["by_goods"]
        items = [{"name": k[0], "unit": v["unit"], "amount": round(v["amount"], 2),
                  "qty": round(v["qty"], 2), "count": v["cnt"]} for k, v in src.items()]
    elif dimension == "warehouse":
        src = agg["by_wh"]
        items = [{"name": k, "amount": round(v["amount"], 2),
                  "qty": round(v["qty"], 2), "count": v["cnt"]} for k, v in src.items()]
    elif dimension == "supplier":
        src = agg["by_sup"]
        items = [{"name": k, "amount": round(v["amount"], 2),
                  "qty": round(v["qty"], 2), "count": v["cnt"]} for k, v in src.items()]
    elif dimension == "goods_category":
        # 采购入库/出库记录里没有分类名称，需用 goodsUuid join 商品主数据与分类树
        gc_map = _build_goods_category_map(client)
        src = agg["by_goods_uuid"]
        cat_agg = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0})
        for gu, v in src.items():
            cat_name = gc_map.get(gu) or "未分类"
            c = cat_agg[cat_name]
            c["qty"] += v["qty"]; c["amount"] += v["amount"]; c["cnt"] += v["cnt"]
        items = [{"name": k, "amount": round(v["amount"], 2),
                  "qty": round(v["qty"], 2), "count": v["cnt"]} for k, v in cat_agg.items()]
    else:
        return {"error": f"未知维度: {dimension}"}
    key = {"amount": "amount", "qty": "qty", "count": "count"}.get(metric, "amount")
    items.sort(key=lambda x: x.get(key, 0), reverse=True)
    return {
        "tool": "rank_by_dimension",
        "dimension": dimension, "metric": metric, "top_n": top_n,
        "range": f"{start_date}~{end_date}",
        "items": items[:top_n],
    }


# ---------------------------------------------------------------------------
# 工具 3：按日趋势
# ---------------------------------------------------------------------------
def daily_trend(client, metric, start_date, end_date, only_inbound=False,
                warehouse_name=None):
    """按日统计采购数据趋势。默认采购含越库；only_inbound=True 仅采购入库。"""
    types = list(ONLY_INBOUND_TYPES) if only_inbound else list(PURCHASE_INBOUND_TYPES)
    try:
        agg = _fetch_stock_in(client, start_date, end_date, stock_in_types=types,
                              warehouse_name=warehouse_name)
    except TooLargeError as e:
        return _too_large_result("daily_trend",
            {"metric": metric, "start_date": start_date, "end_date": end_date,
             "only_inbound": only_inbound, "warehouse_name": warehouse_name},
            start_date, end_date, e.total, e.max_records)
    key = {"amount": "amount", "qty": "qty", "count": "count"}.get(metric, "amount")
    points = [{"date": k, key: round(v[key], 2), "count": v["cnt"]}
              for k, v in sorted(agg["by_date"].items())]
    return {
        "tool": "daily_trend", "metric": metric,
        "range": f"{start_date}~{end_date}", "points": points,
    }


# ---------------------------------------------------------------------------
# 工具 4：库存预警（临期/过期）
# ---------------------------------------------------------------------------
def stock_warning(client, warehouse_name=None):
    params = {"pageNo": 1, "pageSize": PAGE_SIZE, "type": "outdated"}
    # 仓库服务端过滤：page_stock 用 warehouseUuid（小写 h）
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    if wh_filtered:
        if len(wh_uuids) == 1:
            params["warehouseUuid"] = wh_uuids[0]
        else:
            params["warehouseUuidList"] = wh_uuids
    outdated, warning = 0, 0
    outdated_items = []
    warning_items = []
    page = 1
    while True:
        params["pageNo"] = page
        d = client.page_stock(params)
        if not d.get("success"):
            raise RuntimeError(f"接口返回失败: {d.get('message')}")
        rows = (d.get("data") or {}).get("records") or (d.get("data") or {}).get("list") or []
        if not rows:
            break
        now = datetime.now()
        for r in rows:
            od = r.get("outdated"); wd = r.get("warnDated")
            is_out = bool(od) and datetime.strptime(od, "%Y-%m-%d %H:%M:%S") <= now
            is_warn = (not is_out) and bool(wd) and datetime.strptime(wd, "%Y-%m-%d %H:%M:%S") <= now
            item = {
                "goodsName": r.get("goodsName") or "—",
                "warehouseName": r.get("warehouseName") or "—",
                "spec": r.get("goodsSpec") or r.get("unit") or "—",
                "batchNo": r.get("batchNo") or "—",
                "sourceBillNumber": r.get("sourceBillNumber") or "—",
                "supplierName": r.get("supplierName") or "—",
                "qty": r.get("qty"),
                "outdated": od,
                "warnDated": wd,
            }
            if is_out:
                outdated += 1
                if len(outdated_items) < 50:
                    outdated_items.append(item)
            elif is_warn:
                warning += 1
                if len(warning_items) < 50:
                    warning_items.append(item)
        total_pages = (d.get("data") or {}).get("pages", 1)
        if page >= total_pages or page >= MAX_PAGES:
            break
        page += 1
    return {
        "tool": "stock_warning",
        "warehouse_name": warehouse_name,
        "outdated_count": outdated, "warning_count": warning,
        "outdated_items": outdated_items,
        "warning_items": warning_items,
        "sample_items": (outdated_items + warning_items)[:50],
        "note": "outdated=已过期；warning=临期预警中。",
    }


# ---------------------------------------------------------------------------
# 工具 5：当前库存按仓库分类汇总（库存快照）
# ---------------------------------------------------------------------------
def _build_category_map(client):
    """拉取商品分类树（queryGoodsCategory），建立 uuid -> name 映射。"""
    try:
        d = client.query_goods_category({})
        nodes = (d.get("data") or []) if d.get("success") else []
    except Exception:
        nodes = []
    m = {}
    for n in nodes:
        if n.get("uuid") and n.get("name"):
            m[n["uuid"]] = n["name"]
    return m


def _build_goods_category_map(client):
    """建立 goodsUuid -> 一级分类名称 的映射。

    采购入库/出库记录里通常只有 goodsUuid，没有分类名称，因此需要：
    1) 调 queryGoods 拿到所有商品及其 firstCategoryUuid；
    2) 调 queryGoodsCategory 拿到分类 uuid -> name 映射；
    3) 组合成 goodsUuid -> firstCategoryName。
    """
    try:
        d = client.query_goods({})
        goods = (d.get("data") or []) if d.get("success") else []
    except Exception:
        goods = []
    cat_map = _build_category_map(client)
    m = {}
    for g in goods:
        gu = g.get("uuid")
        cu = g.get("firstCategoryUuid")
        if gu and cu:
            m[gu] = cat_map.get(cu) or g.get("firstCategoryName") or "未分类"
    return m


def _resolve_warehouse_uuids(client, warehouse_name):
    """根据仓库名称（支持子串匹配）解析对应的 warehouseUuid 列表。

    page_stock 支持服务端按 warehouseUuid / warehouseUuidList 过滤；
    把用户输入的仓库名映射为 uuid 后传给接口，可显著减少返回总量。
    返回：([], 是否已服务端过滤)
    """
    if not warehouse_name:
        return [], False
    try:
        d = client.query_warehouses({})
        whs = (d.get("data") or []) if d.get("success") else []
    except Exception:
        whs = []
    if not whs:
        return [], False
    name_norm = warehouse_name.strip().lower()
    matched = []
    for w in whs:
        wn = (w.get("warehouseName") or w.get("name") or "").strip()
        if name_norm in wn.lower():
            matched.append(w.get("uuid"))
    # 去重并过滤空值
    uuids = [u for u in dict.fromkeys(matched) if u]
    return uuids, bool(uuids)


def _resolve_supplier_uuids(client, supplier_name):
    """根据供应商名称（支持子串匹配）解析对应的 uuid 列表。

    用于 pagePurStatDayOrSupplier 的 supplierUuidList 服务端过滤。
    返回 uuid 列表（可能为空，表示未匹配到、不附加过滤）。
    """
    if not supplier_name:
        return []
    try:
        d = client.query_suppliers({})
        sups = (d.get("data") or []) if d.get("success") else []
    except Exception:
        sups = []
    if not sups:
        return []
    name_norm = supplier_name.strip().lower()
    matched = []
    for s in sups:
        sn = (s.get("supplierName") or s.get("name") or "").strip()
        if name_norm in sn.lower():
            matched.append(s.get("uuid"))
    return [u for u in dict.fromkeys(matched) if u]


def _fetch_inventory_agg(client, warehouse_name=None, max_pages=MAX_PAGES,
                         max_records=None):
    """拉取当前库存快照（pageStock），一次性按【仓库】和【一级分类】聚合。

    业务规则：
    - 库存为时点快照，无日期。
    - 库存数量为 0 的记录是无效数据，必须剔除（zeroQty=False 服务端过滤 +
      客户端 qty<=0 兜底），否则会混入大量脏数据。
    - 若指定仓库名，优先通过 query_warehouses 解析 uuid，用 warehouseUuid(List)
      传给 page_stock 做服务端过滤，避免把全公司库存 total 误判为超限。
    - 用较大 pageSize（2000）减少翻页次数；流式聚合，内存只保留聚合结果。
    - MAX_RECORDS 保护：zeroQty 已让服务端过滤 qty<=0 的无效库存，仓库过滤也可能叠加，
      此时接口返回的 total 往往是“未过滤全量”（如 68293）而非真实返回量，若据此前置拦截
      会误杀已过滤的小结果。因此：仅当“无任何服务端过滤”时才用 total 做前置拦截；
      其余情况改由“累计实际返回行数 fetched”做渐进式拦截（内存封顶 max_records）。
    """
    max_records = max_records or config.MAX_RECORDS
    cat_name = _build_category_map(client)
    wh_uuids, server_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    # zeroQty：服务端直接过滤掉 qty<=0 的无效库存（口径见 metrics_registry.INVENTORY_ZERO_QTY）
    params = {"pageNo": 1, "pageSize": INV_PAGE_SIZE, "zeroQty": INVENTORY_ZERO_QTY}
    if server_filtered:
        if len(wh_uuids) == 1:
            params["warehouseUuid"] = wh_uuids[0]
        else:
            params["warehouseUuidList"] = wh_uuids
    # zeroQty 始终在 params 中开启服务端过滤，故服务端过滤恒为真；
    # 接口返回的 total 会是未过滤全量，不能据此前置拦截。
    server_side_filtering = True
    wh = defaultdict(lambda: {"qty": 0.0, "amount": 0.0,
                              "goods": set(), "unit_breakdown": defaultdict(float)})
    cat = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "goods": set()})
    total_qty = 0.0
    total_amount = 0.0
    total_goods = set()
    valid_rows = 0
    page = 1
    pages = 1
    total = None
    fetched = 0
    while page <= max_pages:
        params["pageNo"] = page
        print(f"[inventory] fetching page {page}/{pages or '?'} (fetched {fetched})",
              file=sys.stderr, flush=True)
        d = client.page_stock(params)
        if not d.get("success"):
            raise RuntimeError(f"接口返回失败: {d.get('message')}")
        data = d.get("data") or {}
        rows = data.get("records") or data.get("list") or []
        pages = data.get("pages", 1)
        if total is None:
            total = data.get("total")
            # 有服务端过滤时，total 可能是未过滤全量，不能据此前置拦截；
            # 仅“无任何服务端过滤”时才用 total 前置拦截。
            if (total is not None and total > max_records
                    and not server_side_filtering):
                raise TooLargeError(total, max_records, "当前库存", "全部")
        if not rows:
            break
        fetched += len(rows)
        if fetched > max_records:
            raise TooLargeError(fetched, max_records, "当前库存", "全部")
        for r in rows:
            q = _num(r.get("qty"))
            if q <= INVENTORY_VALID_QTY_MIN:
                continue  # 无效库存（数量 <= 阈值，口径见 metrics_registry.INVENTORY_VALID_QTY_MIN），剔除
            wn = r.get("warehouseName") or "未知仓库"
            if warehouse_name and warehouse_name not in wn:
                continue  # 按仓库筛选：不匹配则整条跳过（分类/全局维度同步过滤）
            p = _num(r.get("price"))
            amt = round(q * p, 2)
            gn = r.get("goodsName") or ""
            u = r.get("unit") or "未知"
            # —— 仓库维度 ——
            w = wh[wn]
            w["qty"] += q
            w["amount"] += amt
            w["goods"].add((gn, u))
            w["unit_breakdown"][u] += q
            # —— 一级分类维度 ——
            cuuid = r.get("goodsFirstCategoryUuid")
            cname = cat_name.get(cuuid) or "未分类"
            c = cat[cname]
            c["qty"] += q
            c["amount"] += amt
            c["goods"].add((gn, u))
            # —— 全局 ——
            total_qty += q
            total_amount += amt
            total_goods.add((gn, u))
            valid_rows += 1
        print(f"[inventory] page {page} done: +{len(rows)} raw, valid {valid_rows}",
              file=sys.stderr, flush=True)
        if page >= pages:
            break
        page += 1
    else:
        print(f"[inventory] reached max_pages={max_pages}, truncating", file=sys.stderr, flush=True)
        return {
            "wh": wh, "cat": cat, "total_qty": total_qty, "total_amount": total_amount,
            "total_goods": total_goods, "valid_rows": valid_rows, "truncated": True,
        }
    print(f"[inventory] completed: raw {fetched}, valid {valid_rows}, "
          f"{len(wh)} warehouses, {len(cat)} categories", file=sys.stderr, flush=True)
    return {
        "wh": wh, "cat": cat, "total_qty": total_qty, "total_amount": total_amount,
        "total_goods": total_goods, "valid_rows": valid_rows, "truncated": False,
    }


def inventory_by_warehouse(client, warehouse_name=None):
    """当前库存商品按仓库分类汇总：每仓库商品种类数、合计数量、估算金额。"""
    try:
        inv = _fetch_inventory_agg(client, warehouse_name)
    except TooLargeError as e:
        msg = (f"当前库存数据量较大，约 {e.total} 条记录"
               f"（系统单区间安全上限 {e.max_records} 条），直接全量拉取可能超时或内存不足，"
               f"因此本次未返回结果。")
        sug = ("建议：① 指定具体仓库名称查询；② 使用库存预警工具查看临期/过期商品；"
               "③ 在内存更充足的服务器环境中运行以获取完整结果。")
        return {
            "tool": "inventory_by_warehouse",
            "too_large": True,
            "estimated": e.total,
            "max_records": e.max_records,
            "filters": {"warehouse_name": warehouse_name},
            "message": msg,
            "suggestion": sug,
        }
    wh_list = []
    for name, w in inv["wh"].items():
        wh_list.append({
            "warehouse": name,
            "goods_count": len(w["goods"]),
            "qty": round(w["qty"], 2),
            "amount_est": round(w["amount"], 2),
        })
    wh_list.sort(key=lambda x: x["qty"], reverse=True)
    return {
        "tool": "inventory_by_warehouse",
        "filters": {"warehouse_name": warehouse_name},
        "warehouses": wh_list,
        "total_goods": len(inv["total_goods"]),
        "total_qty": round(inv["total_qty"], 2),
        "total_amount_est": round(inv["total_amount"], 2),
        "valid_rows": inv["valid_rows"],
        "truncated": inv["truncated"],
        "note": "仅统计库存数量>0 的有效库存（数量为 0 的无效数据已剔除）；"
                "amount_est = 单价×数量 的估算值（无单价的记录不计入金额）；"
                "goods_count = 该仓库商品种类数（按商品名+单位去重）；"
                "库存为当前时点快照，无需日期范围。"
                + ("；结果已达分页上限被截断，仅反映部分仓库。" if inv["truncated"] else ""),
    }


def inventory_by_category(client, warehouse_name=None):
    """当前库存商品按【一级商品分类】汇总占比：每分类商品种类数、合计数量、估算金额、占比。

    分类名来自 queryGoodsCategory（pageStock 记录本身分类名为空，仅含 uuid）。
    库存数量=0 的无效记录已剔除。
    """
    try:
        inv = _fetch_inventory_agg(client, warehouse_name)
    except TooLargeError as e:
        msg = (f"当前库存数据量较大，约 {e.total} 条记录"
               f"（系统单区间安全上限 {e.max_records} 条），直接全量拉取可能超时或内存不足，"
               f"因此本次未返回结果。")
        sug = ("建议：① 指定具体仓库名称缩小范围；② 在内存更充足的服务器环境中运行以获取完整结果。")
        return {
            "tool": "inventory_by_category",
            "too_large": True,
            "estimated": e.total,
            "max_records": e.max_records,
            "filters": {"warehouse_name": warehouse_name},
            "message": msg,
            "suggestion": sug,
        }
    cat_list = []
    for name, c in inv["cat"].items():
        cat_list.append({
            "category": name,
            "goods_count": len(c["goods"]),
            "qty": round(c["qty"], 2),
            "amount_est": round(c["amount"], 2),
        })
    cat_list.sort(key=lambda x: x["qty"], reverse=True)
    tq = inv["total_qty"] or 1.0
    for item in cat_list:
        item["qty_ratio"] = round(item["qty"] / tq * 100, 2)
    return {
        "tool": "inventory_by_category",
        "filters": {"warehouse_name": warehouse_name},
        "categories": cat_list,
        "total_goods": len(inv["total_goods"]),
        "total_qty": round(inv["total_qty"], 2),
        "total_amount_est": round(inv["total_amount"], 2),
        "valid_rows": inv["valid_rows"],
        "truncated": inv["truncated"],
        "note": "仅统计库存数量>0 的有效库存；分类名为一级商品分类（join queryGoodsCategory）；"
                "qty_ratio = 该分类数量 / 有效库存总数量 的百分比；"
                "amount_est = 单价×数量 的估算值（无单价的记录不计入金额）。"
                + ("；结果已达分页上限被截断，仅反映部分分类。" if inv["truncated"] else ""),
    }


# ---------------------------------------------------------------------------
# 工具 6：采购入库按仓库分类汇总（purchaseIn + purchaseCrossIn 含越库）
# ---------------------------------------------------------------------------
def purchase_inbound_by_warehouse(client, start_date, end_date, warehouse_name=None):
    """采购入库按仓库分类汇总（含越库）。

    业务口径：采购越库(purchaseCrossIn)在【入库】侧属于采购入库的一部分，
    因此本工具默认同时统计 purchaseIn(采购入库) 与 purchaseCrossIn(采购越库)。
    （采购越库在【出库】侧则归入「领料出库」，见 stock_out_by_warehouse。）
    """
    _wh_err = _require_warehouse(warehouse_name, "purchase_inbound_by_warehouse")
    if _wh_err:
        return _wh_err
    _span_err = _check_max_span(start_date, end_date, "purchase_inbound_by_warehouse")
    if _span_err:
        return _span_err
    try:
        agg = _fetch_stock_in(client, start_date, end_date,
                               stock_in_types=list(PURCHASE_INBOUND_TYPES),
                               warehouse_name=warehouse_name)
    except TooLargeError as e:
        return _too_large_result("purchase_inbound_by_warehouse",
            {"start_date": start_date, "end_date": end_date,
             "warehouse_name": warehouse_name, "type": "purchaseIn+purchaseCrossIn(采购入库,含越库)"},
            start_date, end_date, e.total, e.max_records)
    wh_list = [{"warehouse": k, "count": v["cnt"], "qty": round(v["qty"], 2),
                "amount_est": round(v["amount"], 2)} for k, v in agg["by_wh"].items()]
    wh_list.sort(key=lambda x: x["qty"], reverse=True)
    return {
        "tool": "purchase_inbound_by_warehouse",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "type": "purchaseIn+purchaseCrossIn(采购入库,含越库)"},
        "warehouses": wh_list,
        "total_count": agg["count"],
        "total_qty": round(agg["total_qty"], 2),
        "total_amount_est": round(agg["total_amount"], 2),
        "note": "amount_est = 单价×数量 的估算值；本工具含采购入库(purchaseIn)与采购越库(purchaseCrossIn)。"
                + (f"；已自动按月切片汇总（共 {agg['_months']} 个月）。" if agg.get("_sliced") else ""),
    }


# ---------------------------------------------------------------------------
# 工具 7：出库记录按仓库分类汇总（按出库类型拆分，采购越库归入领料出库）
# ---------------------------------------------------------------------------
def _out_type_label(raw):
    """出库类型归一化：采购越库在【出库】侧按业务口径归入「领料出库」，不再单列。
    实现委托给口径注册表 normalize_out_type（SSOT，避免与注册表口径漂移）。"""
    return normalize_out_type(raw)


def _fetch_one_range_out(client, begin, end, stock_out_types,
                         warehouse_name, max_records):
    """拉取【单个区间】出库记录并聚合（按仓库 + 按出库类型）；首查 total 超上限抛 TooLargeError。"""
    params = {"beginDate": begin, "endDate": end, "pageNo": 1,
              "pageSize": PAGE_SIZE, "dateType": 0}
    if stock_out_types:
        params["stockOutTypeList"] = list(stock_out_types)
    # 仓库服务端过滤：page_stock_out 用 wareHouseUuid（大写 H，不同于库存接口的小写 h）
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    if wh_filtered:
        if len(wh_uuids) == 1:
            params["wareHouseUuid"] = wh_uuids[0]
        else:
            params["wareHouseUuidList"] = wh_uuids
    acc = {"by_wh": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0,
                                         "by_type": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0})}),
           "by_type": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
           "count": 0, "total_qty": 0.0, "total_amount": 0.0}
    page = 1
    first = True
    while True:
        params["pageNo"] = page
        d = client.page_stock_out(params)
        if not d.get("success"):
            raise RuntimeError(f"接口返回失败: {d.get('message')}")
        data = d.get("data") or {}
        if first:
            total = data.get("total")
            if isinstance(total, int) and total > max_records:
                raise TooLargeError(total, max_records, begin, end)
            first = False
        rows = data.get("records") or data.get("list") or []
        if not rows:
            break
        for r in rows:
            wn = r.get("warehouseName") or ""
            if warehouse_name and warehouse_name not in wn:
                continue
            q = _num(r.get("qty"))
            p = _num(r.get("price"))
            amt = round(q * p, 2)
            t = _out_type_label(r.get("stockOutType") or r.get("outType"))
            w = acc["by_wh"][wn]
            wt = w["by_type"][t]
            w["qty"] += q; w["amount"] += amt; w["cnt"] += 1
            wt["qty"] += q; wt["amount"] += amt; wt["cnt"] += 1
            g = acc["by_type"][t]
            g["qty"] += q; g["amount"] += amt; g["cnt"] += 1
            acc["count"] += 1
            acc["total_qty"] += q
            acc["total_amount"] += amt
        total_pages = data.get("pages", 1)
        if page >= total_pages or page >= MAX_PAGES:
            break
        page += 1
    return acc


def _merge_out(dst, src):
    """把 src 出库累加器合并进 dst（跨月切片汇总用）。"""
    dst["count"] += src["count"]
    dst["total_qty"] += src["total_qty"]
    dst["total_amount"] += src["total_amount"]
    for k, v in src["by_wh"].items():
        w = dst["by_wh"][k]
        w["qty"] += v["qty"]; w["amount"] += v["amount"]; w["cnt"] += v["cnt"]
        for tk, tv in v["by_type"].items():
            tw = w["by_type"][tk]
            tw["qty"] += tv["qty"]; tw["amount"] += tv["amount"]; tw["cnt"] += tv["cnt"]
    for k, v in src["by_type"].items():
        g = dst["by_type"][k]
        g["qty"] += v["qty"]; g["amount"] += v["amount"]; g["cnt"] += v["cnt"]
    return dst


def _fetch_stock_out_range(client, begin, end, stock_out_types=None, warehouse_name=None):
    """出库拉取 + 流式聚合，跨月自动按月切片合并（与采购入库保护策略一致）。"""
    max_records = config.MAX_RECORDS
    months = _month_ranges(begin, end)
    if len(months) == 1:
        return _fetch_one_range_out(client, months[0][0], months[0][1],
                                     stock_out_types, warehouse_name, max_records)
    acc = {"by_wh": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0,
                                         "by_type": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0})}),
           "by_type": defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0}),
           "count": 0, "total_qty": 0.0, "total_amount": 0.0,
           "_sliced": True, "_months": len(months)}
    for (mb, me) in months:
        macc = _fetch_one_range_out(client, mb, me, stock_out_types,
                                     warehouse_name, max_records)
        _merge_out(acc, macc)
    return acc


def stock_out_by_warehouse(client, start_date, end_date, warehouse_name=None,
                           stock_out_types=None):
    """出库记录按仓库分类汇总，并给出按出库类型（如领料出库）的拆分。

    业务口径：采购越库在【出库】侧归入「领料出库」（不会单独列示）。
    stock_out_types 为可选的出库类型编码列表；不传则统计全部出库类型。
    """
    _wh_err = _require_warehouse(warehouse_name, "stock_out_by_warehouse")
    if _wh_err:
        return _wh_err
    _span_err = _check_max_span(start_date, end_date, "stock_out_by_warehouse")
    if _span_err:
        return _span_err
    try:
        acc = _fetch_stock_out_range(client, start_date, end_date,
                                     stock_out_types, warehouse_name)
    except TooLargeError as e:
        return _too_large_result("stock_out_by_warehouse",
            {"start_date": start_date, "end_date": end_date,
             "warehouse_name": warehouse_name, "stock_out_types": stock_out_types},
            start_date, end_date, e.total, e.max_records)
    wh_list = []
    for name, w in acc["by_wh"].items():
        wt = {t: {"qty": round(v["qty"], 2), "amount_est": round(v["amount"], 2),
                  "count": v["cnt"]} for t, v in w["by_type"].items()}
        wh_list.append({"warehouse": name, "count": w["cnt"], "qty": round(w["qty"], 2),
                        "amount_est": round(w["amount"], 2), "by_type": wt})
    wh_list.sort(key=lambda x: x["qty"], reverse=True)
    by_type = {t: {"count": v["cnt"], "qty": round(v["qty"], 2),
                   "amount_est": round(v["amount"], 2)} for t, v in acc["by_type"].items()}
    return {
        "tool": "stock_out_by_warehouse",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "stock_out_types": stock_out_types},
        "warehouses": wh_list,
        "by_type": by_type,
        "total_count": acc["count"],
        "total_qty": round(acc["total_qty"], 2),
        "total_amount_est": round(acc["total_amount"], 2),
        "note": "amount_est = 单价×数量 的估算值；by_type 为按出库类型的拆分（采购越库在出库侧归入「领料出库」）；"
                "不指定 stock_out_types 时统计全部出库类型。"
                + (f"；已自动按月切片汇总（共 {acc['_months']} 个月）。" if acc.get("_sliced") else ""),
    }


# ===========================================================================
# 工具 8/9/10：服务端聚合接口（金额准确，首选，规避翻页估算与 OOM）
# ===========================================================================
def purchase_stat(client, start_date, end_date, warehouse_name=None, supplier_name=None):
    """采购统计（区间汇总）—— 直接调用服务端聚合接口，金额准确（非估算）。

    接口 pagePurStatDayOrSupplier 顶层已返回服务端算好的汇总：
      inSubtotalTotal(入库金额) / inCrossSubtotalTotal(越库金额) /
      outSubtotalTotal(出库金额) / subSubtotalTotal(金额小计=入库-出库)。
    业务口径（采购含越库默认计入）：
      采购总额(含越库) = 入库金额 + 越库金额
      结余 = 采购总额(含越库) - 出库金额
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    sup_uuids = _resolve_supplier_uuids(client, supplier_name)
    params = {"beginDate": start_date, "endDate": end_date, "type": 1,
              "pageNo": 1, "pageSize": 1}
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    if sup_uuids:
        params["supplierUuidList"] = sup_uuids
    d = client.page_pur_stat(params)
    if not d.get("success"):
        raise RuntimeError(f"接口返回失败: {d.get('message')}")
    data = d.get("data") or {}
    in_amt = _num(data.get("inSubtotalTotal"))
    in_qty = _num(data.get("inQtyTotal"))
    cross_amt = _num(data.get("inCrossSubtotalTotal"))
    cross_qty = _num(data.get("inCrossQtyTotal"))
    out_amt = _num(data.get("outSubtotalTotal"))
    out_qty = _num(data.get("outQtyTotal"))
    pur_amt = round(in_amt + cross_amt, 2)
    pur_qty = round(in_qty + cross_qty, 2)
    sub_amt = round(pur_amt - out_amt, 2)
    sub_qty = round(pur_qty - out_qty, 2)
    return {
        "tool": "purchase_stat",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "in_amount_total": round(in_amt, 2),
        "in_qty_total": round(in_qty, 2),
        "cross_amount_total": round(cross_amt, 2),
        "cross_qty_total": round(cross_qty, 2),
        "purchase_amount_incl_cross": pur_amt,
        "purchase_qty_incl_cross": pur_qty,
        "out_amount_total": round(out_amt, 2),
        "out_qty_total": round(out_qty, 2),
        "sub_amount": sub_amt,
        "sub_qty": sub_qty,
        "sub_amount_raw": round(_num(data.get("subSubtotalTotal")), 2),
        "note": "金额均为服务端聚合真实金额（非估算）。"
                "口径：采购总额(含越库)=入库金额+越库金额；结余=采购总额(含越库)-出库金额。"
                "若只看纯采购入库(不含越库)见 in_amount_total；越库单独见 cross_amount_total。",
    }


def purchase_ledger(client, start_date, end_date, warehouse_name=None, top_n=10):
    """采购台账（明细聚合）—— 直接调用服务端聚合接口，subtotal 为真实小计（非估算）。

    支持单仓库过滤（台账接口 warehouseUuid 仅支持单个 uuid，按模糊匹配取首个命中）。
    返回：台账总览(purAmount/purCount/stockInCount/supplierCount) +
         按商品/供应商/一级分类的采购额(subtotal)排行 TOP N + 明细样例。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"beginDate": start_date, "endDate": end_date}
    if wh_filtered:
        params["warehouseUuid"] = wh_uuids[0]  # 台账仅支持单仓
    d = client.get_purchase_ledger(params)
    if not d.get("success"):
        raise RuntimeError(f"接口返回失败: {d.get('message')}")
    data = d.get("data") or {}
    details = data.get("details") or []
    total_details = len(details)
    # 流式聚合（细节多也只保留聚合结果）；封顶 MAX_RECORDS 防超大区间内存爆
    max_records = config.MAX_RECORDS
    by_goods = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0, "unit": ""})
    by_sup = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0})
    by_cat = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "cnt": 0})
    processed = 0
    truncated = False
    for r in details:
        processed += 1
        if processed > max_records:
            truncated = True
            break
        q = _num(r.get("qty"))
        amt = _num(r.get("subtotal"))
        g = by_goods[(r.get("goodsName") or "", r.get("unit") or "")]
        g["qty"] += q; g["amount"] += amt; g["cnt"] += 1; g["unit"] = r.get("unit") or ""
        s = by_sup[r.get("supplierName") or ""]
        s["qty"] += q; s["amount"] += amt; s["cnt"] += 1
        c = by_cat[r.get("firstCategoryName") or "未分类"]
        c["qty"] += q; c["amount"] += amt; c["cnt"] += 1

    def _top(src, with_unit=False):
        items = []
        for k, v in src.items():
            it = {"name": k, "amount": round(v["amount"], 2),
                  "qty": round(v["qty"], 2), "count": v["cnt"]}
            if with_unit:
                it["unit"] = v.get("unit", "")
            items.append(it)
        items.sort(key=lambda x: x["amount"], reverse=True)
        return items[:top_n]

    return {
        "tool": "purchase_ledger",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "top_n": top_n},
        "summary": {
            "pur_amount": round(_num(data.get("purAmount")), 2),
            "pur_count": data.get("purCount"),
            "stock_in_count": data.get("stockInCount"),
            "supplier_count": data.get("supplierCount"),
        },
        "by_goods_top": _top(by_goods, with_unit=True),
        "by_supplier_top": _top(by_sup),
        "by_category_top": _top(by_cat),
        "details_sample": [{
            "goodsName": r.get("goodsName"), "supplierName": r.get("supplierName"),
            "warehouseName": r.get("warehouseName"), "firstCategoryName": r.get("firstCategoryName"),
            "price": r.get("price"), "qty": r.get("qty"), "subtotal": r.get("subtotal"),
            "unit": r.get("unit"), "inDate": r.get("inDate"),
        } for r in details[:50]],
        "total_details": total_details,
        "processed": processed,
        "truncated": truncated,
        "note": "金额均为服务端聚合真实金额（subtotal 小计，非估算）。"
                "台账仓库过滤仅支持单仓库（按模糊匹配取首个命中仓库）；"
                "by_*_top 为按 subtotal 金额排行的 TOP N 聚合结果。"
                + (f"；明细过多已截断处理（仅聚合前 {max_records} 行）。" if truncated else ""),
    }


def stock_snapshot(client, report_date, warehouse_name=None, top_n=10):
    """进销存库存快照（指定日期）—— 直接调用服务端聚合接口，金额准确（非估算）。

    接口 pageStockSnapshotReport 必填 reportDate；顶层返回进销存流水汇总
    （期初/采购入库/领料出库/盘盈盘亏/调拨/加工/退货/期末库存金额）+ 分页 records 明细。
    records 自带 firstCategoryName/warehouseName/goodsName（无需 join 分类树），
    可直接按分类/仓库/商品聚合。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"reportDate": report_date, "pageNo": 1, "pageSize": 500}
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    try:
        d = client.page_stock_snapshot(params, max_bytes=config.SNAPSHOT_MAX_BYTES, timeout=150)
        if not d.get("success"):
            raise RuntimeError(f"接口返回失败: {d.get('message')}")
    except ResponseTooLarge as e:
        return {
            "tool": "stock_snapshot",
            "too_large": True,
            "max_bytes": e.max_bytes,
            "filters": {"report_date": report_date, "warehouse_name": warehouse_name},
            "message": f"库存快照（{report_date}）数据量过大（响应超过 {e.max_bytes // 1024 // 1024}MB），"
                       f"直接全量拉取会导致内存溢出，因此本次未返回明细。",
            "suggestion": "建议：① 指定具体仓库名称（warehouse_name）缩小范围；"
                          "② 在内存更充足的服务器环境中运行以获取完整快照；"
                          "③ 使用库存预警工具(stock_warning)查看临期/过期商品。",
        }
    except Exception as e:
        msg = str(e)
        is_timeout = ("timed out" in msg) or ("timeout" in msg.lower())
        return {
            "tool": "stock_snapshot",
            "too_large": True,
            "timeout": is_timeout,
            "filters": {"report_date": report_date, "warehouse_name": warehouse_name},
            "message": (f"库存快照（{report_date}）查询{'超时' if is_timeout else '失败'}，"
                        "无法在本地完成" + ("（服务端对全公司库存快照处理较慢）" if is_timeout else "")),
            "suggestion": "建议：① 指定具体仓库名称（warehouse_name）缩小范围，快照会快很多；"
                          "② 在内存/网络更充足的服务器环境中运行；"
                          "③ 使用库存预警工具(stock_warning)查看临期/过期商品。",
        }
    data = d.get("data") or {}
    summary = {
        "begin_stock_amount": round(_num(data.get("beginStockAmount")), 2),
        "begin_stock_qty": round(_num(data.get("beginStockQty")), 2),
        "purchase_in_amount": round(_num(data.get("purchaseInAmount")), 2),
        "purchase_in_qty": round(_num(data.get("purchaseInQty")), 2),
        "stock_out_amount": round(_num(data.get("stockOutAmount")), 2),
        "stock_out_qty": round(_num(data.get("stockOutQty")), 2),
        "return_out_amount": round(_num(data.get("returnOutAmount")), 2),
        "return_out_qty": round(_num(data.get("returnOutQty")), 2),
        "return_in_amount": round(_num(data.get("returnInAmount")), 2),
        "return_in_qty": round(_num(data.get("returnInQty")), 2),
        "inventory_in_amount": round(_num(data.get("inventoryInAmount")), 2),
        "inventory_in_qty": round(_num(data.get("inventoryInQty")), 2),
        "inventory_out_amount": round(_num(data.get("inventoryOutAmount")), 2),
        "inventory_out_qty": round(_num(data.get("inventoryOutQty")), 2),
        "allocate_in_amount": round(_num(data.get("allocateInAmount")), 2),
        "allocate_in_qty": round(_num(data.get("allocateInQty")), 2),
        "allocate_out_amount": round(_num(data.get("allocateOutAmount")), 2),
        "allocate_out_qty": round(_num(data.get("allocateOutQty")), 2),
        "process_in_amount": round(_num(data.get("processInAmount")), 2),
        "process_in_qty": round(_num(data.get("processInQty")), 2),
        "process_out_amount": round(_num(data.get("processOutAmount")), 2),
        "process_out_qty": round(_num(data.get("processOutQty")), 2),
        "stock_amount": round(_num(data.get("stockAmount")), 2),
        "stock_qty": round(_num(data.get("stockQty")), 2),
    }
    # 翻页拉 records 聚合（封顶 MAX_RECORDS）
    by_cat = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    by_wh = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    by_goods = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "unit": ""})
    max_records = config.MAX_RECORDS
    fetched = 0
    truncated = False
    pages = data.get("pages", 1)
    records_incomplete = False
    while True:
        rows = data.get("records") or []
        for r in rows:
            fetched += 1
            if fetched > max_records:
                truncated = True
                break
            q = _num(r.get("qty"))
            amt = _num(r.get("stockAmount"))
            c = by_cat[r.get("firstCategoryName") or "未分类"]
            c["qty"] += q; c["amount"] += amt
            w = by_wh[r.get("warehouseName") or "未知仓库"]
            w["qty"] += q; w["amount"] += amt
            g = by_goods[(r.get("goodsName") or "", r.get("unit") or "")]
            g["qty"] += q; g["amount"] += amt; g["unit"] = r.get("unit") or ""
        if truncated:
            break
        if params["pageNo"] >= pages:
            break
        params["pageNo"] += 1
        try:
            d2 = client.page_stock_snapshot(params)
        except Exception:
            # 翻页异常（多为超时）：保留已聚合结果，标记维度不完整
            records_incomplete = True
            break
        if not d2.get("success"):
            records_incomplete = True
            break
        data = d2.get("data") or {}
        pages = data.get("pages", pages)
    cat_list = sorted(({"category": k, "qty": round(v["qty"], 2), "amount": round(v["amount"], 2)}
                       for k, v in by_cat.items()), key=lambda x: x["amount"], reverse=True)
    wh_list = sorted(({"warehouse": k, "qty": round(v["qty"], 2), "amount": round(v["amount"], 2)}
                     for k, v in by_wh.items()), key=lambda x: x["amount"], reverse=True)
    goods_list = sorted(({"name": k[0], "unit": v["unit"], "qty": round(v["qty"], 2), "amount": round(v["amount"], 2)}
                         for k, v in by_goods.items()), key=lambda x: x["amount"], reverse=True)
    return {
        "tool": "stock_snapshot",
        "filters": {"report_date": report_date, "warehouse_name": warehouse_name},
        "summary": summary,
        "by_category": cat_list,
        "by_warehouse": wh_list,
        "by_goods_top": goods_list[:top_n],
        "fetched_records": fetched,
        "truncated": truncated,
        "records_incomplete": records_incomplete,
        "note": "金额均为服务端聚合真实金额（非估算）。"
                "report_date 为快照日期（指定某天的进销存时点，默认今天）；"
                "summary 中 stock_amount=期末库存金额、stock_qty=期末库存数量；"
                "purchase_in_*=采购入库、stock_out_*=领料出库(含采购越库)、return_out_*=采购退货、return_in_*=领料退库。"
                + ("；records 过多已截断处理。" if truncated else "")
                + ("；records 维度因翻页异常（超时等）仅聚合了部分页，分类/仓库维度可能不完整，但顶层汇总金额准确。" if records_incomplete else ""),
    }


# ===========================================================================
# 工具 11~16：Phase 1 供应链管理扩展（服务端聚合/分页聚合，金额准确）
# ===========================================================================

def _iter_pages(client, call_fn, params, on_page, max_pages=MAX_PAGES, max_records=None):
    """通用分页迭代：逐页调用 call_fn(params)，把每页 (data, rows) 交给 on_page 聚合，
    聚合后丢弃原始记录，避免把全量记录留在内存导致 OOM。

    返回首页 data（含顶层统计，如配送履约状态）。首查若 total 超过 max_records 直接抛
    TooLargeError（由工具层转友好提示）。内存峰值 = 单页记录 + 聚合结果，与总记录数解耦。
    """
    p = dict(params)
    p["pageNo"] = 1
    first_data = None
    total = None
    pages = 1
    while True:
        d = call_fn(p)
        if not d.get("success"):
            raise RuntimeError(f"接口返回失败: {d.get('message')}")
        data = d.get("data") or {}
        if first_data is None:
            first_data = data
            total = data.get("total")
            pages = data.get("pages", 1)
            if (total is not None and max_records and total > max_records):
                begin = params.get("beginDate") or params.get("startDate") or "区间"
                end = params.get("endDate") or params.get("endDate") or "区间"
                raise TooLargeError(total, max_records, begin, end)
        rows = data.get("records") or data.get("list") or []
        on_page(data, rows)
        if p["pageNo"] >= pages or p["pageNo"] >= max_pages:
            break
        p["pageNo"] += 1
    return first_data


# ---------------------------------------------------------------------------
# 工具 11：供应商采购结算统计（供应商绩效）
# ---------------------------------------------------------------------------
def supplier_settlement(client, start_date, end_date, warehouse_name=None, supplier_name=None, top_n=10):
    """供应商采购结算统计：按供应商(客户)返回入库总金额/结算总金额/实退总金额，并给出按结算金额排行 TOP N。

    接口 pageSupplierPurchaseSettleStatistics 顶层 records 已是「每供应商」的聚合
    （purchaseTotalAmountSum 入库总金额合计 / settleTotalAmountSum 结算总金额合计 /
    hasReturnTotalAmountSum 实退总金额合计），逐页累加即得全量；金额准确（非估算）。
    支持 warehouse_name（warehouseUuidList 服务端过滤）、supplier_name 过滤。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    sup_uuids = _resolve_supplier_uuids(client, supplier_name)
    params = {"beginDate": start_date, "endDate": end_date, "pageNo": 1, "pageSize": 200}
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    if sup_uuids:
        params["supplierUuid"] = sup_uuids[0]  # 该接口仅支持单供应商
    by_sup = defaultdict(lambda: {"purchase": 0.0, "settle": 0.0, "return": 0.0})
    tot_purchase = tot_settle = tot_return = 0.0
    cnt = 0

    def _on(data, rows):
        nonlocal tot_purchase, tot_settle, tot_return, cnt
        for r in rows:
            name = r.get("orgName") or r.get("orgUuid") or "未知供应商"
            pur = _num(r.get("purchaseTotalAmountSum"))
            set_ = _num(r.get("settleTotalAmountSum"))
            ret = _num(r.get("hasReturnTotalAmountSum"))
            s = by_sup[name]
            s["purchase"] += pur; s["settle"] += set_; s["return"] += ret
            tot_purchase += pur; tot_settle += set_; tot_return += ret
            cnt += 1

    _iter_pages(client, client.page_supplier_settle, params, _on, max_records=config.MAX_RECORDS)
    items = sorted(({"name": k, "purchase_amount": round(v["purchase"], 2),
                     "settle_amount": round(v["settle"], 2), "return_amount": round(v["return"], 2)}
                    for k, v in by_sup.items()),
                   key=lambda x: x["settle_amount"], reverse=True)
    return {
        "tool": "supplier_settlement",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "total_suppliers": cnt,
        "total_purchase_amount": round(tot_purchase, 2),
        "total_settle_amount": round(tot_settle, 2),
        "total_return_amount": round(tot_return, 2),
        "top_n": top_n,
        "by_supplier_top": items[:top_n],
        "note": "金额=接口返回真实合计：入库总金额(purchaseTotalAmountSum)/结算总金额(settleTotalAmountSum)/"
                "实退总金额(hasReturnTotalAmountSum)，非估算；按结算金额(settle)排行 TOP N。",
    }


# ---------------------------------------------------------------------------
# 工具 12：配送履约与验收差异
# ---------------------------------------------------------------------------
def delivery_fulfillment(client, start_date, end_date, warehouse_name=None, supplier_name=None, top_n=10):
    """配送履约与验收差异：返回配送履约状态（待分拣/待发货/待验收/已验收）+ 按供应商/分类/仓库的
    采购金额、入库金额、验收差异金额、报废金额的聚合，以及验收状态分布。

    接口 pageDetailsAndStat 顶层直接返回 notSorting/notDelivery/notStockIn/stockIned 四个履约计数；
    records 为配送明细（含 subtotal 采购金额 / stockInSubtotal 入库金额 / diffSubtotal 差异金额 /
    scrapSubtotal 报废金额 / supplierName / firstCategoryName / warehouseName / status）。金额准确（非估算）。
    支持 warehouse_name（warehouseUuidList）、supplier_name（supplierUuidList）过滤。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    sup_uuids = _resolve_supplier_uuids(client, supplier_name)
    params = {"beginDate": start_date, "endDate": end_date, "pageNo": 1, "pageSize": 200}
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    if sup_uuids:
        params["supplierUuidList"] = sup_uuids
    by_sup = defaultdict(lambda: {"purchase": 0.0, "stock_in": 0.0, "diff": 0.0, "scrap": 0.0, "cnt": 0})
    by_cat = defaultdict(lambda: {"purchase": 0.0, "stock_in": 0.0, "diff": 0.0, "cnt": 0})
    by_wh = defaultdict(lambda: {"purchase": 0.0, "stock_in": 0.0, "diff": 0.0, "cnt": 0})
    acc_status = defaultdict(lambda: {"cnt": 0, "purchase": 0.0})
    tot_purchase = tot_stock_in = tot_diff = tot_scrap = 0.0
    tot_diff_qty = tot_scrap_qty = 0.0
    fulfillment = {"notSorting": 0, "notDelivery": 0, "notStockIn": 0, "stockIned": 0}

    def _on(data, rows):
        nonlocal tot_purchase, tot_stock_in, tot_diff, tot_scrap, tot_diff_qty, tot_scrap_qty
        if not by_sup:  # 仅首页读取顶层履约状态
            fulfillment["notSorting"] = data.get("notSorting") or 0
            fulfillment["notDelivery"] = data.get("notDelivery") or 0
            fulfillment["notStockIn"] = data.get("notStockIn") or 0
            fulfillment["stockIned"] = data.get("stockIned") or 0
        for r in rows:
            pur = _num(r.get("subtotal"))
            sin = _num(r.get("stockInSubtotal"))
            dif = _num(r.get("diffSubtotal"))
            scr = _num(r.get("scrapSubtotal"))
            tot_purchase += pur; tot_stock_in += sin; tot_diff += dif; tot_scrap += scr
            tot_diff_qty += _num(r.get("diffQty"))
            tot_scrap_qty += _num(r.get("scrapQty"))
            sn = r.get("supplierName") or "未知供应商"
            s = by_sup[sn]; s["purchase"] += pur; s["stock_in"] += sin; s["diff"] += dif; s["scrap"] += scr; s["cnt"] += 1
            cn = r.get("firstCategoryName") or "未分类"
            c = by_cat[cn]; c["purchase"] += pur; c["stock_in"] += sin; c["diff"] += dif; c["cnt"] += 1
            wn = r.get("warehouseName") or "未知仓库"
            w = by_wh[wn]; w["purchase"] += pur; w["stock_in"] += sin; w["diff"] += dif; w["cnt"] += 1
            st = r.get("status") or "未知"
            a = acc_status[st]; a["cnt"] += 1; a["purchase"] += pur

    _iter_pages(client, client.page_delivery_details_stat, params, _on, max_records=config.MAX_RECORDS)
    sup_items = sorted(({"name": k, "purchase_amount": round(v["purchase"], 2),
                         "stock_in_amount": round(v["stock_in"], 2),
                         "diff_amount": round(v["diff"], 2), "cnt": v["cnt"]}
                        for k, v in by_sup.items()), key=lambda x: x["purchase_amount"], reverse=True)
    cat_items = sorted(({"name": k, "purchase_amount": round(v["purchase"], 2),
                         "stock_in_amount": round(v["stock_in"], 2), "diff_amount": round(v["diff"], 2)}
                        for k, v in by_cat.items()), key=lambda x: x["purchase_amount"], reverse=True)
    wh_items = sorted(({"name": k, "purchase_amount": round(v["purchase"], 2),
                        "stock_in_amount": round(v["stock_in"], 2), "diff_amount": round(v["diff"], 2)}
                       for k, v in by_wh.items()), key=lambda x: x["purchase_amount"], reverse=True)
    return {
        "tool": "delivery_fulfillment",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "fulfillment": fulfillment,
        "total_purchase_amount": round(tot_purchase, 2),
        "total_stock_in_amount": round(tot_stock_in, 2),
        "total_diff_amount": round(tot_diff, 2),
        "total_diff_qty": round(tot_diff_qty, 2),
        "total_scrap_amount": round(tot_scrap, 2),
        "total_scrap_qty": round(tot_scrap_qty, 2),
        "by_supplier_top": sup_items[:top_n],
        "by_category_top": cat_items[:top_n],
        "by_warehouse": wh_items[:top_n],
        "acceptance_status": {k: {"cnt": v["cnt"], "purchase_amount": round(v["purchase"], 2)}
                              for k, v in acc_status.items()},
        "note": "履约状态(notSorting待分拣/notDelivery待发货/notStockIn待验收/stockIned已验收)来自接口顶层统计；"
                "金额=明细字段合计：采购金额(subtotal)/入库金额(stockInSubtotal)/差异金额(diffSubtotal)/"
                "报废金额(scrapSubtotal)，非估算；验收差异反映「采购 vs 实际入库」偏差。",
    }


# ---------------------------------------------------------------------------
# 工具 13：成本利润（收入/支出/利润）
# ---------------------------------------------------------------------------
def _profit_one(client, date_, date_type, type_):
    """调一次 profitChartStat（type_=1收入/2支出），返回该类型的数据。"""
    d = client.profit_chart_stat({"date": date_, "dateType": date_type, "type": type_})
    if not d.get("success"):
        raise RuntimeError(f"接口返回失败: {d.get('message')}")
    data = d.get("data") or {}
    return {
        "total_amount": round(_num(data.get("totalAmount")), 2),
        "avg_amount": round(_num(data.get("avgAmount")), 2),
        "bar": {k: round(_num(v.get("totalAmount")), 2)
                for k, v in (data.get("barChartMap") or {}).items()},
        "pie": [{"item_name": p.get("itemName"), "bill_amount": round(_num(p.get("billAmount")), 2),
                 "prop": round(_num(p.get("prop")), 4)}
                for p in (data.get("pieChartMap") or {}).values()],
        "rankings": [{"item_name": p.get("itemName"), "bill_amount": round(_num(p.get("billAmount")), 2),
                     "prop": round(_num(p.get("prop")), 4)}
                    for p in (data.get("rankingsList") or [])],
    }


def cost_profit(client, date_=None, date_type=2, metric="profit"):
    """成本利润：查询某周期（date + dateType）的收入/支出/利润。

    dateType：1=按周 2=按月 3=按年；metric：income 收入 / expense 支出 / profit 利润（默认，会同时查收入与支出并算利润）。
    金额准确（接口返回）；利润 = 收入总额 − 支出总额。无仓库过滤（成本利润为组织级口径）。
    """
    if not date_:
        date_ = date.today().strftime("%Y-%m-%d")
    types = []
    if metric in ("income", "profit"):
        types.append(1)
    if metric in ("expense", "profit"):
        types.append(2)
    inc = exp = None
    if 1 in types:
        inc = _profit_one(client, date_, date_type, 1)
    if 2 in types:
        exp = _profit_one(client, date_, date_type, 2)
    profit = None
    if inc and exp:
        profit = round(inc["total_amount"] - exp["total_amount"], 2)
    return {
        "tool": "cost_profit",
        "filters": {"date": date_, "date_type": date_type, "metric": metric},
        "income": inc,
        "expense": exp,
        "profit": profit,
        "note": "收入=type1、支出=type2，均来自 profitChartStat 真实返回；利润=收入总额−支出总额；"
                "dateType 1周/2月/3年；date 为周期代表日（默认今天）。",
    }


# ---------------------------------------------------------------------------
# 工具 14：退货统计
# ---------------------------------------------------------------------------
def purchase_return(client, start_date, end_date, warehouse_name=None, supplier_name=None, top_n=10):
    """退货统计：区间退货单的应退/实退金额、笔数，按供应商/分类排行，按退货类型(正常/冲销)与财务状态拆分。

    接口 purchaseReturnBill/page 的 records 含 totalAmount(应退总金额)/hasReturnTotalAmount(实退总金额)/
    hasReturnQty/returnType(0正常1冲销)/status/finStatus(财务状态)/supplierName/warehouseName，
    isDetails=true 时 details 含 firstCategoryName/subtotal 用于分类聚合。金额准确（非估算）。
    支持 warehouse_name（warehouseUuid 单仓）、supplier_name 过滤。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    sup_uuids = _resolve_supplier_uuids(client, supplier_name)
    params = {"beginDate": start_date, "endDate": end_date, "pageNo": 1, "pageSize": 200, "isDetails": True}
    if wh_filtered:
        params["warehouseUuid"] = wh_uuids[0]  # 退货接口仅支持单仓库
    if sup_uuids:
        params["supplierUuid"] = sup_uuids[0]
    by_sup = defaultdict(lambda: {"return_amount": 0.0, "actual_return_amount": 0.0, "bills": 0})
    by_cat = defaultdict(lambda: {"return_amount": 0.0, "qty": 0.0, "bills": 0})
    by_type = defaultdict(lambda: {"bills": 0, "return_amount": 0.0, "actual_return_amount": 0.0})
    fin_status = defaultdict(lambda: {"bills": 0, "return_amount": 0.0})
    tot_bills = tot_return = tot_actual = tot_qty = 0

    def _on(data, rows):
        nonlocal tot_bills, tot_return, tot_actual, tot_qty
        for r in rows:
            amt = _num(r.get("totalAmount"))
            act = _num(r.get("hasReturnTotalAmount"))
            qty = _num(r.get("hasReturnQty"))
            tot_bills += 1; tot_return += amt; tot_actual += act; tot_qty += qty
            sn = r.get("supplierName") or "未知供应商"
            s = by_sup[sn]; s["return_amount"] += amt; s["actual_return_amount"] += act; s["bills"] += 1
            rt = "冲销退货" if str(r.get("returnType")) == "1" else "正常退货"
            t = by_type[rt]; t["bills"] += 1; t["return_amount"] += amt; t["actual_return_amount"] += act
            fs = r.get("finStatus")
            fs_label = {0: "待对账", 5: "对账中", 10: "已对账", 15: "待支付", 20: "已支付"}.get(fs, f"状态{fs}")
            f = fin_status[fs_label]; f["bills"] += 1; f["return_amount"] += amt
            for dline in (r.get("details") or []):
                cn = dline.get("firstCategoryName") or "未分类"
                c = by_cat[cn]; c["return_amount"] += _num(dline.get("subtotal")); c["qty"] += _num(dline.get("returnQty")); c["bills"] += 1

    _iter_pages(client, client.page_purchase_return, params, _on, max_records=config.MAX_RECORDS)
    sup_items = sorted(({"name": k, "return_amount": round(v["return_amount"], 2),
                         "actual_return_amount": round(v["actual_return_amount"], 2), "bills": v["bills"]}
                        for k, v in by_sup.items()), key=lambda x: x["return_amount"], reverse=True)
    cat_items = sorted(({"name": k, "return_amount": round(v["return_amount"], 2), "qty": round(v["qty"], 2),
                        "bills": v["bills"]} for k, v in by_cat.items()),
                       key=lambda x: x["return_amount"], reverse=True)
    return {
        "tool": "purchase_return",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "total_bills": tot_bills,
        "total_return_amount": round(tot_return, 2),
        "total_actual_return_amount": round(tot_actual, 2),
        "total_return_qty": round(tot_qty, 2),
        "by_supplier_top": sup_items[:top_n],
        "by_category_top": cat_items[:top_n],
        "by_return_type": {k: {"bills": v["bills"], "return_amount": round(v["return_amount"], 2),
                            "actual_return_amount": round(v["actual_return_amount"], 2)}
                           for k, v in by_type.items()},
        "fin_status": {k: {"bills": v["bills"], "return_amount": round(v["return_amount"], 2)}
                       for k, v in fin_status.items()},
        "note": "应退总金额=totalAmount 合计；实退总金额=hasReturnTotalAmount 合计；分类来自明细 firstCategoryName；"
                "returnType 0正常/1冲销；finStatus 财务状态 0待对账/5对账中/10已对账/15待支付/20已支付；金额准确非估算。",
    }


# ---------------------------------------------------------------------------
# 工具 15：领料出库统计
# ---------------------------------------------------------------------------
def picking_out(client, start_date, end_date, warehouse_name=None, dest_type=None, status=None, top_n=10):
    """领料出库统计：区间领料单的计划/实际出库金额、数量，按仓库/去向类型/状态拆分与排行。

    接口 pickingBill/page 的 records 含 totalAmount(计划出库总金额)/actualTotalAmount(实际出库总金额)/
    totalQty/outQty/destType(0组织1员工2指定仓库)/status/warehouseName。金额准确（非估算）。
    支持 warehouse_name（warehouseUuid 单仓）、dest_type（去向类型）、status 过滤。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"beginDate": start_date, "endDate": end_date, "pageNo": 1, "pageSize": 200}
    if wh_filtered:
        params["warehouseUuid"] = wh_uuids[0]  # 领料接口仅支持单仓库
    if dest_type is not None:
        params["destType"] = dest_type
    if status:
        params["status"] = status
    by_wh = defaultdict(lambda: {"bills": 0, "planned_amount": 0.0, "actual_out_amount": 0.0})
    by_dest = defaultdict(lambda: {"bills": 0, "actual_out_amount": 0.0})
    by_status = defaultdict(lambda: {"bills": 0, "actual_out_amount": 0.0})
    tot_bills = tot_planned = tot_actual = tot_planned_qty = tot_actual_qty = 0
    completed = 0

    def _on(data, rows):
        nonlocal tot_bills, tot_planned, tot_actual, tot_planned_qty, tot_actual_qty, completed
        for r in rows:
            pa = _num(r.get("totalAmount")); aa = _num(r.get("actualTotalAmount"))
            pq = _num(r.get("totalQty")); aq = _num(r.get("outQty"))
            tot_bills += 1; tot_planned += pa; tot_actual += aa; tot_planned_qty += pq; tot_actual_qty += aq
            st = r.get("status")
            if st in ("stockOuted", "completed"):
                completed += 1
            wn = r.get("warehouseName") or "未知仓库"
            w = by_wh[wn]; w["bills"] += 1; w["planned_amount"] += pa; w["actual_out_amount"] += aa
            dt = r.get("destType")
            dt_label = {0: "组织", 1: "员工", 2: "指定仓库"}.get(dt, f"类型{dt}")
            dn = r.get("destName") or ""
            dkey = f"{dt_label}" + (f"·{dn}" if dn else "")
            d = by_dest[dkey]; d["bills"] += 1; d["actual_out_amount"] += aa
            s = by_status[st or "未知"]; s["bills"] += 1; s["actual_out_amount"] += aa

    _iter_pages(client, client.page_picking_bill, params, _on, max_records=config.MAX_RECORDS)
    wh_items = sorted(({"name": k, "bills": v["bills"], "planned_amount": round(v["planned_amount"], 2),
                        "actual_out_amount": round(v["actual_out_amount"], 2)}
                       for k, v in by_wh.items()), key=lambda x: x["actual_out_amount"], reverse=True)
    dest_items = sorted(({"name": k, "bills": v["bills"], "actual_out_amount": round(v["actual_out_amount"], 2)}
                        for k, v in by_dest.items()), key=lambda x: x["actual_out_amount"], reverse=True)
    return {
        "tool": "picking_out",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "dest_type": dest_type, "status": status},
        "total_bills": tot_bills,
        "total_planned_amount": round(tot_planned, 2),
        "total_actual_out_amount": round(tot_actual, 2),
        "total_planned_qty": round(tot_planned_qty, 2),
        "total_actual_out_qty": round(tot_actual_qty, 2),
        "completed_bills": completed,
        "by_warehouse": wh_items[:top_n],
        "by_dest_type": dest_items[:top_n],
        "by_status": {k: {"bills": v["bills"], "actual_out_amount": round(v["actual_out_amount"], 2)}
                      for k, v in by_status.items()},
        "note": "实际出库金额=actualTotalAmount 合计；计划金额=totalAmount 合计；出库数量=outQty 合计；"
                "destType 0组织/1员工/2指定仓库；status: draft草稿/initial待审批/approved审批通过/"
                "reject已驳回/stockOuted已出库/completed已完成；金额准确非估算。",
    }


# ---------------------------------------------------------------------------
# 工具 16：申购验收状态统计
# ---------------------------------------------------------------------------
def requisition_status(client, start_date, end_date, warehouse_name=None, supplier_name=None):
    """申购验收状态统计：申购明细按状态(已采购/待采购/已驳回)数量，以及申购单总金额与单据数（按仓库/供应商）。

    接口 applyBill/countLineByStatus 返回 hasPurchaseQty(已采购)/notPurchaseQty(待采购)/rejectedQty(已驳回)；
    applyBill/page 返回申购单(含 totalAmount 总金额、status、warehouseName、supplierName、品项数)。
    支持 warehouse_name（warehouseUuid 单仓）、supplier_name 过滤。
    口径：申购 → 转采购(已采购) → 驳回(已驳回) 的全链路状态。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    sup_uuids = _resolve_supplier_uuids(client, supplier_name)
    base = {"beginDate": start_date, "endDate": end_date, "pageNo": 1, "pageSize": 200}
    if wh_filtered:
        base["warehouseUuid"] = wh_uuids[0]
    if sup_uuids:
        base["supplierUuid"] = sup_uuids[0]
    # 1) 明细状态计数
    st_params = dict(base)
    try:
        sd = client.page_apply_bill_count_status(st_params)
        if not sd.get("success"):
            raise RuntimeError(sd.get("message"))
        sdata = sd.get("data") or {}
    except Exception as e:
        sdata = {}
        print(f"[requisition] countLineByStatus 失败: {e}", file=sys.stderr, flush=True)
    line_has = _num(sdata.get("hasPurchaseQty"))
    line_not = _num(sdata.get("notPurchaseQty"))
    line_rej = _num(sdata.get("rejectedQty"))
    # 2) 申购单分页聚合
    by_wh = defaultdict(lambda: {"bills": 0, "amount": 0.0})
    by_sup = defaultdict(lambda: {"bills": 0, "amount": 0.0})
    tot_bills = tot_amount = 0

    def _on(data, rows):
        nonlocal tot_bills, tot_amount
        for r in rows:
            amt = _num(r.get("totalAmount"))
            tot_bills += 1; tot_amount += amt
            wn = r.get("warehouseName") or "未知仓库"
            w = by_wh[wn]; w["bills"] += 1; w["amount"] += amt
            sn = r.get("supplierName") or "未知供应商"
            s = by_sup[sn]; s["bills"] += 1; s["amount"] += amt

    _iter_pages(client, client.page_apply_bill, dict(base), _on, max_records=config.MAX_RECORDS)
    wh_items = sorted(({"name": k, "bills": v["bills"], "amount": round(v["amount"], 2)}
                      for k, v in by_wh.items()), key=lambda x: x["amount"], reverse=True)
    sup_items = sorted(({"name": k, "bills": v["bills"], "amount": round(v["amount"], 2)}
                       for k, v in by_sup.items()), key=lambda x: x["amount"], reverse=True)
    return {
        "tool": "requisition_status",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "line_has_purchase_qty": int(line_has),
        "line_not_purchase_qty": int(line_not),
        "line_rejected_qty": int(line_rej),
        "total_bills": tot_bills,
        "total_apply_amount": round(tot_amount, 2),
        "by_warehouse": wh_items[:10],
        "by_supplier_top": sup_items[:10],
        "note": "申购明细按状态：已采购(hasPurchaseQty)/待采购(notPurchaseQty)/已驳回(rejectedQty)（来自 countLineByStatus）；"
                "申购单总金额(totalAmount)与单据数来自分页查询；口径=申购→转采购(已采购)→驳回 全链路。",
    }


# ---------------------------------------------------------------------------
# 工具 17：健康证合规预警（证照合规）
# ---------------------------------------------------------------------------
def health_certificate(client, warehouse_name=None, status=None):
    """健康证合规预警：返回健康证状态分布（正常/即将到期/已过期/已停用），以及临期/过期明细清单。

    接口 pageAndStat 顶层直接返回 normalQty/aboutToExpireQty/overdueQty/disableQty 四个计数；
    records 为健康证明细（含 fullName 姓名/post 岗位/dueDate 到期日/status/warehouseNames 供应仓库）。
    status 可由服务端过滤（0禁用 1启用 2即将到期 3已到期），不传则查全部。无金额字段，纯计数。
    支持 warehouse_name（warehouseUuid/warehouseUuidList 服务端过滤）。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"pageNo": 1, "pageSize": 200}
    if wh_filtered:
        if len(wh_uuids) == 1:
            params["warehouseUuid"] = wh_uuids[0]
        else:
            params["warehouseUuidList"] = wh_uuids
    if status is not None:
        params["status"] = status
    dist = {"normalQty": 0, "aboutToExpireQty": 0, "overdueQty": 0, "disableQty": 0}
    expiring = []  # 即将到期
    expired = []   # 已过期

    def _on(data, rows):
        if "normalQty" not in _on.seen:
            _on.seen.add("normalQty")
            dist["normalQty"] = data.get("normalQty") or 0
            dist["aboutToExpireQty"] = data.get("aboutToExpireQty") or 0
            dist["overdueQty"] = data.get("overdueQty") or 0
            dist["disableQty"] = data.get("disableQty") or 0
        for r in rows:
            due = r.get("dueDate") or ""
            st = r.get("status")
            item = {"full_name": r.get("fullName") or "未知", "post": r.get("post") or "",
                    "hc_no": r.get("hcNo") or "", "due_date": due,
                    "warehouse_names": r.get("warehouseNames") or "",
                    "status": st, "status_text": r.get("statusText") or ""}
            if st == 2 or st == "2":
                expiring.append(item)
            elif st == 3 or st == "3":
                expired.append(item)
    _on.seen = set()
    _iter_pages(client, client.page_health_certificate_stat, params, _on, max_records=config.MAX_RECORDS)
    expiring.sort(key=lambda x: x["due_date"] or "9999")
    expired.sort(key=lambda x: x["due_date"] or "9999")
    total = dist["normalQty"] + dist["aboutToExpireQty"] + dist["overdueQty"] + dist["disableQty"]
    return {
        "tool": "health_certificate",
        "filters": {"warehouse_name": warehouse_name, "status": status},
        "distribution": dist,
        "total": total,
        "expiring_soon": expiring[:30],
        "expired": expired[:30],
        "note": "状态分布来自接口顶层统计（normalQty 正常/aboutToExpireQty 即将到期/overdueQty 已过期/"
                "disableQty 已停用）；明细按 dueDate 排序，仅展示临期(即将到期)与过期两类。无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 18：食安巡检（日管控 / 周排查 / 月调度，type 切换接口）
# ---------------------------------------------------------------------------
_INSPECT_LABEL = {"day": "日管控", "week": "周排查", "month": "月调度"}


def food_inspect(client, inspect_type="day", start_date=None, end_date=None, warehouse_name=None):
    """食安巡检统计：按类型(day/week/month)返回巡检完成率（已审核/待审核）与不符合项统计。

    接口 inspectDay/inspectWeek/inspectMonth 的 pageAndStat 顶层返回 auditedQty(已审核)/
    initialQty(待审核)；records 含 inspectDate/status/itemNcQty(不符合项)/itemQty(检查项)/
    warehouseName/prodSituation(0正常1未生产)。完成率=已审核/(已审核+待审核)。
    支持 start_date/end_date(beginDate/endDate)、warehouse_name(warehouseUuidList)过滤。无金额。
    """
    if inspect_type not in _INSPECT_LABEL:
        inspect_type = "day"
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"pageNo": 1, "pageSize": 200}
    if start_date:
        params["beginDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    audited = initial = total_nc = total_item = nc_bills = 0
    by_wh = defaultdict(lambda: {"audited": 0, "initial": 0, "nc_qty": 0, "item_qty": 0})
    nc_details = []  # 不符合项明细（仅收集前若干，避免无限增长）

    def _on(data, rows):
        nonlocal audited, initial, total_nc, total_item, nc_bills
        if "auditedQty" not in _on.seen:
            _on.seen.add("auditedQty")
            audited += data.get("auditedQty") or 0
            initial += data.get("initialQty") or 0
        for r in rows:
            nc = _num(r.get("itemNcQty"))
            it = _num(r.get("itemQty"))
            total_nc += nc
            total_item += it
            if nc > 0:
                nc_bills += 1
            wn = r.get("warehouseName") or "未知仓库"
            w = by_wh[wn]
            # 单单的审核状态计入该仓库完成率
            if (r.get("status") == 2 or r.get("status") == "2"):
                w["audited"] += 1
            else:
                w["initial"] += 1
            w["nc_qty"] += nc
            w["item_qty"] += it
            if nc > 0 and len(nc_details) < 50:
                nc_details.append({
                    "inspect_date": r.get("inspectDate") or "",
                    "warehouse_name": wn,
                    "nc_qty": int(nc),
                    "item_qty": int(it),
                })
    _on.seen = set()
    _iter_pages(client, lambda p: client.inspect_page_stat(inspect_type, p),
                params, _on, max_records=config.MAX_RECORDS)
    total_bills = audited + initial
    completion = round(audited / total_bills * 100, 2) if total_bills else 0.0
    return {
        "tool": "food_inspect",
        "inspect_type": inspect_type,
        "inspect_type_label": _INSPECT_LABEL[inspect_type],
        "filters": {"start_date": start_date, "end_date": end_date, "warehouse_name": warehouse_name},
        "total_bills": total_bills,
        "audited_qty": audited,
        "initial_qty": initial,
        "completion_rate": completion,
        "total_nc_qty": int(total_nc),
        "total_item_qty": int(total_item),
        "nc_bills": nc_bills,
        "by_warehouse": [{"warehouse_name": k, **{kk: int(vv) for kk, vv in v.items()}}
                         for k, v in sorted(by_wh.items(), key=lambda x: x[1]["nc_qty"], reverse=True)],
        "nc_details_sample": nc_details,
        "note": f"完成率=已审核(auditedQty)/总单数；不符合项 itemNcQty>0 的单据计入 nc_bills。"
                f"巡检类型={_INSPECT_LABEL[inspect_type]}。无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 19：留样管理（按类型计数）
# ---------------------------------------------------------------------------
def sample_retention(client, start_date=None, end_date=None, warehouse_name=None):
    """留样管理：返回各状态留样数量（待存入/待取出/留样中/已取出），以及留样覆盖率口径。

    接口 sampleBill/countBy 按 type 单值计数（0待存入 1待取出 2留样中 3已取出）；逐类型调用，
    累加得到四态计数。支持 beginDate/endDate、warehouseUuid 过滤。无金额。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    base = {}
    if start_date:
        base["beginDate"] = start_date
    if end_date:
        base["endDate"] = end_date
    if wh_filtered:
        base["warehouseUuid"] = wh_uuids[0] if len(wh_uuids) == 1 else wh_uuids
    counts = {}
    for t, label in [(0, "待存入"), (1, "待取出"), (2, "留样中"), (3, "已取出")]:
        p = dict(base)
        p["type"] = t
        d = client.sample_count_by(p)
        if not d.get("success"):
            raise RuntimeError(f"留样计数接口失败(type={t}): {d.get('message')}")
        counts[label] = int(_num((d.get("data") or 0)))
    active = counts.get("留样中", 0) + counts.get("已取出", 0)  # 合规留存（已存入过）
    return {
        "tool": "sample_retention",
        "filters": {"start_date": start_date, "end_date": end_date, "warehouse_name": warehouse_name},
        "counts": counts,
        "active_retained": active,
        "note": "留样状态计数来自 countBy（type 0待存入/1待取出/2留样中/3已取出）；"
                "active_retained=留样中+已取出（视为合规留存）。无金额口径；覆盖率需结合采购入库批次（二期）。",
    }


# ---------------------------------------------------------------------------
# 工具 20：晨检记录（合格/不合格/在岗）
# ---------------------------------------------------------------------------
def morning_check(client, start_date=None, end_date=None, warehouse_name=None, check_type=None, qualified=None):
    """晨检记录统计：返回晨检合格/不合格数量、在岗数量，以及不合格原因分布。

    接口 morningCheck/pageAndStat 顶层返回 qualifiedYesQty(合格)/qualifiedNoQty(不合格)/
    totalQty(在岗)；records 含 checkTime/qualified(0不合格1合格)/temperature/sick(不合格原因)/
    type(5晨检10午检15晚检)/post/warehouseName。支持起止日期、仓库、type、qualified 过滤。无金额。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"pageNo": 1, "pageSize": 200}
    if start_date:
        params["beginDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if wh_filtered:
        if len(wh_uuids) == 1:
            params["warehouseUuid"] = wh_uuids[0]
        else:
            params["warehouseUuidList"] = wh_uuids
    if check_type is not None:
        params["type"] = check_type
    if qualified is not None:
        params["qualified"] = qualified
    yes = no = total_qty = 0
    sick_dist = defaultdict(int)  # 不合格原因分布
    by_wh = defaultdict(lambda: {"yes": 0, "no": 0})
    by_type = defaultdict(lambda: {"yes": 0, "no": 0})

    def _on(data, rows):
        nonlocal yes, no, total_qty
        if "qualifiedYesQty" not in _on.seen:
            _on.seen.add("qualifiedYesQty")
            yes += data.get("qualifiedYesQty") or 0
            no += data.get("qualifiedNoQty") or 0
            total_qty += data.get("totalQty") or 0
        for r in rows:
            q = r.get("qualified")
            is_yes = (q == 1 or q == "1" or q is True)
            wn = r.get("warehouseName") or "未知仓库"
            t = r.get("type")
            tlabel = {5: "晨检", 10: "午检", 15: "晚检"}.get(t, f"type{t}")
            bw = by_wh[wn]; bt = by_type[tlabel]
            if is_yes:
                bw["yes"] += 1; bt["yes"] += 1
            else:
                bw["no"] += 1; bt["no"] += 1
                sick = r.get("sick")
                if sick is not None:
                    sick_dist[str(sick)] += 1
    _on.seen = set()
    _iter_pages(client, client.morning_check_page_stat, params, _on, max_records=config.MAX_RECORDS)
    checked = yes + no
    qualified_rate = round(yes / checked * 100, 2) if checked else 0.0
    _SICK = {0: "腹痛", 1: "腹泻", 2: "恶心", 3: "呕吐", 4: "发热", 5: "手部伤口", 6: "湿疹",
             7: "长疖子", 8: "流鼻涕", 9: "异物", 10: "创可贴", 11: "戒指", 12: "指甲油",
             13: "灰指甲", 99: "其他"}
    return {
        "tool": "morning_check",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "check_type": check_type, "qualified": qualified},
        "qualified_yes": yes,
        "qualified_no": no,
        "total_qty": total_qty,
        "checked": checked,
        "qualified_rate": qualified_rate,
        "sick_distribution": {_SICK.get(int(k), k): v for k, v in
                               sorted(sick_dist.items(), key=lambda x: x[1], reverse=True)},
        "by_warehouse": [{"warehouse_name": k, **v} for k, v in
                         sorted(by_wh.items(), key=lambda x: x[1]["no"], reverse=True)],
        "by_type": [{"check_type": k, **v} for k, v in by_type.items()],
        "note": "合格/不合格/在岗来自接口顶层统计；不合格原因 sick 编码已映射为中文；"
                "合格率为 合格/(合格+不合格)。无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 21：检测报告（合格率 / 不合格项）
# ---------------------------------------------------------------------------
def detection_report(client, start_date=None, end_date=None, warehouse_name=None, supplier_name=None):
    """检测报告统计：返回食材/环境检测合格率、不合格数量，以及按供应商/商品的不合格分布。

    接口 detection/page records 含 checkDate/qualified(布尔 是否合格)/checkItem(检测项)/
    goodsNames/supplierName/type(检测方式 0其它1自检2供应商检测3第三方)/warehouseNames。
    逐项统计合格/不合格，按供应商/商品聚合不合格数。支持起止日期、仓库、供应商过滤。无金额。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    sup_uuids = _resolve_supplier_uuids(client, supplier_name)
    params = {"pageNo": 1, "pageSize": 200}
    if start_date:
        params["beginDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    if sup_uuids:
        params["supplierUuid"] = sup_uuids[0]
    total = yes = no = 0
    by_sup_nc = defaultdict(int)
    by_goods_nc = defaultdict(int)
    nc_samples = []

    def _on(data, rows):
        nonlocal total, yes, no
        for r in rows:
            total += 1
            q = r.get("qualified")
            is_yes = (q is True or q == 1 or q == "true" or q == "1")
            if is_yes:
                yes += 1
            else:
                no += 1
                sn = r.get("supplierName") or "未知供应商"
                by_sup_nc[sn] += 1
                gn = r.get("goodsNames") or r.get("goodsName") or "未知商品"
                by_goods_nc[gn] += 1
                if len(nc_samples) < 30:
                    nc_samples.append({
                        "check_date": r.get("checkDate") or "",
                        "check_item": r.get("checkItem") or "",
                        "goods_names": gn,
                        "supplier_name": sn,
                        "warehouse_names": r.get("warehouseNames") or "",
                        "detect_type": r.get("type"),
                    })
    _iter_pages(client, client.detection_page, params, _on, max_records=config.MAX_RECORDS)
    qualified_rate = round(yes / total * 100, 2) if total else 0.0
    return {
        "tool": "detection_report",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "total": total,
        "qualified_yes": yes,
        "qualified_no": no,
        "qualified_rate": qualified_rate,
        "by_supplier_nc_top": sorted(({"supplier_name": k, "nc_qty": v}
                                      for k, v in by_sup_nc.items()),
                                     key=lambda x: x["nc_qty"], reverse=True)[:10],
        "by_goods_nc_top": sorted(({"goods_names": k, "nc_qty": v}
                                   for k, v in by_goods_nc.items()),
                                  key=lambda x: x["nc_qty"], reverse=True)[:10],
        "nc_samples": nc_samples,
        "note": "合格率=合格数/总数（qualified 布尔）；不合格项按供应商/商品聚合。检测方式 type: "
                "0其它1自检2供应商检测3第三方。无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 22：食品添加剂（使用台账 + 限量预警）
# ---------------------------------------------------------------------------
def food_additive(client, start_date=None, end_date=None, warehouse_name=None, top_n=10):
    """食品添加剂使用台账与限量预警：返回添加剂使用记录、用量超标预警。

    接口 foodAdditive/page records 含 additiveName/usagePerKg(使用量 g/kg)/standardUsagePerKg
    (标准使用量 g/kg)/flourUsageKg(面粉用量 kg)/remainingQty(剩余量 g)/status(5待审核10已审核)/
    warehouseName/functionPurpose。
    超标判定：usagePerKg > standardUsagePerKg（存在标准时）视为超标。按仓库聚合使用记录数。
    支持 beginDate/endDate、warehouseName(warehouseUuidList)过滤。无金额。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"pageNo": 1, "pageSize": 200}
    if start_date:
        params["beginDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids
    total = over_standard = 0
    by_name = defaultdict(lambda: {"cnt": 0, "usage_sum": 0.0, "standard_sum": 0.0,
                                   "over": 0, "flour_sum": 0.0})
    by_wh = defaultdict(int)
    over_samples = []

    def _on(data, rows):
        nonlocal total, over_standard
        for r in rows:
            total += 1
            name = r.get("additiveName") or "未知添加剂"
            usage = _num(r.get("usagePerKg"))
            std = _num(r.get("standardUsagePerKg"))
            flour = _num(r.get("flourUsageKg"))
            wn = r.get("warehouseName") or "未知仓库"
            s = by_name[name]
            s["cnt"] += 1
            s["usage_sum"] += usage
            s["standard_sum"] += std
            s["flour_sum"] += flour
            by_wh[wn] += 1
            over = (std > 0 and usage > std)
            if over:
                over_standard += 1
                s["over"] += 1
                if len(over_samples) < 30:
                    over_samples.append({
                        "additive_name": name,
                        "warehouse_name": wn,
                        "usage_per_kg": round(usage, 3),
                        "standard_per_kg": round(std, 3),
                        "flour_usage_kg": round(flour, 3),
                        "function_purpose": r.get("functionPurpose") or "",
                    })
    _iter_pages(client, client.food_additive_page, params, _on, max_records=config.MAX_RECORDS)
    items = sorted(({"additive_name": k, "cnt": v["cnt"],
                     "avg_usage_per_kg": round(v["usage_sum"] / v["cnt"], 3) if v["cnt"] else 0,
                     "avg_standard_per_kg": round(v["standard_sum"] / v["cnt"], 3) if v["cnt"] else 0,
                     "over_standard_cnt": v["over"],
                     "total_flour_kg": round(v["flour_sum"], 3)}
                    for k, v in by_name.items()),
                   key=lambda x: x["over_standard_cnt"], reverse=True)
    return {
        "tool": "food_additive",
        "filters": {"start_date": start_date, "end_date": end_date, "warehouse_name": warehouse_name},
        "total": total,
        "over_standard_cnt": over_standard,
        "top_n": top_n,
        "by_additive_top": items[:top_n],
        "by_warehouse": sorted(({"warehouse_name": k, "cnt": v} for k, v in by_wh.items()),
                               key=lambda x: x["cnt"], reverse=True)[:top_n],
        "over_standard_samples": over_samples,
        "note": "超标判定：usagePerKg(使用量) > standardUsagePerKg(标准使用量) 且标准>0 视为超标；"
                "按添加剂聚合使用次数/平均用量/超标次数。无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 21：综合预警中心（证照到期 / 库存过期 / 巡检不符合项 统一入口）
# ---------------------------------------------------------------------------
_CAT_LABEL = {
    "inquiry": "询比价", "pricing": "定价", "purchase": "采购", "accept": "验收",
    "dish": "排菜", "fs": "食安", "certificate": "证照", "stock": "仓储",
}
_STATUS_LABEL = {0: "待整改", 1: "已整改", 2: "已忽略", 4: "已确认"}


def warning_center(client, start_date=None, end_date=None, category=None,
                   status=None, warehouse_name=None, top_n=20):
    """综合预警中心：统一查询各类预警（证照到期 / 库存过期 / 食安巡检不符合项 / 采购验收等）。

    接口 earlyWarn/pageAndStat 顶层同时返回四态聚合：waitRectifyQty(待整改)/rectifiedQty(已整改)/
    ignoreQty(已忽略)/confirmedQty(已确认)；records 为预警明细（category 分类 / status 状态 /
    content 内容 / warehouseName / createTime / startDate / endDate / handleList 处理记录）。
    category 可选：fs 食安 / certificate 证照 / stock 仓储 / purchase 采购 / accept 验收 等；
    status 可选：0 待整改 / 1 已整改 / 2 已忽略 / 4 已确认。支持日期区间与仓库过滤。
    返回四态聚合 + 按分类分布 + 待整改/未处理明细 TOP。无金额口径。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"pageNo": 1, "pageSize": 200}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if category:
        params["category"] = category
    if status is not None:
        params["status"] = status
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids

    # 顶层四态聚合字段 → 状态枚举（pageAndStat 顶层已是全局合计，不按页累加）
    _FIELD_TO_STATUS = {"waitRectifyQty": 0, "rectifiedQty": 1, "ignoreQty": 2, "confirmedQty": 4}
    by_category = defaultdict(int)
    pending = []  # 待整改/未处理明细
    total = 0

    def _on(data, rows):
        nonlocal total
        for r in rows:
            cat = r.get("category") or "未知"
            by_category[cat] += 1
            if int(_num(r.get("status"))) in (0,):  # 待整改
                pending.append({
                    "category": _CAT_LABEL.get(cat, cat),
                    "content": (r.get("content") or "")[:60],
                    "warehouse": r.get("warehouseName") or "—",
                    "create_time": r.get("createTime") or "",
                    "end_date": r.get("endDate") or "",
                })
        total += len(rows)

    first = _iter_pages(client, client.page_early_warn_stat, params, _on,
                        max_records=config.MAX_RECORDS)
    # 四态聚合直接取自首查顶层（全局合计），避免逐页累加导致翻倍
    status_agg = {_STATUS_LABEL.get(s, s): int(_num(first.get(fld))) if first else 0
                  for fld, s in _FIELD_TO_STATUS.items()}
    cat_items = sorted(({"category": _CAT_LABEL.get(k, k), "count": v}
                        for k, v in by_category.items()),
                       key=lambda x: x["count"], reverse=True)
    pending.sort(key=lambda x: x["end_date"] or "")
    return {
        "tool": "warning_center",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "category": _CAT_LABEL.get(category, category) if category else None,
                    "status": _STATUS_LABEL.get(status, status) if status is not None else None,
                    "warehouse_name": warehouse_name},
        "total": total,
        "status_agg": status_agg,
        "by_category": cat_items,
        "pending_top": pending[:top_n],
        "note": "数据来自 earlyWarn/pageAndStat：category 覆盖 fs食安/certificate证照/stock仓储/purchase采购/"
                "accept验收；status 0待整改 1已整改 2已忽略 4已确认。四态聚合取自接口顶层全局合计；"
                "待整改(pending)明细按到期日排序；无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 22：环境设备告警指数（温度/湿度/烟雾/燃气/水浸/AI巡检）
# ---------------------------------------------------------------------------
def device_alarm_index(client, warehouse_name=None):
    """环境设备告警指数：返回各类环境告警的累计总数（温度/湿度/烟雾/燃气/水浸/AI巡检）。

    接口 thirdDeviceWarn/getTarget 返回 data 对象：tempWarnTotal 温度 / humWarnTotal 湿度 /
    smokeWarnTotal 烟雾 / gasWarnTotal 燃气 / floodWarnTotal 水浸 / aiWarnTotal AI巡检 /
    dataLineMap 设备告警。可按仓库过滤。无明细，仅做指数看板。无金额口径。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {}
    if wh_filtered and len(wh_uuids) == 1:
        params["warehouseUuid"] = wh_uuids[0]
    d = client.get_third_device_warn_target(params)
    if not d.get("success"):
        raise RuntimeError(f"环境告警指数接口失败: {d.get('message')}")
    data = d.get("data") or {}
    items = [
        {"type": "温度告警", "value": int(_num(data.get("tempWarnTotal")))},
        {"type": "湿度告警", "value": int(_num(data.get("humWarnTotal")))},
        {"type": "烟雾告警", "value": int(_num(data.get("smokeWarnTotal")))},
        {"type": "燃气告警", "value": int(_num(data.get("gasWarnTotal")))},
        {"type": "水浸告警", "value": int(_num(data.get("floodWarnTotal")))},
        {"type": "AI巡检告警", "value": int(_num(data.get("aiWarnTotal")))},
    ]
    line_map = data.get("dataLineMap") or {}
    return {
        "tool": "device_alarm_index",
        "filters": {"warehouse_name": warehouse_name},
        "items": items,
        "total_alarms": sum(i["value"] for i in items),
        "device_line_map": line_map,
        "note": "数据来自 thirdDeviceWarn/getTarget：温度/湿度/烟雾/燃气/水浸/AI巡检 各类累计告警数；"
                "device_line_map 为设备维度告警。无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 23：环境设备告警明细（消杀/环境设备告警）
# ---------------------------------------------------------------------------
def device_alarm_detail(client, start_date=None, end_date=None, status=None,
                        warehouse_name=None, app_type=None, top_n=20):
    """环境设备告警明细：查询消杀/环境相关设备告警（温度/湿度/烟雾/燃气/水浸/AI巡检等）的明细列表。

    接口 thirdDeviceWarn/page：records 为告警明细（warnType/warnTypeText 告警类型 / warnValue 告警数值 /
    warnContent 告警内容 / status 0未处理 1已处理 2已忽略 / typeText 设备类型 / warehouseName /
    warnTime 告警时间 / eviUrl 取证URL / result 结果记录）。支持日期区间、status、仓库、app_type(1物联网 2AI巡检)。
    返回状态分布 + 按类型分布 + 未处理/未忽略明细 TOP。无金额口径。
    """
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"pageNo": 1, "pageSize": 200}
    if start_date:
        params["beginDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if status is not None:
        params["status"] = status
    if wh_filtered and len(wh_uuids) == 1:
        params["warehouseUuid"] = wh_uuids[0]
    if app_type is not None:
        params["appType"] = app_type

    by_status = defaultdict(int)
    by_type = defaultdict(int)
    unresolved = []  # 未处理(0)/未忽略(0,1)
    total = 0

    def _on(data, rows):
        nonlocal total
        for r in rows:
            st = int(_num(r.get("status")))
            by_status[st] += 1
            by_type[r.get("warnTypeText") or r.get("typeText") or "未知"] += 1
            if st in (0, 1):
                unresolved.append({
                    "type_text": r.get("warnTypeText") or r.get("typeText") or "—",
                    "content": (r.get("warnContent") or "")[:60],
                    "value": r.get("warnValue") or "",
                    "warehouse": r.get("warehouseName") or "—",
                    "warn_time": r.get("warnTime") or "",
                    "status": {0: "未处理", 1: "已处理", 2: "已忽略"}.get(st, st),
                })
        total += len(rows)

    _iter_pages(client, client.page_third_device_warn, params, _on,
                max_records=config.MAX_RECORDS)
    status_items = sorted(({"status": {0: "未处理", 1: "已处理", 2: "已忽略"}.get(k, k), "count": v}
                           for k, v in by_status.items()),
                          key=lambda x: x["count"], reverse=True)
    type_items = sorted(({"type": k, "count": v} for k, v in by_type.items()),
                        key=lambda x: x["count"], reverse=True)
    unresolved.sort(key=lambda x: x["warn_time"] or "")
    return {
        "tool": "device_alarm_detail",
        "filters": {"start_date": start_date, "end_date": end_date,
                    "status": {0: "未处理", 1: "已处理", 2: "已忽略"}.get(status, status)
                    if status is not None else None,
                    "warehouse_name": warehouse_name,
                    "app_type": {1: "物联网", 2: "AI巡检"}.get(app_type, app_type)
                    if app_type is not None else None},
        "total": total,
        "by_status": status_items,
        "by_type": type_items[:10],
        "unresolved_top": unresolved[:top_n],
        "note": "数据来自 thirdDeviceWarn/page：status 0未处理 1已处理 2已忽略；warnTypeText 为告警类型名称；"
                "unresolved_top=未处理/已处理明细按告警时间排序；无金额口径。",
    }


# ---------------------------------------------------------------------------
# 工具 27：周期对比 / 趋势（问数增强）
# ---------------------------------------------------------------------------
def _parse_period(p: str):
    """把单个周期描述解析为 (start, end, kind) —— kind 用于 cost_profit 的 dateType。

    - "YYYY-MM"           -> 该自然月，kind="month"（cost_profit 用 dateType=2）
    - "YYYY"              -> 该自然年，kind="year" （cost_profit 用 dateType=3）
    - "YYYY-MM-DD~YYYY-MM-DD" -> 显式区间，kind="range"（仅 purchase_stat 用）
    返回 (start, end, kind)；无法解析抛 ValueError。
    """
    p = (p or "").strip()
    import re
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})~(\d{4})-(\d{2})-(\d{2})$", p)
    if m:
        return (f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                f"{m.group(4)}-{m.group(5)}-{m.group(6)}", "range")
    m = re.match(r"^(\d{4})-(\d{2})$", p)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = f"{y}-{mo:02d}-01"
        if mo == 12:
            end = f"{y}-12-31"
        else:
            from datetime import date as _d, timedelta as _td
            nxt = _d(y, mo + 1, 1)
            end = (nxt - _td(days=1)).strftime("%Y-%m-%d")
        return (start, end, "month")
    m = re.match(r"^(\d{4})$", p)
    if m:
        y = int(m.group(1))
        return (f"{y}-01-01", f"{y}-12-31", "year")
    raise ValueError(f"无法识别的周期格式: {p}（支持 YYYY / YYYY-MM / YYYY-MM-DD~YYYY-MM-DD）")


def period_compare(client, base_tool, periods, warehouse_name=None,
                   supplier_name=None, metric="profit", date_type=None):
    """周期对比 / 趋势（问数增强）：把同一个【准确金额工具】在多个周期上分别执行，
    串成时间序列，并计算相邻周期的环比（%）与差值。

    支持两种底层准确工具（金额均来自服务端聚合，准确非估算）：
    - base_tool="purchase_stat"：采购统计区间汇总（采购总额含越库/入库/出库/结余等）。
      periods 为区间列表（"YYYY-MM" 月 / "YYYY" 年 / 显式 "YYYY-MM-DD~YYYY-MM-DD"）。
      主对比指标 = 采购总额(含越库)。可按 warehouse_name / supplier_name 过滤。
    - base_tool="cost_profit"：成本利润（收入/支出/利润）。
      periods 为周期列表（"YYYY-MM" 月 / "YYYY" 年）；metric=income 收入 / expense 支出 / profit 利润。
      无仓库过滤（组织级口径）。

    返回：
      - series: [{period, label, start, end, values:{指标名: 值}, main_value, main_delta, main_delta_pct}]
      - 只报真实返回数字，无数据如实记 0 / None，绝不编造。
    """
    if base_tool not in ("purchase_stat", "cost_profit"):
        return {"error": f"period_compare 不支持的 base_tool: {base_tool}（仅支持 purchase_stat / cost_profit）"}
    if not periods or not isinstance(periods, list):
        return {"error": "periods 不能为空，应为周期字符串列表（如 [\"2026-05\",\"2026-06\",\"2026-07\"]）"}
    if len(periods) > 24:
        return {"error": "periods 最多 24 个周期"}

    series = []
    prev_main = None
    main_metric_label = ("采购总额(含越库)" if base_tool == "purchase_stat"
                         else {"income": "收入", "expense": "支出", "profit": "利润"}.get(metric, "利润"))
    for p in periods:
        try:
            start, end, kind = _parse_period(str(p))
        except ValueError as e:
            return {"error": str(e)}
        try:
            if base_tool == "purchase_stat":
                res = purchase_stat(client, start, end, warehouse_name, supplier_name)
                if res.get("error") or res.get("too_large"):
                    return {"error": f"周期 {p} 查询失败: {res.get('error') or '数据量过大'}"}
                values = {
                    "采购总额(含越库)": res["purchase_amount_incl_cross"],
                    "采购入库金额": res["in_amount_total"],
                    "采购越库金额": res["cross_amount_total"],
                    "出库金额": res["out_amount_total"],
                    "结余金额": res["sub_amount"],
                    "采购数量(含越库)": res["purchase_qty_incl_cross"],
                    "出库数量": res["out_qty_total"],
                }
                main_value = res["purchase_amount_incl_cross"]
                label = p
            else:  # cost_profit
                dt = date_type or (3 if kind == "year" else 2)
                res = cost_profit(client, date_=start, date_type=dt, metric=metric)
                if res.get("error"):
                    return {"error": f"周期 {p} 查询失败: {res.get('error')}"}
                if metric == "profit":
                    val = res.get("profit")
                elif metric == "income":
                    val = (res.get("income") or {}).get("total_amount")
                else:
                    val = (res.get("expense") or {}).get("total_amount")
                values = {"收入": (res.get("income") or {}).get("total_amount"),
                          "支出": (res.get("expense") or {}).get("total_amount"),
                          "利润": res.get("profit")}
                main_value = val
                label = p
        except Exception as e:  # noqa
            return {"error": f"周期 {p} 调用底层工具异常: {e}"}

        if prev_main not in (None, 0) and main_value is not None:
            d = round(main_value - prev_main, 2)
            dp = round(d / prev_main * 100, 2)
        elif prev_main == 0 and main_value:
            d = round(main_value - prev_main, 2)
            dp = None  # 基期为 0，环比无意义
        else:
            d, dp = None, None
        prev_main = main_value
        series.append({
            "period": str(p),
            "label": label,
            "start": start,
            "end": end,
            "values": {k: (round(v, 2) if isinstance(v, (int, float)) else v)
                       for k, v in values.items()},
            "main_metric": main_metric_label,
            "main_value": main_value,
            "main_delta": d,
            "main_delta_pct": dp,
        })

    n = len(series)
    rising = sum(1 for s in series if s["main_delta"] is not None and s["main_delta"] > 0)
    falling = sum(1 for s in series if s["main_delta"] is not None and s["main_delta"] < 0)
    return {
        "tool": "period_compare",
        "base_tool": base_tool,
        "metric": metric,
        "filters": {"warehouse_name": warehouse_name, "supplier_name": supplier_name},
        "main_metric": main_metric_label,
        "series": series,
        "period_count": n,
        "summary": {
            "first_period": series[0]["period"] if n else None,
            "last_period": series[-1]["period"] if n else None,
            "first_value": series[0]["main_value"] if n else None,
            "last_value": series[-1]["main_value"] if n else None,
            "rising_count": rising,
            "falling_count": falling,
        },
        "note": ("周期对比基于服务端聚合的准确金额（非估算）。"
                 "环比 = (本期 − 上一期) / 上一期 × 100%；基期为 0 时环比标记为 null。"
                 f"底层工具：{base_tool}。"),
    }


# ---------------------------------------------------------------------------
# Phase 5：报表体系（食堂管理者视角）—— 驾驶舱 / 采购价对比 / 库存月报 / 食安预警
# ---------------------------------------------------------------------------
def _default_report_month():
    """返回上一完整自然月首日 yyyy-MM-dd（月度报表默认取最近一个完整月）。"""
    t = date.today().replace(day=1)
    last_month = t - timedelta(days=1)
    return last_month.replace(day=1).strftime("%Y-%m-%d")


def dashboard_overview(client, warehouse_name=None):
    """经营驾驶舱总览：一站汇总「今日关键指标 + 待处理单据 + 食安概况」，是管理者每天进系统的第一眼。

    - 进销存侧（wms/reportStat/getIndex）：今日采购金额(purchaseAmount) / 今日验收金额(stockInAmount) /
      今日留样项数(sampleCount)，每项含 dayRatio(日同比%) / todayValue / yesterdayValue。
    - 待处理单据（wms/reportStat/getWaitProcessedReport，默认本月）：调整单/申购/采购/退货数量与金额。
    - 食安侧（data/reportStat/getIndex）：今日晨检人数(morningCheckCount，含日同比)。
    - 食安概况（data/reportStat/getOverviewData）：各模块执行概况清单 {name, status}。
    无金额估算；指标为接口直接返回。返回今日指标卡 + 待处理汇总 + 食安概况。
    """
    sd, ed = _default_month_range()
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)

    wms_idx = client.wms_report_index({}) or {}
    wms_idx_data = (wms_idx.get("data") or {}) if wms_idx.get("success") else {}

    wp = {"beginDate": sd, "endDate": ed}
    wms_wait = client.wms_report_wait_processed(wp) or {}
    wms_wait_data = (wms_wait.get("data") or {}) if wms_wait.get("success") else {}

    dfs_idx = client.data_report_index({}) or {}
    dfs_idx_data = (dfs_idx.get("data") or {}) if dfs_idx.get("success") else {}

    ovp = {}
    if wh_filtered:
        ovp["warehouseUuid"] = wh_uuids[0] if isinstance(wh_uuids, list) else wh_uuids
    dfs_ov = client.data_report_overview(ovp) or {}
    dfs_ov_data = (dfs_ov.get("data") or []) if dfs_ov.get("success") else []

    def _idx(d, key):
        v = d.get(key) or {}
        return {"today": _num(v.get("todayValue")), "yesterday": _num(v.get("yesterdayValue")),
                "day_ratio": _num(v.get("dayRatio"))}

    today_metrics = {
        "purchase_amount": _idx(wms_idx_data, "purchaseAmount"),
        "stock_in_amount": _idx(wms_idx_data, "stockInAmount"),
        "sample_count": _idx(wms_idx_data, "sampleCount"),
        "morning_check": _idx(dfs_idx_data, "morningCheckCount"),
    }
    wait = {
        "adjust_bill_count": _num(wms_wait_data.get("adjustBillCount")),
        "apply_count": _num(wms_wait_data.get("applyCount")),
        "apply_total_amount": _num(wms_wait_data.get("applyTotalAmount")),
        "flow_bill_count": _num(wms_wait_data.get("flowBillCount")),
        "pur_count": _num(wms_wait_data.get("purCount")),
        "pur_return_count": _num(wms_wait_data.get("purReturnCount")),
        "pur_return_total_amount": _num(wms_wait_data.get("purReturnTotalAmount")),
        "pur_total_amount": _num(wms_wait_data.get("purTotalAmount")),
    }
    overview = [{"name": o.get("name"), "status": o.get("status")} for o in dfs_ov_data]
    return {
        "tool": "dashboard_overview",
        "filters": {"warehouse_name": warehouse_name, "month_range": [sd, ed]},
        "today_metrics": today_metrics,
        "wait_processed": wait,
        "fs_overview": overview,
        "note": "数据来自 wms/reportStat/getIndex + getWaitProcessedReport（本月）+ data/reportStat/getIndex + getOverviewData；"
                "指标为接口直接返回，无金额估算。日同比 day_ratio 单位为%。",
    }


def purchase_price_compare(client, start_date=None, end_date=None, warehouse_name=None, top_n=20):
    """采购价对比（食堂成本把控核心）：逐笔对比采购单价与平台参考价，标出超价比例，找出买贵了的单子。

    接口 wms/reportStat/pagePurPriceCompare：goodsName 商品 / goodsSpec 规格 / unit 单位 / warehouseName 仓库 /
    supplierName 供应商 / price 采购单价 / highPrice 平台价格 / outOfProp 超出比例(%) /
    hasStockInQty 入库数量 / deliveryTime 发货时间 / dataSource 数据来源(如北京新发地)。
    默认取当前自然月；支持仓库过滤。返回超价统计 + 明细 TOP（按超出比例降序）。
    金额口径：price×hasStockInQty 为采购额估算（接口无金额字段），回答须注明「估算」。
    """
    sd, ed = (start_date, end_date) if start_date else _default_month_range()
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"beginDate": sd, "endDate": ed, "pageNo": 1, "pageSize": 200, "orderBy": "outOfProp_desc"}
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids

    rows = []
    over_cnt = 0
    over_amount_est = 0.0

    def _on(data, recs):
        nonlocal over_cnt, over_amount_est
        for r in recs:
            price = _num(r.get("price"))
            oop = _num(r.get("outOfProp"))
            qty = _num(r.get("hasStockInQty"))
            rows.append({
                "goods": r.get("goodsName") or "",
                "spec": r.get("goodsSpec") or "",
                "unit": r.get("unit") or "",
                "warehouse": r.get("warehouseName") or "",
                "supplier": r.get("supplierName") or "",
                "price": price,
                "high_price": _num(r.get("highPrice")),
                "out_of_prop": oop,
                "qty": qty,
                "delivery_time": r.get("deliveryTime") or "",
            })
            if oop > 0:
                over_cnt += 1
                over_amount_est += price * qty

    first = _iter_pages(client, client.page_pur_price_compare, params, _on,
                        max_records=config.MAX_RECORDS)
    rows.sort(key=lambda x: x["out_of_prop"], reverse=True)
    return {
        "tool": "purchase_price_compare",
        "filters": {"start_date": sd, "end_date": ed, "warehouse_name": warehouse_name},
        "total": (first.get("total") if first else len(rows)),
        "over_count": over_cnt,
        "over_amount_est": over_amount_est,
        "rows": rows[:max(int(top_n), 200)],
        "note": "数据来自 wms/reportStat/pagePurPriceCompare；price×hasStockInQty 为采购额估算（接口无金额字段）。"
                "outOfProp 为超出平台价比例(%)；已按超出比例降序，TOP 取超价最严重项。",
    }


def stock_month_report(client, report_date=None, warehouse_name=None, top_n=20):
    """库存月报（进销存月度经营复盘）：按商品汇总当月进销存金额/数量（服务端已聚合，金额准确）。

    接口 wms/reportStat/pageStockMonthReport 需 reportDate（月报日期，取当月首日 yyyy-MM-dd）。
    默认取上一完整月（_default_report_month）。records：goodsName/spec/unit/warehouseName/firstCategoryName +
    purchaseInAmount/Qty 采购入库、purchaseCrossInAmount/Qty 采购越库、pickingOutAmount/Qty 领料出库、
    stockInAmount/Qty 入库、stockOutAmount/Qty 出库、stockAmount 期末金额、stockQty 期末数量、
    beginStockAmount/Qty 期初。
    返回月度汇总（期初/入库/出库/期末金额与数量）+ 商品明细 TOP（按期末金额降序）。无金额估算。
    """
    report_date = report_date or _default_report_month()
    _wh_err = _require_warehouse(warehouse_name, "stock_month_report")
    if _wh_err:
        return _wh_err
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    params = {"reportDate": report_date, "pageNo": 1, "pageSize": 2000}
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids

    rows = []
    sums = defaultdict(float)

    def _on(data, recs):
        for r in recs:
            rows.append({
                "goods": r.get("goodsName") or "",
                "spec": r.get("spec") or "",
                "unit": r.get("unit") or "",
                "category": r.get("firstCategoryName") or "",
                "warehouse": r.get("warehouseName") or "",
                "begin_amount": _num(r.get("beginStockAmount")),
                "purchase_in_amount": _num(r.get("purchaseInAmount")),
                "purchase_cross_in_amount": _num(r.get("purchaseCrossInAmount")),
                "picking_out_amount": _num(r.get("pickingOutAmount")),
                "stock_in_amount": _num(r.get("stockInAmount")),
                "stock_out_amount": _num(r.get("stockOutAmount")),
                "stock_amount": _num(r.get("stockAmount")),
                "stock_qty": _num(r.get("stockQty")),
            })
            for k in ("beginStockAmount", "purchaseInAmount", "purchaseCrossInAmount",
                      "pickingOutAmount", "stockInAmount", "stockOutAmount", "stockAmount", "stockQty"):
                sums[k] += _num(r.get(k))
        # 内存保护：月度全量可达 7 万+ 条，只保留期末金额最高的 top 300，避免全量堆积导致 OOM；
        # 月度汇总金额（sums）已全量精确累加，不受此裁剪影响。
        if len(rows) > 300:
            rows.sort(key=lambda x: x["stock_amount"], reverse=True)
            del rows[300:]

    # 该报表单月全量可达 7 万+ 条，放宽分页上限（配合上面的 top300 裁剪，内存不随总量膨胀）。
    first = _iter_pages(client, client.page_stock_month_report, params, _on,
                        max_records=max(200000, config.MAX_RECORDS))
    rows.sort(key=lambda x: x["stock_amount"], reverse=True)
    return {
        "tool": "stock_month_report",
        "filters": {"report_date": report_date, "warehouse_name": warehouse_name},
        "total": (first.get("total") if first else len(rows)),
        "summary": {
            "begin_amount": sums["beginStockAmount"],
            "purchase_in_amount": sums["purchaseInAmount"],
            "purchase_cross_in_amount": sums["purchaseCrossInAmount"],
            "picking_out_amount": sums["pickingOutAmount"],
            "stock_in_amount": sums["stockInAmount"],
            "stock_out_amount": sums["stockOutAmount"],
            "stock_amount": sums["stockAmount"],
            "stock_qty": sums["stockQty"],
        },
        "rows": rows[:max(int(top_n), 200)],
        "note": "数据来自 wms/reportStat/pageStockMonthReport，金额由服务端按月聚合，准确非估算；"
                "report_date 默认取上一完整月首日。期末金额=stockAmount，期末数量=stockQty。",
    }


def food_safety_alert(client, start_date=None, end_date=None, category=None,
                      warehouse_name=None, status=None, top_n=20):
    """预警中心总览（全分类）：统一查看各类预警（食安 fs / 证照 certificate / 定价 pricing / 采购 purchase /
    验收 accept / 排菜 dish / 仓储 stock / 询比价 inquiry）的待整改/已整改/已忽略/已确认四态，并列出待整改明细 TOP。

    复用 data/earlyWarn/pageAndStat。category 默认 None（查**全部分类**，对齐生产系统「预警中心」默认视图）；
    用户明确问某分类（如"食安预警"）时 LLM 传 category=fs 只看该类。
    日期口径：按**推送日期(创建时间 createTime)**查询，使用 startDate/endDate 参数（系统推送/生成预警的时间窗口）。
    注意：该接口只认 startDate/endDate，beginDate/endDate 会被忽略（导致日期条件失效、返回全量历史）。
    records 含 category / content 预警内容 / warehouseName / createTime(推送时间) / startDate / endDate /
    status 状态 / handleList 处理记录。
    status 可选：0 待整改 / 1 已整改 / 2 已忽略 / 4 已确认。默认取当前自然月（推送日期）。无金额口径。
    返回：四态聚合 + 按仓库各状态分布&处置完成率 + 按预警类型汇总 + 待整改明细 TOP（按推送时间倒序）。
    """
    sd, ed = (start_date, end_date) if start_date else _default_month_range()
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    # 关键：按推送日期查询 → startDate/endDate（createTime 范围）。
    # 该接口不认 beginDate/endDate，之前误用会导致日期条件失效、返回全量历史数据。
    params = {"startDate": sd, "endDate": ed, "pageNo": 1, "pageSize": 200}
    if category:
        params["category"] = category
    if status is not None:
        params["status"] = status
    if wh_filtered:
        params["warehouseUuidList"] = wh_uuids

    _FIELD_TO_STATUS = {"waitRectifyQty": 0, "rectifiedQty": 1, "ignoreQty": 2, "confirmedQty": 4}
    # 四态聚合：优先用 getStatItem 直接拿准确四态（不受分页截断影响，且能验证日期区间生效）
    stat_params = {"startDate": sd, "endDate": ed}
    if category:
        stat_params["category"] = category
    if wh_filtered:
        stat_params["warehouseUuidList"] = wh_uuids
    stat_item = client.get_early_warn_stat_item(stat_params)
    stat_data = (stat_item.get("data") or {}) if stat_item.get("success") else {}
    status_agg = {_STATUS_LABEL.get(s, s): int(_num(stat_data.get(fld))) if stat_data else 0
                  for fld, s in _FIELD_TO_STATUS.items()}
    stat_sum = sum(status_agg.values())
    # 按仓库：各状态计数 + 总数
    by_warehouse_state = defaultdict(lambda: {"total": 0, 0: 0, 1: 0, 2: 0, 4: 0})
    # 按预警类型(type 指标)
    by_type = defaultdict(int)
    pending = []
    total = 0

    def _on(data, recs):
        nonlocal total
        for r in recs:
            st = int(_num(r.get("status")))
            wh = r.get("warehouseName") or "—"
            w = by_warehouse_state[wh]
            w["total"] += 1
            w[st] += 1
            t = (r.get("type") or "").strip() or "未分类"
            by_type[t] += 1
            if st == 0:
                pending.append({
                    "content": (r.get("content") or "")[:60],
                    "warehouse": wh,
                    "create_time": r.get("createTime") or "",
                    "end_date": r.get("endDate") or "",
                })
            total += 1

    _iter_pages(client, client.page_early_warn_stat, params, _on,
                max_records=config.MAX_RECORDS)
    # 交叉校验：聚合四态之和应与明细累加一致（未触发分页截断时）
    if stat_data and stat_sum and total and abs(stat_sum - total) > max(stat_sum, total) * 0.05:
        import logging
        logging.getLogger("semantic_tools").warning(
            f"[food_safety_alert] 四态合计 {stat_sum} 与明细累加 {total} 偏差>5%，区间 {sd}~{ed}")
    # total 优先用聚合四态之和（更准确，不受分页截断）
    if stat_data and stat_sum:
        total = stat_sum
    # 按仓库：各状态分布 + 处置完成率（待整改之外的状态均计为已处置）
    by_warehouse_state_list = []
    for wh, w in by_warehouse_state.items():
        resolved = w[2] + w[4]  # 完成率口径：已忽略 + 已确认（不含已整改）
        completion_rate = round(resolved / w["total"] * 100, 1) if w["total"] else 0
        by_warehouse_state_list.append({
            "warehouse": wh,
            "total": w["total"],
            "wait_rectify": w[0],
            "rectified": w[1],
            "ignored": w[2],
            "confirmed": w[4],
            "completion_rate": completion_rate,
        })
    by_warehouse_state_list.sort(key=lambda x: (x["total"], x["completion_rate"]), reverse=True)
    by_type_list = sorted(({"type": k, "type_count": v} for k, v in by_type.items()),
                          key=lambda x: x["type_count"], reverse=True)
    # 兼容保留 by_warehouse（仅总数）
    by_warehouse = [{"warehouse": x["warehouse"], "count": x["total"]} for x in by_warehouse_state_list]
    # 按推送时间(createTime)倒序，最新推送的待整改排在最前
    pending.sort(key=lambda x: x["create_time"] or "", reverse=True)
    return {
        "tool": "food_safety_alert",
        "filters": {"start_date": sd, "end_date": ed,
                    "category": _CAT_LABEL.get(category, category) if category else "全部(全分类)",
                    "date_type": "推送日期(startDate/endDate=createTime)",
                    "status": _STATUS_LABEL.get(status, status) if status is not None else None,
                    "warehouse_name": warehouse_name},
        "total": total,
        "status_agg": status_agg,
        "by_warehouse": by_warehouse,
        "by_warehouse_state": by_warehouse_state_list,
        "by_type": by_type_list,
        "pending_top": pending[:top_n],
        "note": "数据来自 data/earlyWarn/pageAndStat（records 明细）+ getStatItem（四态聚合），按**推送日期(startDate/endDate=createTime)**查询；"
                "四态聚合取自首查顶层全局合计；处置完成率=(已忽略+已确认)/总数（已整改不计入完成）。无金额口径。"
                "status 0待整改 1已整改 2已忽略 4已确认；type 为预警类型指标。",
    }


# ---------------------------------------------------------------------------
# P1 报表工具（排菜管理 DISH + 询比价 PMS）
# ---------------------------------------------------------------------------

def _resolve_all_warehouse_uuids(client, warehouse_name):
    """排菜/询比价等需 warehouseUuid 的接口：给定仓库名则取其 uuid，否则取全部仓库（封顶 30）。
    返回 uuid 列表。menu/list 要求必填 warehouseUuid，故无仓库维度时按仓库逐个调用后合并。"""
    wh_uuids, wh_filtered = _resolve_warehouse_uuids(client, warehouse_name)
    if wh_filtered:
        return wh_uuids
    try:
        d = client.query_warehouses({})
        whs = (d.get("data") or []) if d.get("success") else []
    except Exception:
        whs = []
    return [w.get("uuid") for w in whs if w.get("uuid")][:30]


def dish_cost_rate(client, start_date=None, end_date=None, warehouse_name=None, top_n=20):
    """排菜成本率（食堂差异化核心指标）：成本率 = 成本(costPrice) / 标准伙食费(stdExpAmount)。

    - 接口 dish/menu/list（warehouseUuid 必填，故按仓库逐个调用合并）：顶层 costTotal/costRatio/mealTotal，
      dateDetails[].dishDetails[] 含 dishesName/categoryName/costPrice/stdExpAmount/costRatio/meals。
    - 默认取当前自然月；支持仓库过滤。返回整体成本率 + 超成本TOP（成本率降序，重点看 >1 或偏高）。
    口径：整体成本率 = ΣcostPrice / ΣstdExpAmount；单菜成本率 = costPrice/stdExpAmount（stdExpAmount>0）。
    金额=接口直给，非估算；成本率为比值（已化为百分比展示）。"""
    sd, ed = (start_date, end_date) if start_date else _default_month_range()
    wh_list = _resolve_all_warehouse_uuids(client, warehouse_name)

    sum_cost = 0.0
    sum_std = 0.0
    sum_meal = 0.0
    dishes = []
    daily = defaultdict(lambda: {"cost": 0.0, "std": 0.0})
    menu_count = 0

    for wu in wh_list:
        try:
            r = client.dish_menu_list({"beginDate": sd, "endDate": ed, "warehouseUuid": wu})
        except Exception:
            continue
        if not (r.get("success") and r.get("data")):
            continue
        data = r["data"]
        sum_cost += _num(data.get("costTotal"))
        sum_meal += _num(data.get("mealTotal"))
        for dd in (data.get("dateDetails") or []):
            ddate = dd.get("date") or ""
            for dh in (dd.get("dishDetails") or []):
                cost = _num(dh.get("costPrice"))
                std = _num(dh.get("stdExpAmount"))
                rate = (cost / std) if std > 0 else None
                daily[ddate]["cost"] += cost
                daily[ddate]["std"] += std
                menu_count += 1
                dishes.append({
                    "dish": dh.get("dishesName") or "",
                    "category": dh.get("categoryName") or "",
                    "meals": dh.get("meals") or "",
                    "cost": cost,
                    "std": std,
                    "cost_rate": round(rate * 100, 1) if rate is not None else None,
                })

    # 用明细重算整体成本率（更准确）
    total_cost = sum(d["cost"] for d in dishes)
    total_std = sum(d["std"] for d in dishes)
    overall_rate = (total_cost / total_std * 100) if total_std > 0 else None
    over = [d for d in dishes if d["cost_rate"] is not None and d["cost_rate"] > 0]
    over.sort(key=lambda x: (x["cost_rate"] is None, x["cost_rate"] if x["cost_rate"] is not None else 0), reverse=True)
    daily_rows = [{"date": k, "cost": round(v["cost"], 2), "std": round(v["std"], 2),
                   "cost_rate": round(v["cost"] / v["std"] * 100, 1) if v["std"] > 0 else None}
                  for k, v in sorted(daily.items())]
    return {
        "tool": "dish_cost_rate",
        "filters": {"start_date": sd, "end_date": ed, "warehouse_name": warehouse_name},
        "total_cost": round(total_cost, 2),
        "total_std": round(total_std, 2),
        "overall_cost_rate": round(overall_rate, 1) if overall_rate is not None else None,
        "meal_total": round(sum_meal, 2),
        "dish_count": len(dishes),
        "over_budget_top": over[:max(int(top_n), 200)],
        "daily": daily_rows,
        "note": "数据来自 dish/menu/list（按仓库合并）；成本率=成本costPrice/标准伙食费stdExpAmount，单位为%。"
                "overall_cost_rate 用全部明细重算（更准确）；top_n 取成本率最高项（>100% 即超标准）。",
    }


def dish_reputation(client, start_date=None, end_date=None, warehouse_name=None, top_n=20):
    """出品口碑（食堂差异化核心）：菜品评价数(commentCount)与评分(scoreCount)。

    - 接口 dish/menu/list（isComment=true 才返回评价）：dishDetails[].commentCount/scoreCount/dishesName/categoryName。
    - 默认取当前自然月；支持仓库过滤。返回整体概况 + 评价数TOP + 评分偏低TOP（有评价且评分最低）。
    无金额口径；commentCount/scoreCount 为接口直给。"""
    sd, ed = (start_date, end_date) if start_date else _default_month_range()
    wh_list = _resolve_all_warehouse_uuids(client, warehouse_name)

    dishes = []
    for wu in wh_list:
        try:
            r = client.dish_menu_list({"beginDate": sd, "endDate": ed, "warehouseUuid": wu, "isComment": True})
        except Exception:
            continue
        if not (r.get("success") and r.get("data")):
            continue
        for dd in (r["data"].get("dateDetails") or []):
            for dh in (dd.get("dishDetails") or []):
                dishes.append({
                    "dish": dh.get("dishesName") or "",
                    "category": dh.get("categoryName") or "",
                    "comment_count": _num(dh.get("commentCount")),
                    "score": _num(dh.get("scoreCount")),
                    "meals": dh.get("meals") or "",
                })

    total_comments = sum(d["comment_count"] for d in dishes)
    scored = [d for d in dishes if d["score"] > 0]
    avg_score = (sum(d["score"] for d in scored) / len(scored)) if scored else None
    by_comment = sorted([d for d in dishes if d["comment_count"] > 0],
                        key=lambda x: x["comment_count"], reverse=True)[:max(int(top_n), 200)]
    low_score = sorted(scored, key=lambda x: x["score"])[:max(int(top_n), 200)]
    return {
        "tool": "dish_reputation",
        "filters": {"start_date": sd, "end_date": ed, "warehouse_name": warehouse_name},
        "dish_count": len(dishes),
        "total_comments": total_comments,
        "avg_score": round(avg_score, 2) if avg_score is not None else None,
        "top_commented": by_comment,
        "low_score_top": low_score,
        "note": "数据来自 dish/menu/list(isComment=true)；commentCount 评价总数、scoreCount 菜品评分，均为接口直给。",
    }


def dish_nutrition(client, start_date=None, end_date=None, warehouse_name=None, top_n=10):
    """营养 NRV（食堂健康膳食）：按菜单查看营养素占每日参考摄入量(NRV)比例。

    - 先 dish/menu/list 收集区间内的菜单 uuid（distinct，封顶 20 个），再逐个 dish/menu/nutrition 取 NRV。
    - 关注关键营养素占比(Rate%)：能量(nlKcal)/蛋白质(dbzG)/脂肪(zfG)/碳水(tshhwG)/钠(naMg)/钙(gaiMg)。
    - 默认取当前自然月；支持仓库过滤。返回各菜单 NRV 占比表 + 平均占比。
    口径：NRV 为每菜单(每份)占比，跨菜单平均仅为参考示意；Rate 字段已是百分比数值。"""
    sd, ed = (start_date, end_date) if start_date else _default_month_range()
    wh_list = _resolve_all_warehouse_uuids(client, warehouse_name)

    menu_uuids = []
    menu_date = {}
    for wu in wh_list:
        try:
            r = client.dish_menu_list({"beginDate": sd, "endDate": ed, "warehouseUuid": wu})
        except Exception:
            continue
        if not (r.get("success") and r.get("data")):
            continue
        for dd in (r["data"].get("dateDetails") or []):
            for dh in (dd.get("dishDetails") or []):
                mu = dh.get("menuUuid")
                if mu and mu not in menu_uuids:
                    menu_uuids.append(mu)
                    menu_date[mu] = dd.get("date") or ""

    menu_uuids = menu_uuids[:20]
    KEYS = [("nlKcal", "能量"), ("dbzG", "蛋白质"), ("zfG", "脂肪"), ("tshhwG", "碳水"),
            ("naMg", "钠"), ("gaiMg", "钙"), ("tieMg", "铁"), ("xinMcg", "锌")]
    rows = []
    sum_rate = defaultdict(float)
    ok = 0
    for mu in menu_uuids:
        try:
            r = client.dish_menu_nutrition({"uuid": mu})
        except Exception:
            continue
        if not (r.get("success") and r.get("data")):
            continue
        d = r["data"]
        row = {"menu": mu[:8], "date": menu_date.get(mu, "")}
        for k, label in KEYS:
            rate = _num(d.get(k + "Rate"))
            row[label] = rate
            if rate:
                sum_rate[label] += rate
        rows.append(row)
        ok += 1
    avg = {label: round(sum_rate[label] / ok, 1) if ok else None for _, label in KEYS}
    return {
        "tool": "dish_nutrition",
        "filters": {"start_date": sd, "end_date": ed, "warehouse_name": warehouse_name},
        "menu_count": len(menu_uuids),
        "nutrition_rows": rows,
        "avg_rate": avg,
        "note": "数据来自 dish/menu/nutrition（按菜单 uuid，封顶20个）；Rate 字段为营养素占 NRV 百分比。"
                "跨菜单平均(avg_rate)仅为示意；钠/脂肪占比偏高通常需关注。",
    }


def inquiry_effect(client, start_date=None, end_date=None, warehouse_name=None, top_n=20):
    """询比价成效（采购降本抓手）：报价参与率 + 截止情况 + 金额。

    - 接口 pms/quoteBill/page（分页，records 为报价单）：status 1待报价/2已报价；isClose 是否截止；
      matCount 品项/quoteMatCount 已报价品项；amount 总金额；inquiryBillNo 询价单号；supplierName；type 1采购2定价。
    - 默认取当前自然月；支持状态/类型过滤（本工具按区间全量聚合）。返回整体成效 + 按询价单分组报价率。
    口径：报价率 = 已报价数/总数；报价品项率 = ΣquoteMatCount/ΣmatCount；无金额估算。"""
    sd, ed = (start_date, end_date) if start_date else _default_month_range()
    params = {"beginDate": sd, "endDate": ed, "pageNo": 1, "pageSize": 200}

    recs = []
    total = 0
    quoted = 0
    closed = 0
    sum_amount = 0.0
    sum_mat = 0
    sum_quote_mat = 0
    by_inquiry = defaultdict(lambda: {"total": 0, "quoted": 0})

    def _on(data, rows):
        nonlocal total, quoted, closed, sum_amount, sum_mat, sum_quote_mat
        for x in rows:
            st = _num(x.get("status"))
            total += 1
            if st == 2:
                quoted += 1
            if x.get("isClose"):
                closed += 1
            sum_amount += _num(x.get("amount"))
            mc = _num(x.get("matCount"))
            qmc = _num(x.get("quoteMatCount"))
            sum_mat += mc
            sum_quote_mat += qmc
            ib = x.get("inquiryBillNo") or "—"
            by_inquiry[ib]["total"] += 1
            if st == 2:
                by_inquiry[ib]["quoted"] += 1
            recs.append({
                "bill_no": x.get("billNo") or "",
                "inquiry_no": ib,
                "supplier": x.get("supplierName") or "",
                "status": "已报价" if st == 2 else "待报价",
                "is_close": bool(x.get("isClose")),
                "mat_count": mc,
                "quote_mat_count": qmc,
                "amount": _num(x.get("amount")),
                "type": "采购" if _num(x.get("type")) == 1 else ("定价" if _num(x.get("type")) == 2 else ""),
            })

    first = _iter_pages(client, client.pms_quote_bill_page, params, _on, max_records=config.MAX_RECORDS)
    quote_rate = (quoted / total * 100) if total else None
    mat_rate = (sum_quote_mat / sum_mat * 100) if sum_mat else None
    inquiry_rows = [{"inquiry_no": k, "total": v["total"], "quoted": v["quoted"],
                     "quote_rate": round(v["quoted"] / v["total"] * 100, 1) if v["total"] else None}
                    for k, v in sorted(by_inquiry.items(), key=lambda kv: kv[1]["total"], reverse=True)]
    return {
        "tool": "inquiry_effect",
        "filters": {"start_date": sd, "end_date": ed, "warehouse_name": warehouse_name},
        "total": total,
        "quoted": quoted,
        "unquoted": total - quoted,
        "quote_rate": round(quote_rate, 1) if quote_rate is not None else None,
        "closed": closed,
        "sum_amount": round(sum_amount, 2),
        "mat_rate": round(mat_rate, 1) if mat_rate is not None else None,
        "by_inquiry": inquiry_rows,
        "records_sample": recs[:max(int(top_n), 200)],
        "note": "数据来自 pms/quoteBill/page；报价率=已报价/总数；报价品项率=Σ已报价品项/Σ品项。"
                "status 1待报价 2已报价；按区间全量聚合，支持前端再筛选。",
    }


# ---------------------------------------------------------------------------
# 工具注册表 + 给 LLM 的 JSON Schema 描述
# ---------------------------------------------------------------------------
TOOLS = {
    "purchase_inbound_summary": purchase_inbound_summary,
    "rank_by_dimension": rank_by_dimension,
    "daily_trend": daily_trend,
    "stock_warning": stock_warning,
    "inventory_by_warehouse": inventory_by_warehouse,
    "inventory_by_category": inventory_by_category,
    "purchase_inbound_by_warehouse": purchase_inbound_by_warehouse,
    "stock_out_by_warehouse": stock_out_by_warehouse,
    # 服务端聚合接口（金额准确，首选）
    "purchase_stat": purchase_stat,
    "purchase_ledger": purchase_ledger,
    "stock_snapshot": stock_snapshot,
    # Phase 1 供应链扩展（金额准确，服务端聚合/分页聚合）
    "supplier_settlement": supplier_settlement,
    "delivery_fulfillment": delivery_fulfillment,
    "cost_profit": cost_profit,
    "purchase_return": purchase_return,
    "picking_out": picking_out,
    "requisition_status": requisition_status,
    # Phase 2 食堂食安管理域（证照/巡检/留样/晨检/检测/添加剂，均为计数/合规口径，无金额）
    "health_certificate": health_certificate,
    "food_inspect": food_inspect,
    "sample_retention": sample_retention,
    "morning_check": morning_check,
    "detection_report": detection_report,
    "food_additive": food_additive,
    # Phase 3 综合看板 + 智能预警
    "warning_center": warning_center,
    "device_alarm_index": device_alarm_index,
    "device_alarm_detail": device_alarm_detail,
    # Phase 4 问数增强：周期对比 / 趋势（复用 purchase_stat / cost_profit 的准确金额）
    "period_compare": period_compare,
    # Phase 5 报表体系（食堂管理者视角）：驾驶舱 / 采购价对比 / 库存月报 / 食安预警
    "dashboard_overview": dashboard_overview,
    "purchase_price_compare": purchase_price_compare,
    "stock_month_report": stock_month_report,
    "food_safety_alert": food_safety_alert,
    # P1 报表：排菜管理 DISH + 询比价 PMS
    "dish_cost_rate": dish_cost_rate,
    "dish_reputation": dish_reputation,
    "dish_nutrition": dish_nutrition,
    "inquiry_effect": inquiry_effect,
}

TOOL_SCHEMAS = [
    {
        "name": "purchase_inbound_summary",
        "description": "查询某时间段内【采购数据】的汇总：入库笔数、估算总金额、合计数量（按单位拆分）。"
                       "越库(purchaseCrossIn)也是一种采购，默认同时计入采购入库与采购越库；"
                       "仅当用户明确说\"只要进库的/不含越库\"时才将 only_inbound 设为 true。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
                "only_inbound": {"type": "boolean", "description": "可选，默认 false。true=仅采购入库(purchaseIn)，不含越库"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "rank_by_dimension",
        "description": "按维度（goods商品 / goods_category商品一级分类 / warehouse仓库 / supplier供应商）对【采购数据】做金额/数量/笔数排行，返回 TOP N。"
                       "当用户问\"采购入库 商品分类分析/各分类采购金额/采购品类占比\"时，必须选 dimension='goods_category'。"
                       "默认采购含越库；仅当用户明确说\"只要进库的/不含越库\"时才将 only_inbound 设为 true。",
        "parameters": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": ["goods", "goods_category", "warehouse", "supplier"],
                              "description": "排行维度：goods=单个商品，goods_category=商品一级分类（用于分类分析/品类占比），warehouse=仓库，supplier=供应商"},
                "metric": {"type": "string", "enum": ["amount", "qty", "count"],
                           "description": "排行指标：amount估算金额 / qty数量 / count笔数"},
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "top_n": {"type": "integer", "description": "返回前几名，默认10"},
                "only_inbound": {"type": "boolean", "description": "可选，默认 false。true=仅采购入库(purchaseIn)，不含越库"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）；不传则统计全部仓库"},
            },
            "required": ["dimension", "metric", "start_date", "end_date"],
        },
    },
    {
        "name": "daily_trend",
        "description": "按日统计【采购数据】的【金额/数量/笔数】趋势序列，用于看随时间变化。"
                       "默认采购含越库；仅当用户明确说\"只要进库的/不含越库\"时才将 only_inbound 设为 true。",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["amount", "qty", "count"],
                           "description": "统计指标"},
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "only_inbound": {"type": "boolean", "description": "可选，默认 false。true=仅采购入库(purchaseIn)，不含越库"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）；不传则统计全部仓库"},
            },
            "required": ["metric", "start_date", "end_date"],
        },
    },
    {
        "name": "stock_warning",
        "description": "查询库存情况（临期/过期预警、当前库存分析）：返回已过期数量、临期预警中数量，及样例明细。",
        "parameters": {
            "type": "object",
            "properties": {
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "inventory_by_warehouse",
        "description": "查询【当前库存】商品按【仓库】分类汇总：每个仓库的商品种类数、合计数量、估算金额。"
                       "这是库存时点快照，无需日期范围；可按仓库名称筛选。"
                       "用于回答\"各仓库库存了多少/各仓库存了什么/库存按仓库分布\"。",
        "parameters": {
            "type": "object",
            "properties": {
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）；不传则返回全部仓库"},
            },
            "required": [],
        },
    },
    {
        "name": "inventory_by_category",
        "description": "查询【当前库存】商品按【一级商品分类】分类汇总与占比：每个分类的商品种类数、合计数量、估算金额，及数量占比(qty_ratio)。"
                       "分类名来自商品分类树(queryGoodsCategory)，库存数量为 0 的无效记录已剔除。"
                       "用于回答\"库存分类占比/各分类库存多少/哪些分类库存最多/按分类看库存\"。",
        "parameters": {
            "type": "object",
            "properties": {
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）；不传则返回全部仓库"},
            },
            "required": [],
        },
    },
    {
        "name": "purchase_inbound_by_warehouse",
        "description": "查询某时间段内【采购入库】按【仓库】分类汇总：每个仓库的入库笔数、合计数量、估算金额。"
                       "口径：采购入库含采购越库(purchaseCrossIn)——越库在入库侧即记为采购入库，"
                       "因此本工具默认同时统计 purchaseIn 与 purchaseCrossIn。"
                       "用于回答\"各仓库采购入库多少/各仓库进货多少/按仓库看采购\"。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "必填，具体仓库名称（模糊匹配）；不指定则拒绝查询（保护后端数据库）"},
            },
            "required": ["start_date", "end_date", "warehouse_name"],
        },
    },
    {
        "name": "stock_out_by_warehouse",
        "description": "查询某时间段内【出库记录】按【仓库】分类汇总，并拆分出库类型（如领料出库）："
                       "每个仓库的出库笔数、合计数量、估算金额，及按出库类型的拆分。"
                       "口径：采购越库在出库侧归入「领料出库」，不会单列。"
                       "用于回答\"各仓库出库多少/领料出库多少/按仓库看出库\"。"
                       "stock_out_types 可选（出库类型编码列表），不传则统计全部出库类型。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "必填，具体仓库名称（模糊匹配）；不指定则拒绝查询（保护后端数据库）"},
                "stock_out_types": {"type": "array", "items": {"type": "string"},
                                    "description": "可选，出库类型编码列表（如领料出库/采购越库）；不传则统计全部出库类型"},
            },
            "required": ["start_date", "end_date", "warehouse_name"],
        },
    },
    {
        "name": "purchase_stat",
        "description": "【采购统计·区间汇总】查询某时间段内采购/入库/出库/越库/结余的真实金额与数量。"
                       "金额由服务端聚合返回（准确，非估算），是回答\"采购额/采购金额/采购总额/入库多少金额/采购入库统计/采购含越库多少\"的首选工具。"
                       "支持 warehouse_name、supplier_name 过滤。本工具返回金额/数量汇总，不含逐笔明细；若需逐笔明细或按商品/供应商排行，请用 purchase_ledger。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "purchase_ledger",
        "description": "【采购台账·明细排行】查询某时间段内采购入库的逐笔台账，并给出按商品/供应商/一级分类的采购额(subtotal真实小计)排行 TOP N，以及台账总览(采购总额/采购次数/入库项数/供应商数)。"
                       "金额准确（服务端 subtotal 小计，非估算）。仓库过滤仅支持单仓库（模糊匹配取首个命中）。"
                       "用于回答\"采购台账/哪些商品采购最多/哪个供应商采购额最高/采购分类排行/采购明细\"。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配，仅支持单仓库）"},
                "top_n": {"type": "integer", "description": "返回前几名，默认10"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "stock_snapshot",
        "description": "【进销存库存快照·指定日期】查询某一天(快照日期)的进销存全貌：期初/采购入库/领料出库(含越库)/盘盈盘亏/调拨/加工/采购退货/领料退库/期末库存的金额与数量，以及按分类/仓库/商品的库存金额分布。"
                       "金额准确（服务端聚合，非估算），且分类名/仓库名/商品名均为接口自带（无需额外关联）。"
                       "用于回答\"某天库存金额/期末库存/进销存/库存分类金额/库存按仓库/库存按分类\"。"
                       "report_date 必填(默认今天)，是时点快照不是区间。",
        "parameters": {
            "type": "object",
            "properties": {
                "report_date": {"type": "string", "description": "快照日期 yyyy-MM-dd（默认今天）"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "supplier_settlement",
        "description": "【供应商绩效·采购结算统计】查询某时间段内各供应商(客户)的入库总金额、结算总金额、实退总金额，并给出按结算金额排行 TOP N。"
                       "金额由服务端返回（准确，非估算），是回答\"供应商结算/供应商绩效/各供应商结算金额/供应商采购排行(结算口径)\"的首选工具。"
                       "支持 warehouse_name、supplier_name 过滤；不传则统计全部。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "返回前几名供应商，默认10"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "delivery_fulfillment",
        "description": "【配送履约与验收差异】查询某时间段内配送单据的履约状态（待分拣/待发货/待验收/已验收）以及采购金额、入库金额、验收差异金额、报废金额的聚合，"
                       "并按供应商/分类/仓库拆分，给出验收状态分布。金额准确（非估算）。"
                       "用于回答\"配送履约/验收差异/配送完成情况/各供应商配送金额/采购验收差异\"。支持 warehouse_name、supplier_name 过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "返回前几名，默认10"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "cost_profit",
        "description": "【成本利润】查询某周期(date + dateType)的收入/支出/利润。利润=收入−支出，金额准确（服务端返回）。"
                       "dateType: 1按周 2按月 3按年；metric: income 收入 / expense 支出 / profit 利润(默认，同时查收支并算利润)。"
                       "用于回答\"利润/收支/盈亏/收入多少/支出多少/成本利润\"。该工具无仓库过滤（成本利润为组织级口径）。"
                       "date 默认今天；dateType 默认 2(按月)。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "周期代表日 yyyy-MM-dd（默认今天）"},
                "date_type": {"type": "integer", "description": "时间类型 1按周 2按月 3按年（默认2）"},
                "metric": {"type": "string", "enum": ["profit", "income", "expense"],
                           "description": "查询口径：profit(收支都查并算利润,默认) / income(仅收入) / expense(仅支出)"},
            },
            "required": [],
        },
    },
    {
        "name": "purchase_return",
        "description": "【退货统计】查询某时间段内退货单的应退/实退金额、笔数，按供应商/分类排行，按退货类型(正常/冲销)与财务状态拆分。金额准确（非估算）。"
                       "用于回答\"退货金额/退货多少/退货明细/各供应商退货/退货类型/退货财务状态\"。支持 warehouse_name、supplier_name 过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "返回前几名，默认10"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "picking_out",
        "description": "【领料出库统计】查询某时间段内领料单的计划/实际出库金额、数量，按仓库/去向类型(组织/员工/指定仓库)/状态拆分与排行。金额准确（非估算）。"
                       "用于回答\"领料出库/领用多少/各仓领料/领料去向/领料完成情况\"。支持 warehouse_name、dest_type、status 过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "dest_type": {"type": "integer", "description": "可选，去向类型 0组织 1员工 2指定仓库"},
                "status": {"type": "string", "description": "可选，状态 draft/initial/approved/reject/stockOuted/completed"},
                "top_n": {"type": "integer", "description": "返回前几名，默认10"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "requisition_status",
        "description": "【申购验收状态】查询某时间段内申购明细按状态(已采购/待采购/已驳回)的数量，以及申购单总金额与单据数（按仓库/供应商）。"
                       "用于回答\"申购多少/待采购多少/已采购(转采购)多少/已驳回多少/申购金额\"。支持 warehouse_name、supplier_name 过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "health_certificate",
        "description": "【健康证合规预警】查询食堂员工健康证状态分布（正常/即将到期/已过期/已停用）及临期/过期明细清单。"
                       "用于回答\"健康证快到期/过期的有几人/哪些人健康证过期/证照合规情况/健康证预警\"。"
                       "支持 warehouse_name 过滤；status 可选(0禁用1启用2即将到期3已到期)。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "status": {"type": "integer", "description": "可选，状态过滤 0禁用 1启用 2即将到期 3已到期；不传则查全部"},
            },
            "required": [],
        },
    },
    {
        "name": "food_inspect",
        "description": "【食安巡检·日管控/周排查/月调度】查询某时间段内巡检完成率（已审核/待审核）、不符合项数量与分布。"
                       "inspect_type 取 day(日管控)/week(周排查)/month(月调度)。用于回答\"巡检完成率/食安巡检/日管控/周排查/月调度/"
                       "不符合项多少/巡检情况\"。支持 start_date/end_date、warehouse_name 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "inspect_type": {"type": "string", "enum": ["day", "week", "month"],
                                 "description": "巡检类型：day=日管控(默认) / week=周排查 / month=月调度"},
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "sample_retention",
        "description": "【留样管理】查询留样各状态数量（待存入/待取出/留样中/已取出）及合规留存口径。"
                       "用于回答\"留样多少/留样情况/待取出几单/留样中几单/食品留样\"。支持 start_date/end_date、warehouse_name 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "morning_check",
        "description": "【晨检记录】查询某时间段内晨检（含午/晚检）的合格/不合格数量、在岗数量、不合格原因分布与按仓库分布。"
                       "用于回答\"晨检合格率/有多少人晨检不合格/晨检异常/体温异常/员工健康晨检\"。"
                       "支持 start_date/end_date、warehouse_name、check_type(5晨检10午检15晚检)、qualified(0不合格1合格)过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "check_type": {"type": "integer", "description": "可选，5晨检 10午检 15晚检"},
                "qualified": {"type": "integer", "description": "可选，0不合格 1合格"},
            },
            "required": [],
        },
    },
    {
        "name": "detection_report",
        "description": "【检测报告】查询食材/环境检测合格率、不合格数量，以及按供应商/商品的不合格分布。"
                       "用于回答\"检测合格率/食材检测合格吗/哪些检测不合格/检测报告/农残检测\"。"
                       "支持 start_date/end_date、warehouse_name、supplier_name 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，供应商名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "food_additive",
        "description": "【食品添加剂】查询添加剂使用台账与限量预警：按添加剂聚合使用次数、平均用量、超标(使用量>标准量)次数，"
                       "及按仓库分布。用于回答\"添加剂使用/添加剂超标/添加剂台账/添加剂用量预警\"。"
                       "支持 start_date/end_date、warehouse_name 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "返回前几名，默认10"},
            },
            "required": [],
        },
    },
    {
        "name": "warning_center",
        "description": "【综合预警中心】查询各类预警（证照到期/库存过期/食安巡检不符合项/采购验收等）的待整改/已整改/已忽略/已确认状态聚合，"
                       "及按分类、待整改明细 TOP。用于回答\"有哪些预警/哪些待整改/证照快到期/库存过期预警/巡检不符合项/预警看板\"。"
                       "支持 category(fs食安/certificate证照/stock仓储/purchase采购/accept验收)、status(0待整改1已整改2已忽略4已确认)、"
                       "start_date/end_date、warehouse_name 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "category": {"type": "string", "description": "可选，分类：fs食安/certificate证照/stock仓储/purchase采购/accept验收"},
                "status": {"type": "integer", "description": "可选，状态 0待整改 1已整改 2已忽略 4已确认"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "待整改明细返回前几名，默认20"},
            },
            "required": [],
        },
    },
    {
        "name": "device_alarm_index",
        "description": "【环境设备告警指数】查询厨房环境设备（温度/湿度/烟雾/燃气/水浸/AI巡检）的累计告警总数，做指数看板。"
                       "用于回答\"环境告警多少/温度告警/燃气告警/烟雾告警/设备告警指数/消杀环境看板\"。支持 warehouse_name 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "device_alarm_detail",
        "description": "【环境设备告警明细】查询消杀/环境设备告警明细（温度/湿度/烟雾/燃气/水浸/AI巡检等），含告警类型/内容/数值/状态/取证。"
                       "用于回答\"设备告警明细/未处理告警/消杀告警/环境设备告警记录/温度超标明细\"。"
                       "支持 start_date/end_date、status(0未处理1已处理2已忽略)、warehouse_name、app_type(1物联网2AI巡检) 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "可选，开始日期 yyyy-MM-dd"},
                "end_date": {"type": "string", "description": "可选，结束日期 yyyy-MM-dd"},
                "status": {"type": "integer", "description": "可选，处理状态 0未处理 1已处理 2已忽略"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "app_type": {"type": "integer", "description": "可选，应用类型 1物联网 2AI巡检"},
                "top_n": {"type": "integer", "description": "未处理明细返回前几名，默认20"},
            },
            "required": [],
        },
    },
    {
        "name": "period_compare",
        "description": "【周期对比 / 趋势（问数增强）】把同一个【金额准确的底层工具】在多个周期上分别执行，"
                       "串成时间序列并自动计算相邻周期环比（差值与百分比）。"
                       "当用户问「趋势 / 走势 / 比上个月 / 环比 / 同比 / 每月对比 / 上半年走势 / 一季度各月 / "
                       "7月比6月多多少 / 近半年采购额变化 / 各月利润对比」时使用本工具。"
                       "base_tool 可选：purchase_stat（采购统计，主对比指标=采购总额含越库；支持 warehouse_name/supplier_name 过滤）；"
                       "cost_profit（成本利润，metric=income 收入 / expense 支出 / profit 利润，组织级无仓库过滤）。"
                       "periods 为周期列表，元素格式支持：\"YYYY-MM\"（自然月）、\"YYYY\"（自然年）、"
                       "\"YYYY-MM-DD~YYYY-MM-DD\"（显式区间）。周期按列表顺序串联。金额均来自服务端聚合，准确非估算。",
        "parameters": {
            "type": "object",
            "properties": {
                "base_tool": {"type": "string", "enum": ["purchase_stat", "cost_profit"],
                              "description": "底层准确工具：purchase_stat=采购统计(采购额/入库/出库/结余)；cost_profit=成本利润(收入/支出/利润)"},
                "periods": {"type": "array", "items": {"type": "string"},
                            "description": "周期列表，按时间顺序排列。格式：\"YYYY-MM\" 自然月 / \"YYYY\" 自然年 / \"YYYY-MM-DD~YYYY-MM-DD\" 显式区间"},
                "warehouse_name": {"type": "string", "description": "可选，仅 base_tool=purchase_stat 时生效，仓库名称（模糊匹配）"},
                "supplier_name": {"type": "string", "description": "可选，仅 base_tool=purchase_stat 时生效，供应商名称（模糊匹配）"},
                "metric": {"type": "string", "enum": ["profit", "income", "expense"],
                           "description": "可选，仅 base_tool=cost_profit 时生效，默认 profit（利润）；income=收入；expense=支出"},
            },
            "required": ["base_tool", "periods"],
        },
    },
    {
        "name": "dashboard_overview",
        "description": "【经营驾驶舱总览】食堂管理者每天进系统的第一眼：一站式汇总今日关键经营指标 + 待处理单据 + 食安概况。"
                       "当用户问「经营总览 / 驾驶舱 / 今天经营情况 / 今日概览 / 待办单据 / 今天采购了多少 / 晨检多少人 / 食安概况」时优先用本工具。"
                       "返回：今日采购金额/验收金额/留样项数/晨检人数（均含日同比）、本月待处理单据（调整单/申购/采购/退货数量与金额）、食安各模块概况清单。"
                       "指标为接口直接返回，无金额估算。可选 warehouse_name 过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "warehouse_name": {"type": "string", "description": "可选，仓库/食堂名称（模糊匹配）"},
            },
            "required": [],
        },
    },
    {
        "name": "purchase_price_compare",
        "description": "【采购价对比】逐笔对比采购单价与平台参考价，找出买贵了的单子（食堂成本把控核心）。"
                       "当用户问「采购价对比 / 比平台价高多少 / 哪里买贵了 / 超价 / 采购单价 vs 市场价 / 新发地价对比 / 采购价异常」时使用。"
                       "返回超价统计（超价笔数、超价采购额估算）与明细 TOP（按超出比例降序，含商品/规格/仓库/供应商/采购单价/平台价/超出比例/入库数量）。"
                       "默认当前自然月；支持 start_date/end_date/warehouse_name 过滤。price×入库数量 为采购额估算，回答须注明「估算」。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd，默认当前自然月首日"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd，默认今天"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "返回超价最严重的前 N 条，默认 20"},
            },
            "required": [],
        },
    },
    {
        "name": "stock_month_report",
        "description": "【库存月报】按月复盘进销存（食堂月度经营复盘）。按商品汇总当月期初/入库/出库/期末金额与数量，金额由服务端聚合准确非估算。"
                       "当用户问「库存月报 / 某月库存 / 期末库存多少 / 当月进销存 / 库存月度汇总 / 各商品库存金额」时使用。"
                       "返回月度汇总（期初/采购入库/采购越库/领料出库/入库/出库/期末金额与数量）+ 商品明细 TOP（按期末金额降序）。"
                       "report_date 为月报日期（取当月首日，默认上一完整月）；支持 warehouse_name 过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "report_date": {"type": "string", "description": "月报日期 yyyy-MM-dd（取当月首日），默认上一完整月首日，如 2026-07-01"},
                "warehouse_name": {"type": "string", "description": "必填，具体仓库名称（模糊匹配）；不指定则拒绝查询（保护后端数据库）"},
                "top_n": {"type": "integer", "description": "返回期末金额最高的前 N 个商品，默认 20"},
            },
            "required": ["warehouse_name"],
        },
    },
    {
        "name": "food_safety_alert",
        "description": "【预警中心总览】统一查看各类预警（食安fs/证照certificate/定价pricing/采购purchase/验收accept/排菜dish/仓储stock/询比价inquiry）的红线看板。"
                       "默认查**全部分类**（对齐生产系统「预警中心」默认视图），展示待整改/已整改/已忽略/已确认四态数量、按仓库状态分布与处置完成率、按预警类型汇总，并列出待整改明细 TOP。"
                       "当用户问「预警中心 / 预警总览 / 各类预警 / 待整改多少 / 预警明细 / 哪些问题没改」时使用；"
                       "若明确问某分类（如「食安预警」「证照到期」「定价预警」）可传 category=fs/certificate/pricing 等只看该类。"
                       "默认当前自然月；支持 start_date/end_date/category/warehouse_name/status(0待整改 1已整改 2已忽略 4已确认) 过滤。无金额口径。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd，默认当前自然月首日"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd，默认今天"},
                "category": {"type": "string", "description": "可选，预警分类：fs食安/certificate证照/pricing定价/purchase采购/accept验收/dish排菜/stock仓储/inquiry询比价；不传则查全部分类"},
                "warehouse_name": {"type": "string", "description": "可选，仓库/食堂名称（模糊匹配）"},
                "status": {"type": "integer", "description": "可选，预警状态：0待整改 1已整改 2已忽略 4已确认；不传则查全部"},
                "top_n": {"type": "integer", "description": "返回待整改明细前 N 条，默认 20"},
            },
            "required": [],
        },
    },
    {
        "name": "dish_cost_rate",
        "description": "【排菜成本率】分析某时间段菜单成本占标准伙食费的比例（成本率=成本/标准伙食费），找出超成本的菜品。"
                       "数据来自 dish/menu/list（按仓库合并）。用于回答\"排菜成本率/菜单成本/哪些菜超标准/餐标\"。可选 warehouse_name、top_n。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd，默认本月"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd，默认本月"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "超成本TOP返回条数，默认20"},
            },
            "required": [],
        },
    },
    {
        "name": "dish_reputation",
        "description": "【出品口碑】分析菜品的评价数与评分，找出评价最多、评分偏低的菜品。"
                       "数据来自 dish/menu/list(isComment=true)。用于回答\"菜品评价/口碑/评分/哪些菜受欢迎/差评\"。可选 warehouse_name、top_n。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd，默认本月"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd，默认本月"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "返回前 N 条，默认20"},
            },
            "required": [],
        },
    },
    {
        "name": "dish_nutrition",
        "description": "【营养 NRV】查看菜单营养素占每日参考摄入量(NRV)的比例（能量/蛋白质/脂肪/碳水/钠/钙等）。"
                       "数据来自 dish/menu/nutrition（按菜单 uuid）。用于回答\"营养/热量/蛋白质/钠/健康膳食\"。可选 warehouse_name、top_n。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd，默认本月"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd，默认本月"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "营养菜单数，默认10"},
            },
            "required": [],
        },
    },
    {
        "name": "inquiry_effect",
        "description": "【询比价成效】分析询比价的报价参与率、截止情况与金额，按询价单分组看报价率。"
                       "数据来自 pms/quoteBill/page。用于回答\"询比价成效/报价率/中标/比价/降本\"。可选 warehouse_name、top_n。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "开始日期 yyyy-MM-dd，默认本月"},
                "end_date": {"type": "string", "description": "结束日期 yyyy-MM-dd，默认本月"},
                "warehouse_name": {"type": "string", "description": "可选，仓库名称（模糊匹配）"},
                "top_n": {"type": "integer", "description": "明细样本条数，默认20"},
            },
            "required": [],
        },
    },
]


# 口径同步：TOOL_SCHEMAS 的描述文案统一以口径注册表 METRICS 为准（单一来源，消除漂移）。
# 上方参数 schema 仍是结构化字面量（低频变动）；此处把每个工具的 description 覆盖为注册表值，
# 保证「工具描述/口径说明」只有口径注册表一个真相来源（改口径只动 metrics_registry.py）。
for _schema in TOOL_SCHEMAS:
    _m = METRICS.get(_schema["name"])
    if _m:
        _schema["description"] = _m["description"]


TOOL_LABELS = {
    "purchase_inbound_summary": "采购入库汇总",
    "rank_by_dimension": "维度排行",
    "daily_trend": "按日趋势",
    "stock_warning": "库存预警",
    "inventory_by_warehouse": "库存按仓库汇总",
    "inventory_by_category": "库存分类占比",
    "purchase_inbound_by_warehouse": "采购入库按仓库汇总",
    "stock_out_by_warehouse": "出库按仓库汇总",
    "purchase_stat": "采购统计(服务端聚合)",
    "purchase_ledger": "采购台账(服务端聚合)",
    "stock_snapshot": "进销存库存快照(服务端聚合)",
    "supplier_settlement": "供应商结算统计",
    "delivery_fulfillment": "配送履约与验收差异",
    "cost_profit": "成本利润",
    "purchase_return": "退货统计",
    "picking_out": "领料出库统计",
    "requisition_status": "申购验收状态",
    "health_certificate": "健康证合规预警",
    "food_inspect": "食安巡检(日/周/月)",
    "sample_retention": "留样管理",
    "morning_check": "晨检记录",
    "detection_report": "检测报告",
    "food_additive": "食品添加剂",
    "warning_center": "综合预警中心",
    "device_alarm_index": "环境设备告警指数",
    "device_alarm_detail": "环境设备告警明细",
    "period_compare": "周期对比·趋势",
    "dashboard_overview": "经营驾驶舱总览",
    "purchase_price_compare": "采购价对比",
    "stock_month_report": "库存月报",
    "food_safety_alert": "预警中心总览",
    "dish_cost_rate": "排菜成本率",
    "dish_reputation": "出品口碑",
    "dish_nutrition": "营养 NRV",
    "inquiry_effect": "询比价成效",
}


# 带时间区间的工具：若模型漏填日期，统一兜底为「当前自然月（本月1日~今天）」
_DATE_TOOLS = {
    "purchase_inbound_summary", "rank_by_dimension", "daily_trend",
    "purchase_inbound_by_warehouse", "stock_out_by_warehouse",
    "supplier_settlement", "delivery_fulfillment", "purchase_return",
    "picking_out", "requisition_status",
    # Phase 2 食安域（含可选日期的）：模型漏填时默认本月，避免无区间空查
    "food_inspect", "sample_retention", "morning_check", "detection_report", "food_additive",
    # Phase 3 综合预警/环境告警（含可选日期）
    "warning_center", "device_alarm_detail",
}


def _default_month_range():
    """返回当前自然月区间：本月1日 ~ 今天（yyyy-MM-dd）。"""
    from datetime import date
    t = date.today()
    return t.replace(day=1).strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d")


def call_tool(client, name, args):
    """按名称调用工具；返回 (result_dict, error_str)。"""
    fn = TOOLS.get(name)
    if not fn:
        return None, f"未知工具: {name}"
    try:
        # 过滤掉 LLM 可能多传的字段
        import inspect
        sig = inspect.signature(fn)
        allowed = {k: v for k, v in args.items() if k in sig.parameters}
        # 日期兜底：五个带时间区间的工具若模型未给日期，默认取当前自然月
        if name in _DATE_TOOLS:
            sd, ed = _default_month_range()
            if not allowed.get("start_date"):
                allowed["start_date"] = sd
            if not allowed.get("end_date"):
                allowed["end_date"] = ed
        # 库存快照：report_date 必填，缺省默认今天（时点快照）
        if name == "stock_snapshot" and not allowed.get("report_date"):
            allowed["report_date"] = date.today().strftime("%Y-%m-%d")
        # 成本利润：schema/LLM 用 `date`，函数参数名为 `date_`（避免与 datetime.date 冲突）。
        # 此处统一把 LLM 传来的 `date` 映射到函数参数 `date_`（allowed 已被签名过滤掉 `date`，
        # 故需从原始 args 取回 LLM 意图的日期）。date_type 缺省 2(按月)、metric 缺省 profit。
        if name == "cost_profit":
            raw_date = args.get("date") or allowed.get("date_") or date.today().strftime("%Y-%m-%d")
            allowed["date_"] = raw_date
            allowed.setdefault("date_type", 2)
            allowed.setdefault("metric", "profit")
        return fn(client, **allowed), None
    except Exception as e:  # noqa
        return None, f"工具执行异常: {e}"
