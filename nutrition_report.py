# -*- coding: utf-8 -*-
"""营养分析报表：菜单 + 实际就餐人数 + 领料出库营养分析。

数据源（均为后厨管家开放接口，经 HCGClient 代理调用）：
  1) /hcgj-portal/api/dish/menu/list          菜单（带 /api，beginDate/endDate/warehouseUuid 必传）
  2) /hcgj-portal/cost/meals/page               实际就餐人数（分页，不带 /api）
  3) /hcgj-portal/api/wms/stock/pageStockOut   领料出库记录（按商品合并总重量；分类优先用记录自带 firstCategoryName）
  4) /hcgj-portal/wms/goods/details             按商品 uuid 查商品详情（含 goodsNutrition 每 100g 营养）
  5) /hcgj-portal/api/wms/com/queryGoodsCategory  商品分类树（firstCategoryName 为空时按 uuid 还原分类名）

口径：
  - 领料出库按业务口径包含采购越库（出库侧归入领料出库，与 metrics_registry 一致）。
  - 分类：优先用 pageStockOut 记录自带的 firstCategoryName（第一分类名称）；为空时，
    用商品详情的 firstCategoryUuid 经 queryGoodsCategory 还原分类名（兜底）。
  - 商品营养：用 /wms/goods/details 按出库记录实际的商品 uuid 查询（只查用到的，
    不全量拉商品主数据），结果按 goodsUuid 本地文件缓存（默认 24h 有效，环境变量
    GOODS_CACHE_TTL 可调、GOODS_CACHE_REFRESH=1 强制刷新）；缓存命中则不发请求。
    details 返回 data.goodsNutrition 含 nlKcal/dbzG/zfG/tshhwG（字符串），已兼容提取。
  - 营养值优先级：① 商品主数据每 100g 营养字段 > ② 内置食材营养表（确定性、无需联网）
    > ③ 大模型估算（仅 NUTRITION_USE_LLM=1 时启用，默认关闭）。默认不调大模型，
    报表生成无数十秒模型等待；主数据营养为非检测值，仅供参考。
  - 重量单位换算：公斤/斤/克 统一折算成克；无法识别单位时按 qty 当克估算并标注。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from hunyuan import get_llm, MockLLM
from hcg_client import extract_warehouses


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _num(v):
    try:
        f = float(v)
        return f if f == f else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_json(text: str) -> dict | list | None:
    """从模型输出中稳健提取 JSON（容忍 markdown 代码块 / 前后缀文字）。"""
    if not text:
        return None
    s = text.strip()
    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # 找第一个 { 或 [ 到最后一个 } 或 ]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i = s.find(open_c)
        j = s.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return None


def _walk_date_qty(x, out):
    """递归扫描接口返回，提取 {date, qty} 结构（防御不同 schema）。

    cost/meals/page 真实人数字段为 realRepastQty（实际就餐人数）/ stdRepastQty
    （标准就餐人数）；优先取实际就餐人数，兼容旧接口的 repastQty/qty 等写法。
    """
    if isinstance(x, dict):
        dte = x.get("date") or x.get("statDate") or x.get("mealDate")
        q = x.get("realRepastQty", x.get("stdRepastQty",
                x.get("repastQty", x.get("qty", x.get("repastCount", x.get("count"))))))
        if dte is not None and q is not None:
            try:
                out.append({"date": str(dte), "qty": _num(q)})
            except Exception:
                pass
            return
        for v in x.values():
            _walk_date_qty(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk_date_qty(v, out)


# 出库类型枚举（据后端文档）：
#   pickingOut:领料出库, purchaseReturnOut:采购退货, takeStockOut:盘亏出库,
#   processOut:加工出库, purchaseCrossOut:采购越库, scrapOut:报废出库,
#   allocateOut:调拨出库, batchImportOut:批量导入出库, sortingOut:分拣出库
# 营养报表只统计「领料出库」和「采购越库」。
LINGLIAO_OUT_CODES = {"pickingOut", "purchaseCrossOut"}
LINGLIAO_TYPE_TEXTS = {"领料出库", "采购越库"}
LINGLIAO_KEYWORDS = ("领料", "越库")  # 中文关键字兜底

# 中国居民膳食营养素参考摄入量（DRIs 2023）参考值
# 18-49岁轻体力活动成年人每日平均参考；脂肪/碳水按供能比中位数折算。
DRIS_2023_ADULT = {
    "energy_kcal": 2000,
    "protein_g": 60,
    "fat_g": 55,
    "carb_g": 280,
    "source": "《中国居民膳食营养素参考摄入量》（2023版），18-49岁轻体力活动成年人平均值；脂肪/碳水按供能比中位数折算，仅供参考",
}


def _is_lingliao_type(out_type: str, type_text: str = "") -> bool:
    """判断是否「领料出库 / 采购越库」（营养报表统计口径，精确）。

    匹配优先级：
      1) 后端 code 精确命中（pickingOut / purchaseCrossOut）；
      2) typeText 中文标签精确命中（领料出库 / 采购越库）；
      3) 中文关键字（领料/越库）兜底，兼容历史宽松写法。
    其余出库类型（采购退货/盘亏/加工/报废/调拨/批量导入/分拣）一律排除。
    """
    code = (out_type or "").strip()
    if code in LINGLIAO_OUT_CODES:
        return True
    txt = (type_text or "").strip()
    if txt in LINGLIAO_TYPE_TEXTS:
        return True
    if any(k in txt for k in LINGLIAO_KEYWORDS) or any(k in code for k in LINGLIAO_KEYWORDS):
        return True
    return False


def _to_grams(qty: float, unit: str) -> tuple:
    """把数量按单位折算为克。返回 (克, 是否精确)。"""
    if not unit:
        return qty, False
    u = str(unit).lower()
    if "公斤" in u or u in ("kg", "千克"):
        return qty * 1000.0, True
    if "斤" in u:
        return qty * 500.0, True
    if "克" in u or u in ("g", "ml", "毫升", "l", "升"):
        return qty, True
    return qty, False  # 件/份/只 等无法精确换算，按克估算


# ---------------------------------------------------------------------------
# 1) 菜单
# ---------------------------------------------------------------------------
def fetch_menu(client, begin_date: str, end_date: str, warehouse_uuid: str | None = None,
               warehouse_name: str | None = None) -> dict:
    """拉取区间菜单。返回结构化菜单（按日期 -> 餐次 -> 菜品）。"""
    params = {"beginDate": begin_date, "endDate": end_date}
    if warehouse_uuid:
        params["warehouseUuid"] = warehouse_uuid
    d = client.dish_menu(params)
    if not d.get("success"):
        raise RuntimeError(d.get("message") or "菜单接口调用失败")
    data = d.get("data") or {}
    dates = []
    for dd in data.get("dateDetails") or []:
        dishes = []
        for it in dd.get("dishDetails") or []:
            recipe = [r.get("goodsName") for r in (it.get("dishDishesRecipeDetails") or [])
                      if r.get("goodsName")]
            dishes.append({
                "dishesName": it.get("dishesName") or "",
                "img": it.get("img") or "",
                "meals": it.get("meals") or "",
                "categoryName": it.get("categoryName") or "",
                "mealStand": it.get("mealStand"),
                "qty": it.get("qty"),
                "isRec": it.get("isRec"),
                "scoreCount": it.get("scoreCount"),
                "repeat": it.get("repeat"),
                "tags": it.get("tags") or [],
                "recipe": recipe,
            })
        dates.append({
            "date": dd.get("date"),
            "week": dd.get("week"),
            "status": dd.get("status"),
            "stdRepastQty": dd.get("stdRepastQty"),
            "mealStandTotal": dd.get("mealStandTotal"),
            "schedulingUser": dd.get("schedulingUser"),
            "dishes": dishes,
        })
    return {
        "begin_date": data.get("beginDate") or begin_date,
        "end_date": data.get("endDate") or end_date,
        "warehouse_name": data.get("warehouseName") or warehouse_name or "",
        "warehouse_uuid": data.get("warehouseUuid") or warehouse_uuid or "",
        "date_details": dates,
    }


# ---------------------------------------------------------------------------
# 2) 实际就餐人数
# ---------------------------------------------------------------------------
def _months_between(begin_date: str, end_date: str) -> list:
    """返回覆盖 begin~end（YYYY-MM-DD）的所有月份 YYYY-MM 列表（含跨月）。

    前端「周」模式区间可能跨月，就餐人数接口以月份为维度（monthDate），
    故需逐月查询覆盖整个区间。
    """
    ym1 = (begin_date or "")[:7]
    ym2 = (end_date or "")[:7]
    if not ym1 or not ym2:
        return [x for x in (ym1, ym2) if x]
    if ym1 == ym2:
        return [ym1]
    res = []
    y, m = int(ym1[:4]), int(ym1[5:7])
    y2, m2 = int(ym2[:4]), int(ym2[5:7])
    while (y, m) <= (y2, m2):
        res.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return res


def fetch_repast_qty(client, begin_date: str, end_date: str,
                     warehouse_uuid: str | None = None) -> dict:
    """获取实际就餐人数（按日期）。使用 /cost/meals/page 分页查询。

    返回 {dates:[{date,qty}], total, source, note}。就餐人数取分页 records 里
    的日期 + 人数（_walk_date_qty 防御性提取 date/statDate/mealDate 与
    repastQty/qty/repastCount/count）。区间无记录则 total=None。
    """
    result = {"dates": [], "total": None, "source": None, "note": ""}
    rows = []

    def _collect(params):
        # meals_page 翻页汇总（params 已含分页基准字段，仅追加 pageNo）
        try:
            page = 1
            while True:
                params["pageNo"] = page
                d = client.meals_page(params)
                if not d.get("success"):
                    break
                data = d.get("data") or {}
                recs = data.get("records") or []
                if not recs:
                    break
                for r in recs:
                    _walk_date_qty(r, rows)
                pages = data.get("pages", 1)
                if page >= pages or page >= 200:
                    break
                page += 1
        except Exception:
            pass

    # 优先按 monthDate 查询（对齐后厨管家系统；cost/meals/page 以月份为维度）。
    # begin_date/end_date 为 YYYY-MM-DD；周模式可能跨月，逐月覆盖。
    for ym in _months_between(begin_date, end_date):
        p = {"monthDate": ym}
        if warehouse_uuid:
            p["warehouseUuid"] = warehouse_uuid
        _collect(p)

    # 兜底：若 monthDate 全空，回退 beginDate/endDate 区间查询
    if not rows:
        p = {"beginDate": begin_date, "endDate": end_date}
        if warehouse_uuid:
            p["warehouseUuid"] = warehouse_uuid
        _collect(p)

    if rows:
        result["dates"] = rows
        result["source"] = "cost/meals/page"
    else:
        result["note"] = "就餐人数接口未返回数据"
    if rows:
        result["total"] = round(sum(r["qty"] for r in rows), 2)
    return result


# ---------------------------------------------------------------------------
# 3) 领料出库商品详情（按商品合并总重量）
# ---------------------------------------------------------------------------
def _extract_goods_nutri(g: dict) -> dict | None:
    """从商品主数据记录里提取每 100g 营养值（能量/蛋白质/脂肪/碳水）。

    来源：pageGoods 商品主数据。兼容两类字段命名：
      1) 后排菜营养约定（nlKcal/dbzG/zfG/tshhwG）；
      2) 常见英文/中文写法（energy/热量/能量、protein/蛋白质、fat/脂肪、carb/碳水）；
      3) 若存在嵌套营养对象（goodsNutrition / nutrition / nutri / foodNutrition）
         则优先在该对象内查找。
    四个值都缺失时返回 None（交由大模型/兜底表处理）。
    """
    cand = {
        "energy_kcal": ("nlKcal", "energyKcal", "energy", "kcal", "heat",
                        "heatEnergy", "rl", "calorie", "热量", "能量"),
        "protein_g":   ("dbzG", "proteinG", "protein", "dbz", "蛋白质"),
        "fat_g":       ("zfG", "fatG", "fat", "zf", "脂肪"),
        "carb_g":      ("tshhwG", "carbG", "carbohydrate", "carb", "tshhw", "碳水"),
    }

    def _pick(src: dict) -> dict:
        out = {}
        for key, names in cand.items():
            v = None
            for n in names:
                if src.get(n) not in (None, "", 0):
                    v = _num(src.get(n))
                    break
            out[key] = v
        return out

    # 先查顶层字段
    top = _pick(g)
    # 再查嵌套营养对象（若有）
    nested = None
    for nk in ("goodsNutrition", "nutrition", "nutri", "foodNutrition", "nutriInfo"):
        obj = g.get(nk)
        if isinstance(obj, dict):
            nested = _pick(obj)
            break
    # 合并：嵌套优先于顶层（避免顶层放的是别的东西）
    merged = {}
    for key in cand:
        v = (nested or {}).get(key)
        if v in (None, 0):
            v = top.get(key)
        merged[key] = v
    if all(v in (None, 0) for v in merged.values()):
        return None
    return merged


def _build_category_map(client) -> dict:
    """分类 uuid -> name 映射（queryGoodsCategory 扁平列表，兼容 records/list 两种结构）。

    用于：当 pageStockOut 记录的 firstCategoryName 为空时，用商品主数据的
    firstCategoryUuid 还原一级分类名（部分账号的采购越库记录 firstCategoryName 为空）。
    单次调用，失败则返回空映射（不影响主流程）。
    """
    m = {}
    try:
        d = client.query_goods_category({})
        if not d.get("success"):
            return m
        data = d.get("data") or {}
        cats = data if isinstance(data, list) else (data.get("records") or data.get("list") or [])
        for c in cats:
            u = c.get("uuid")
            if u:
                m[u] = c.get("name") or ""
    except Exception:
        pass
    return m


# ---------------------------------------------------------------------------
# 商品营养缓存（按 uuid 本地缓存，只查出库记录用到的商品）
# ---------------------------------------------------------------------------
# 说明：后厨管家 /wms/goods/details 可按商品 uuid 查询详情（含 goodsNutrition
# 每 100g 营养）。「只拉用到的」即：报表先取区间出库记录，收集实际出现的商品
# uuid，再对每个不在本地缓存里的 uuid 调 details 拉营养并写入本地文件缓存
# （默认 24h 有效），避免全量拉取商品主数据、也避免重复网络请求。
_GOODS_CACHE: dict = {}
_GOODS_CACHE_TS: float = 0.0
_GOODS_CACHE_TTL: int = int(os.environ.get("GOODS_CACHE_TTL", "86400"))  # 默认 24h
_GOODS_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "goods_nutrition_details.json"


def _goods_cache_load_file() -> bool:
    global _GOODS_CACHE, _GOODS_CACHE_TS
    if not _GOODS_CACHE_PATH.exists():
        return False
    try:
        blob = json.loads(_GOODS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    g = blob.get("goods")
    ts = blob.get("saved_at")
    if not isinstance(g, dict) or not ts:
        return False
    _GOODS_CACHE = g
    _GOODS_CACHE_TS = ts
    return True


def _goods_cache_save_file():
    try:
        _GOODS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GOODS_CACHE_PATH.write_text(
            json.dumps({"saved_at": _GOODS_CACHE_TS, "goods": _GOODS_CACHE},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _fetch_one_goods(client, uuid: str, cat_map: dict) -> dict:
    """单个商品详情 -> {name, nutri100, category, firstCategoryUuid}。

    调 /wms/goods/details（参数 uuid）；营养来自返回的 goodsNutrition 嵌套对象，
    分类优先用详情自带 firstCategoryName，否则用 firstCategoryUuid 经
    queryGoodsCategory 还原（兜底）。失败返回空壳（不阻断主流程）。
    """
    try:
        d = client.goods_details(uuid)
        if d.get("success"):
            g = d.get("data") or {}
            cat = g.get("firstCategoryName") or cat_map.get(g.get("firstCategoryUuid")) or ""
            return {
                "name": g.get("goodsName") or g.get("name") or "未知商品",
                "nutri100": _extract_goods_nutri(g),
                "category": cat,
                "firstCategoryUuid": g.get("firstCategoryUuid") or "",
            }
    except Exception:
        pass
    return {"name": "未知商品", "nutri100": None, "category": "", "firstCategoryUuid": ""}


def get_goods_map(client, goods_uuids) -> dict:
    """返回用到的商品 uuid -> {name, category, nutri100, firstCategoryUuid}。

    只查出库记录实际出现的商品：对每个不在本地缓存里的 uuid 调
    /wms/goods/details 拉营养，结果写入本地文件缓存（默认 24h 有效，
    GOODS_CACHE_TTL 可调，GOODS_CACHE_REFRESH=1 强制刷新）。缓存命中则不发请求，
    因此「只拉用到的 + 本地缓存」兼顾速度与后端压力。
    """
    global _GOODS_CACHE, _GOODS_CACHE_TS
    if os.environ.get("GOODS_CACHE_REFRESH") == "1":
        _GOODS_CACHE = {}
        _GOODS_CACHE_TS = 0.0
        try:
            _GOODS_CACHE_PATH.unlink()
        except Exception:
            pass
    if not _GOODS_CACHE:
        _goods_cache_load_file()

    uuids = list(goods_uuids or [])
    missing = [u for u in uuids if u and u not in _GOODS_CACHE]
    if missing:
        cat_map = _build_category_map(client)
        for u in missing:
            _GOODS_CACHE[u] = _fetch_one_goods(client, u, cat_map)
        _GOODS_CACHE_TS = time.time()
        _goods_cache_save_file()

    return {u: _GOODS_CACHE[u] for u in uuids if u in _GOODS_CACHE}


def fetch_stock_out_by_goods(client, begin_date: str, end_date: str,
                             warehouse_uuid: str | None = None) -> dict:
    """拉取区间出库记录，按商品合并总重量。

    统计口径（据后端枚举）：只统计「领料出库 pickingOut」与「采购越库 purchaseCrossOut」。
    其余出库类型（采购退货/盘亏/加工/报废/调拨/批量导入/分拣）一律排除。

    返回 dict：
      - items: 商品聚合列表（按重量降序），仅含领料出库 + 采购越库；
      - out_type_dist: 后端实际返回的出库类型分布（诊断用）；
      - picked_count / total_count / excluded_count: 命中 / 原始 / 排除 记录数；
      - note: 诊断说明（命中为空时提示后端实际类型分布）。
    """
    params = {"beginDate": begin_date, "endDate": end_date,
              "pageNo": 1, "pageSize": 200, "dateType": 0}
    if warehouse_uuid:
        params["wareHouseUuid"] = warehouse_uuid  # pageStockOut 用大写 H
    agg = {}
    out_type_dist: dict = {}
    all_rows = []
    page = 1
    while True:
        params["pageNo"] = page
        try:
            d = client.page_stock_out(params)
        except Exception:
            break
        if not d.get("success"):
            break
        data = d.get("data") or {}
        rows = data.get("records") or data.get("list") or []
        if not rows:
            break
        for r in rows:
            code = str(r.get("stockOutType") or r.get("outType")
                       or r.get("type") or "")
            txt = str(r.get("typeText") or r.get("stockOutTypeName")
                      or r.get("outTypeName") or "")
            # 诊断用：以 code 或 typeText 标注分布
            key = f"{txt}({code})" if (txt or code) else "未知"
            out_type_dist[key] = out_type_dist.get(key, 0) + 1
            all_rows.append((code, txt, r))
        pages = data.get("pages", 1)
        if page >= pages or page >= 200:
            break
        page += 1

    # 精确统计：仅领料出库 + 采购越库
    picked = [(c, t, r) for (c, t, r) in all_rows if _is_lingliao_type(c, t)]
    note = ""
    if not picked and all_rows:
        note = (f"区间内未匹配到领料出库/采购越库记录，已按类型分布排除其余出库。"
                f"后端实际出库类型分布={out_type_dist}")
    elif picked:
        note = f"已按领料出库+采购越库统计（命中 {len(picked)}/{len(all_rows)} 条）"

    # 只查出库记录实际用到的商品 uuid 的营养（本地缓存，缺才调 details 接口）
    used_uuids = {r.get("goodsUuid") for (_, _, r) in picked if r.get("goodsUuid")}
    goods_map = get_goods_map(client, used_uuids)

    for c, t, r in picked:
        gu = r.get("goodsUuid")
        if not gu:
            continue
        q = _num(r.get("qty"))
        info = goods_map.get(gu) or {}
        # 分类：优先用 pageStockOut 记录自带的 firstCategoryName（第一分类名称）；
        # 该字段为空时（如采购越库记录），回退用商品主数据的 firstCategoryUuid 还原的分类名；
        # 仍无则「未分类」。
        cat = (r.get("firstCategoryName") or r.get("goodsFirstCategoryName")
               or info.get("category") or "未分类")
        # 营养优先级：商品主数据 nutri100 -> 出库记录自带的营养字段 -> 后续大模型/兜底表
        nutri100 = info.get("nutri100") or _extract_goods_nutri(r)
        a = agg.setdefault(gu, {
            "uuid": gu,
            "name": info.get("name") or r.get("goodsName") or "未知商品",
            "category": cat,
            "unit": r.get("unit") or info.get("unit", "") or "",
            "qty": 0.0,
            "records": 0,
            "nutri100": nutri100,
        })
        a["qty"] += q
        a["records"] += 1
    items = sorted(agg.values(), key=lambda x: x["qty"], reverse=True)
    # 折算克数
    for it in items:
        it["weight_g"], it["unit_exact"] = _to_grams(it["qty"], it["unit"])
        it["weight_g"] = round(it["weight_g"], 2)
    return {
        "items": items,
        "out_type_dist": out_type_dist,
        "picked_count": len(picked),
        "total_count": len(all_rows),
        "excluded_count": len(all_rows) - len(picked),
        "note": note,
    }


# ---------------------------------------------------------------------------
# 4) 营养值计算（模型估算每 100g，再按重量折算）
# ---------------------------------------------------------------------------
# 常见食材每 100g 营养兜底表（模型不可用/解析失败时用），量级参考中国食物成分表。
_FALLBACK_NUTRITION = {
    "大米": (346, 7.4, 0.8, 77.9), "米饭": (116, 2.6, 0.3, 25.9),
    "面粉": (349, 10.3, 1.1, 75.2), "面条": (110, 3.6, 0.2, 24.3),
    "猪肉": (395, 13.2, 37.0, 2.4), "五花肉": (508, 7.7, 53.0, 0),
    "牛肉": (125, 19.9, 4.2, 2.0), "羊肉": (203, 19.0, 14.1, 0),
    "鸡肉": (167, 19.3, 9.4, 1.3), "鸡胸肉": (133, 19.4, 5.0, 2.5),
    "鸭肉": (240, 15.5, 19.7, 0.2), "鸡蛋": (144, 13.3, 8.8, 2.8),
    "鱼": (113, 16.6, 5.2, 0), "草鱼": (113, 16.6, 5.2, 0),
    "虾": (93, 18.6, 0.8, 2.8), "豆腐": (81, 8.1, 3.7, 4.2),
    "豆干": (140, 16.2, 3.6, 11.5), "白菜": (17, 1.5, 0.1, 3.2),
    "大白菜": (17, 1.5, 0.1, 3.2), "青菜": (15, 1.4, 0.3, 2.4),
    "菠菜": (24, 2.6, 0.3, 4.5), "土豆": (77, 2.0, 0.2, 17.2),
    "西红柿": (20, 0.9, 0.2, 4.0), "番茄": (20, 0.9, 0.2, 4.0),
    "胡萝卜": (39, 1.0, 0.2, 8.8), "黄瓜": (16, 0.8, 0.2, 2.9),
    "茄子": (23, 1.1, 0.2, 4.9), "青椒": (22, 1.0, 0.2, 5.4),
    "洋葱": (40, 1.1, 0.2, 9.0), "冬瓜": (12, 0.4, 0.2, 2.6),
    "南瓜": (23, 0.7, 0.1, 5.3), "豆角": (30, 2.5, 0.2, 6.7),
    "西兰花": (36, 4.1, 0.6, 4.3), "菜花": (26, 2.1, 0.2, 4.6),
    "苹果": (54, 0.2, 0.2, 13.5), "香蕉": (93, 1.4, 0.2, 22.0),
    "橙子": (48, 0.8, 0.2, 11.1), "梨": (51, 0.3, 0.1, 13.1),
    "食用油": (899, 0, 99.9, 0), "菜籽油": (899, 0, 99.9, 0),
    "花生油": (899, 0, 99.9, 0), "盐": (0, 0, 0, 0),
    "酱油": (63, 5.6, 0.1, 10.1), "醋": (31, 2.1, 0.3, 4.9),
    "白糖": (400, 0, 0, 99.9), "姜": (46, 1.3, 0.6, 10.3),
    "蒜": (128, 4.5, 0.2, 27.6), "葱": (30, 1.7, 0.3, 6.5),
    "香菇": (26, 2.2, 0.3, 5.2), "木耳": (27, 1.5, 0.2, 6.0),
    "粉条": (337, 0.5, 0.1, 84.2), "馒头": (223, 7.0, 1.1, 47.0),
}
_DEFAULT_100G = (100.0, 5.0, 3.0, 12.0)  # 未知食材默认（千卡/蛋白/脂肪/碳水）


def _fallback_100g(name: str):
    for k, v in _FALLBACK_NUTRITION.items():
        if k in name:
            return v
    return _DEFAULT_100G


def compute_nutrition(llm, goods_items: list) -> dict:
    """对商品列表估算每 100g 营养值。

    优先级：① 商品主数据自带营养（nutri100，每 100g）> ② 内置食材营养兜底表
    （参考《中国食物成分表》量级，确定性、无需联网）> ③ 大模型估算（仅当显式开启
    NUTRITION_USE_LLM=1 时启用，默认关闭——营养以商品主数据为准，不依赖大模型猜测）。

    注意：默认不再调用大模型，因此报表生成不再有 ~数十秒的模型等待，速度显著更快。
    """
    if not goods_items:
        return {"items": [], "totals": {"energy_kcal": 0, "protein_g": 0,
                "fat_g": 0, "carb_g": 0}, "total_weight_g": 0,
                "note": "无领料出库商品", "model_based": False,
                "from_goods_count": 0}
    # 仅当显式开启且存在缺主数据营养的商品时，才走大模型（默认关闭）
    use_model = os.environ.get("NUTRITION_USE_LLM") == "1"
    need_llm = [g for g in goods_items if not g.get("nutri100")]
    llm_result = {}
    if use_model and need_llm and llm is not None:
        names = [g["name"] for g in need_llm]
        system = ("你是营养学专家，熟悉《中国食物成分表》。根据食物名称估算其"
                  "每100克可食部营养成分。只输出 JSON，不要任何其它文字。")
        user = (
            "请为以下食材估算每 100 克的营养值：\n" +
            json.dumps(names, ensure_ascii=False) +
            "\n返回格式（严格 JSON，勿输出其它内容）：\n"
            '{"goods":[{"name":"食材名","energy_kcal":0,"protein_g":0,"fat_g":0,"carb_g":0}]}'
        )
        try:
            r = llm.chat(system, user)
            text = r if isinstance(r, str) else r[0]
            data = _extract_json(text) or {}
            for g in (data.get("goods") or []):
                nm = (g.get("name") or "").strip()
                if nm:
                    llm_result[nm] = (_num(g.get("energy_kcal")),
                                      _num(g.get("protein_g")),
                                      _num(g.get("fat_g")),
                                      _num(g.get("carb_g")))
        except Exception:
            llm_result = {}

    items = []
    from_goods = 0  # 命中商品主数据自带营养的商品数
    for g in goods_items:
        name = g["name"]
        # 优先用商品主数据自带营养（nutri100，每 100g）；其次大模型；最后兜底表
        per100 = None
        n100 = g.get("nutri100")
        if n100:
            per100 = (n100.get("energy_kcal") or 0, n100.get("protein_g") or 0,
                      n100.get("fat_g") or 0, n100.get("carb_g") or 0)
            from_goods += 1
        elif use_model and name in llm_result:
            per100 = llm_result[name]
        else:
            per100 = _fallback_100g(name)
        items.append({
            "name": name,
            "category": g["category"],
            "unit": g["unit"],
            "qty": g["qty"],
            "weight_g": g["weight_g"],
            "unit_exact": g["unit_exact"],
            "energy_kcal": round(per100[0] * g["weight_g"] / 100.0, 1),
            "protein_g": round(per100[1] * g["weight_g"] / 100.0, 1),
            "fat_g": round(per100[2] * g["weight_g"] / 100.0, 1),
            "carb_g": round(per100[3] * g["weight_g"] / 100.0, 1),
        })
    items.sort(key=lambda x: x["energy_kcal"], reverse=True)
    totals = {
        "energy_kcal": round(sum(i["energy_kcal"] for i in items), 1),
        "protein_g": round(sum(i["protein_g"] for i in items), 1),
        "fat_g": round(sum(i["fat_g"] for i in items), 1),
        "carb_g": round(sum(i["carb_g"] for i in items), 1),
    }
    total_weight = round(sum(i["weight_g"] for i in items), 1)
    # 注记：说明营养来源优先级
    if from_goods == len(items):
        note = "营养值取自商品主数据（每 100g），非实验室检测值。"
    elif from_goods > 0:
        note = (f"营养值优先取自商品主数据（{from_goods}/{len(items)} 种），"
                f"其余由{'大模型估算' if use_model else '内置食材营养表'}；非实验室检测值。")
    elif use_model:
        note = ("营养值为大模型按食材估算，非实验室检测值，仅供参考。"
                "（未从商品主数据识别到营养字段，已回退大模型；"
                "如商品详情含营养字段，请告知字段名或贴一条商品记录）")
    else:
        note = ("营养值为内置食材营养表估算（参考《中国食物成分表》量级），"
                "非实验室检测值；未识别食材按默认值估算。"
                "（默认不调用大模型；如需模型估算可设置 NUTRITION_USE_LLM=1）")
    return {"items": items, "totals": totals, "total_weight_g": total_weight,
            "note": note, "model_based": use_model, "from_goods_count": from_goods}


# ---------------------------------------------------------------------------
# 5) 组装报表
# ---------------------------------------------------------------------------
def _resolve_warehouse_uuid(client, warehouse_uuid, warehouse_name):
    """按权限兜底解析 warehouseUuid：
    优先用调用方传入的 warehouse_uuid；若未传，调 query_warehouses 取当前用户
    可见的第一个仓库作为兜底，保证下游必传 uuid 的接口不会空指针。
    返回 (uuid, name, list_of_all_visible_warehouses)。
    """
    visible = []
    try:
        r = client.query_warehouses({})
        whs = extract_warehouses(r)
        for w in whs:
            visible.append({
                "uuid": w.get("uuid") or w.get("warehouseUuid") or "",
                "name": w.get("warehouseName") or w.get("name") or "",
            })
    except Exception:
        pass
    if warehouse_uuid:
        nm = warehouse_name or next((w["name"] for w in visible if w["uuid"] == warehouse_uuid), "")
        return warehouse_uuid, nm, visible
    if visible:
        return visible[0]["uuid"], warehouse_name or visible[0]["name"], visible
    return "", warehouse_name or "", visible


def build_nutrition_report(client, llm=None, begin_date: str = "",
                           end_date: str = "", warehouse_uuid: str | None = None,
                           warehouse_name: str | None = None) -> dict:
    """组装完整营养报表。llm 缺省时按环境自动获取（无密钥降级 Mock + 兜底表）。"""
    llm = llm or get_llm()
    # 仓库兜底：按用户权限（query_warehouses 即按 session 权限返回）取首个仓库作为兜底，
    # 避免下游 dish/menu/list 等必传 uuid 的接口报"仓库uuid不能为空"。
    warehouse_uuid, warehouse_name, visible_wh = _resolve_warehouse_uuid(
        client, warehouse_uuid, warehouse_name)
    if not warehouse_uuid:
        raise RuntimeError("当前账号无任何可见仓库，无法生成营养报表")

    menu = fetch_menu(client, begin_date, end_date, warehouse_uuid, warehouse_name)
    repast = fetch_repast_qty(client, begin_date, end_date, warehouse_uuid)
    goods = fetch_stock_out_by_goods(client, begin_date, end_date, warehouse_uuid)
    nutr = compute_nutrition(llm, goods["items"])

    # 领料出库商品分类占比（按折算重量），用 nutr.items（已带分类和营养值）
    cat_agg = defaultdict(float)
    for g in nutr["items"]:
        cat_agg[g["category"] or "未分类"] += g["weight_g"]
    cat_total = sum(cat_agg.values()) or 1.0
    cat_items = sorted(
        ({"name": k, "weight_g": round(v, 1),
          "ratio": round(v / cat_total * 100, 1)} for k, v in cat_agg.items()),
        key=lambda x: x["weight_g"], reverse=True)

    # 人均营养
    repast_total = repast.get("total")
    nutr_totals = nutr.get("totals") or {"energy_kcal": 0, "protein_g": 0,
                                         "fat_g": 0, "carb_g": 0}
    per_capita = None
    if repast_total:
        per_capita = {
            "energy_kcal": round(nutr_totals["energy_kcal"] / repast_total, 1),
            "protein_g": round(nutr_totals["protein_g"] / repast_total, 1),
            "fat_g": round(nutr_totals["fat_g"] / repast_total, 1),
            "carb_g": round(nutr_totals["carb_g"] / repast_total, 1),
        }

    return {
        "success": True,
        "begin_date": begin_date,
        "end_date": end_date,
        "menu": menu,
        "repast": repast,
        "stock_out": {
            "goods": nutr["items"],
            "out_type_dist": goods["out_type_dist"],
            "picked_count": goods["picked_count"],
            "total_count": goods["total_count"],
            "excluded_count": goods["excluded_count"],
            "note": goods["note"],
            "category_ratio": cat_items,
            "total_weight_g": round(cat_total, 1),
            "count": len(nutr["items"]),
        },
        "nutrition": nutr,
        "per_capita": per_capita,
        "dri": DRIS_2023_ADULT,
        "warehouse_uuid": warehouse_uuid,
        "warehouse_name": warehouse_name or menu.get("warehouse_name", ""),
        "visible_warehouses": [w["name"] for w in visible_wh if w["name"]],
    }
