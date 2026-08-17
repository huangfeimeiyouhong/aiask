# -*- coding: utf-8 -*-
"""营养分析报表：菜单 + 实际就餐人数 + 领料出库营养分析。

数据源（均为后厨管家开放接口，经 HCGClient 代理调用）：
  1) /hcgj-portal/api/dish/menu/list          菜单（带 /api，beginDate/endDate/warehouseUuid 必传）
  2) /hcgj-portal/cost/meals/queryDateGroupStat 实际就餐人数（不带 /api！前缀混用，实测路径）
  3) /hcgj-portal/api/wms/stock/pageStockOut   领料出库记录（按商品合并总重量）
  4) get_llm() 大模型按商品名估算每 100g 营养值 → 折算总营养 / 人均营养

口径：
  - 领料出库按业务口径包含采购越库（出库侧归入领料出库，与 metrics_registry 一致）。
  - 重量单位换算：公斤/斤/克 统一折算成克；无法识别单位时按 qty 当克估算并标注。
  - 营养值为模型估算值（参考《中国食物成分表》量级），仅供参考，非精确检测值。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from hunyuan import get_llm, MockLLM


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
    """递归扫描接口返回，提取 {date, qty} 结构（防御不同 schema）。"""
    if isinstance(x, dict):
        dte = x.get("date") or x.get("statDate") or x.get("mealDate")
        q = x.get("repastQty", x.get("qty", x.get("repastCount", x.get("count"))))
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


def _is_lingliao(t: str) -> bool:
    """判断出库类型是否为领料出库（含采购越库归入）。"""
    if not t:
        return False
    tl = t.lower()
    return any(k in t for k in ("领料", "越库")) or any(k in tl for k in ("material", "picking", "cross"))


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
def fetch_repast_qty(client, begin_date: str, end_date: str,
                     warehouse_uuid: str | None = None) -> dict:
    """获取实际就餐人数（按日期）。主接口失败自动回退 mealRecord/stat。"""
    result = {"dates": [], "total": None, "source": None, "note": ""}
    params = {"beginDate": begin_date, "endDate": end_date}
    if warehouse_uuid:
        params["warehouseUuid"] = warehouse_uuid

    def _try_call(fn):
        out = []
        try:
            d = fn(params)
            if d and d.get("success"):
                _walk_date_qty(d.get("data"), out)
        except Exception:
            pass
        return out

    rows = _try_call(client.meals_query_date_group_stat)
    if rows:
        result["dates"] = rows
        result["source"] = "queryDateGroupStat"
    else:
        rows = _try_call(client.meal_record_stat)
        if rows:
            result["dates"] = rows
            result["source"] = "mealRecord/stat"
        else:
            result["note"] = "就餐人数接口未返回数据"
    if rows:
        result["total"] = round(sum(r["qty"] for r in rows), 2)
    return result


# ---------------------------------------------------------------------------
# 3) 领料出库商品详情（按商品合并总重量）
# ---------------------------------------------------------------------------
def _build_category_map(client) -> dict:
    """分类 uuid -> name 映射（树形递归）。"""
    m = {}
    try:
        d = client.query_goods_category({})
        cats = (d.get("data") or []) if d.get("success") else []

        def walk(items):
            for c in items or []:
                u = c.get("uuid")
                if u:
                    m[u] = c.get("name") or ""
                walk(c.get("children") or c.get("childList") or [])

        walk(cats)
    except Exception:
        pass
    return m


def _build_goods_map(client) -> dict:
    """goodsUuid -> {name, category, unit}。"""
    m = {}
    try:
        d = client.query_goods({})
        goods = (d.get("data") or []) if d.get("success") else []
        cat_map = _build_category_map(client)
        for g in goods:
            gu = g.get("uuid")
            if not gu:
                continue
            m[gu] = {
                "name": g.get("goodsName") or g.get("name") or "未知商品",
                "category": cat_map.get(g.get("firstCategoryUuid"))
                            or g.get("firstCategoryName") or "未分类",
                "unit": g.get("standardUnit") or g.get("unit")
                        or g.get("goodsUnit") or "",
            }
    except Exception:
        pass
    return m


def fetch_stock_out_by_goods(client, begin_date: str, end_date: str,
                             warehouse_uuid: str | None = None) -> list:
    """拉取区间领料出库记录，按商品合并总重量。返回商品列表（按重量降序）。"""
    goods_map = _build_goods_map(client)
    params = {"beginDate": begin_date, "endDate": end_date,
              "pageNo": 1, "pageSize": 200, "dateType": 0}
    if warehouse_uuid:
        params["wareHouseUuid"] = warehouse_uuid  # pageStockOut 用大写 H
    agg = {}
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
            t = r.get("stockOutType") or r.get("outType") or ""
            if not _is_lingliao(str(t)):
                continue
            gu = r.get("goodsUuid") or r.get("goodsUuid")
            if not gu:
                continue
            q = _num(r.get("qty"))
            info = goods_map.get(gu) or {}
            a = agg.setdefault(gu, {
                "uuid": gu,
                "name": info.get("name") or r.get("goodsName") or "未知商品",
                "category": info.get("category", "未分类"),
                "unit": r.get("unit") or info.get("unit", "") or "",
                "qty": 0.0,
                "records": 0,
            })
            a["qty"] += q
            a["records"] += 1
        pages = data.get("pages", 1)
        if page >= pages or page >= 200:
            break
        page += 1
    items = sorted(agg.values(), key=lambda x: x["qty"], reverse=True)
    # 折算克数
    for it in items:
        it["weight_g"], it["unit_exact"] = _to_grams(it["qty"], it["unit"])
        it["weight_g"] = round(it["weight_g"], 2)
    return items


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
    """对商品列表估算每 100g 营养值（模型优先，兜底表保底）。"""
    if not goods_items:
        return {"items": [], "note": "无领料出库商品"}
    names = [g["name"] for g in goods_items]
    system = ("你是营养学专家，熟悉《中国食物成分表》。根据食物名称估算其"
              "每100克可食部营养成分。只输出 JSON，不要任何其它文字。")
    user = (
        "请为以下食材估算每 100 克的营养值：\n" +
        json.dumps(names, ensure_ascii=False) +
        "\n返回格式（严格 JSON，勿输出其它内容）：\n"
        '{"goods":[{"name":"食材名","energy_kcal":0,"protein_g":0,"fat_g":0,"carb_g":0}]}'
    )
    llm_result = {}
    use_model = True
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
    if not llm_result:
        use_model = False

    items = []
    for g in goods_items:
        name = g["name"]
        if use_model and name in llm_result:
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
    note = ("营养值为模型按食材估算（参考《中国食物成分表》量级），"
            "非实验室检测值；重量单位无法识别时按克估算。") if not use_model else \
           "营养值为大模型按食材估算，非实验室检测值，仅供参考。"
    return {"items": items, "totals": totals, "total_weight_g": total_weight,
            "note": note, "model_based": use_model}


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
        whs = (r.get("data") or []) if r.get("success") else []
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
    nutr = compute_nutrition(llm, goods)

    # 领料出库商品分类占比（按折算重量）
    cat_agg = defaultdict(float)
    for g in goods:
        cat_agg[g["category"] or "未分类"] += g["weight_g"]
    cat_total = sum(cat_agg.values()) or 1.0
    cat_items = sorted(
        ({"name": k, "weight_g": round(v, 1),
          "ratio": round(v / cat_total * 100, 1)} for k, v in cat_agg.items()),
        key=lambda x: x["weight_g"], reverse=True)

    # 人均营养
    repast_total = repast.get("total")
    per_capita = None
    if repast_total:
        per_capita = {
            "energy_kcal": round(nutr["totals"]["energy_kcal"] / repast_total, 1),
            "protein_g": round(nutr["totals"]["protein_g"] / repast_total, 1),
            "fat_g": round(nutr["totals"]["fat_g"] / repast_total, 1),
            "carb_g": round(nutr["totals"]["carb_g"] / repast_total, 1),
        }

    return {
        "success": True,
        "begin_date": begin_date,
        "end_date": end_date,
        "menu": menu,
        "repast": repast,
        "stock_out": {
            "goods": goods,
            "category_ratio": cat_items,
            "total_weight_g": round(cat_total, 1),
            "count": len(goods),
        },
        "nutrition": nutr,
        "per_capita": per_capita,
        "warehouse_uuid": warehouse_uuid,
        "warehouse_name": warehouse_name or menu.get("warehouse_name", ""),
        "visible_warehouses": [w["name"] for w in visible_wh if w["name"]],
    }
