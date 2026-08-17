# -*- coding: utf-8 -*-
"""营养报表核心逻辑单测（mock client + mock llm，无需网络）。"""
import json
import sys

import nutrition_report as nr


class FakeLLM:
    """返回固定 JSON 的假 LLM。"""
    def chat(self, system, user):
        return json.dumps({"goods": [
            {"name": "猪肉", "energy_kcal": 395, "protein_g": 13.2, "fat_g": 37.0, "carb_g": 2.4},
            {"name": "白菜", "energy_kcal": 17, "protein_g": 1.5, "fat_g": 0.1, "carb_g": 3.2},
        ]}, ensure_ascii=False)


class FakeClient:
    """最小可用的假 HCGClient（只实现营养报表用到的接口）。"""
    def __init__(self):
        self.base_url = "http://test.local/"

    def dish_menu(self, params=None):
        return {"success": True, "data": {
            "beginDate": params.get("beginDate"), "endDate": params.get("endDate"),
            "warehouseName": "测试仓",
            "dateDetails": [{
                "date": "2026-08-17", "week": "星期一", "status": "audited",
                "stdRepastQty": 500, "mealStandTotal": 20,
                "dishDetails": [
                    {"dishesName": "红烧肉", "img": "/img/a.jpg", "meals": "lunch",
                     "categoryName": "荤菜", "mealStand": 5, "qty": 1,
                     "isRec": True, "scoreCount": 4.5, "tags": ["招牌"],
                     "dishDishesRecipeDetails": [
                         {"goodsName": "猪肉"}, {"goodsName": "白糖"}]},
                    {"dishesName": "清炒白菜", "img": "", "meals": "lunch",
                     "categoryName": "素菜", "mealStand": 3, "qty": 1,
                     "isRec": False, "scoreCount": 0, "tags": [],
                     "dishDishesRecipeDetails": [{"goodsName": "白菜"}]},
                ],
            }],
        }}

    def meals_query_date_group_stat(self, params=None):
        return {"success": True, "data": {
            "dateDetails": [{"date": "2026-08-17", "repastQty": 480}]}}

    def meal_record_stat(self, params=None):
        return {"success": False, "message": "fallback"}

    def query_goods_category(self, params=None):
        return {"success": True, "data": [
            {"uuid": "c1", "name": "肉禽蛋", "children": []},
            {"uuid": "c2", "name": "蔬菜", "children": []}]}

    def query_goods(self, params=None):
        return {"success": True, "data": [
            {"uuid": "g1", "goodsName": "猪肉", "firstCategoryUuid": "c1", "standardUnit": "斤"},
            {"uuid": "g2", "goodsName": "白菜", "firstCategoryUuid": "c2", "standardUnit": "斤"}]}

    def page_stock_out(self, params=None):
        if params.get("pageNo", 1) > 1:
            return {"success": True, "data": {"records": [], "pages": 1}}
        return {"success": True, "data": {"records": [
            {"goodsUuid": "g1", "stockOutType": "materialOut", "qty": 10, "unit": "斤"},
            {"goodsUuid": "g2", "stockOutType": "领料出库", "qty": 20, "unit": "斤"},
            {"goodsUuid": "g1", "stockOutType": "returnOut", "qty": 999, "unit": "斤"},  # 非领料，应剔除
        ], "pages": 1}}


passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {label}")
    else:
        failed += 1
        print(f"  ✗ {label}  {detail}")


def main():
    print("T1 菜单解析（图片/名称/餐次/原料/分类）")
    menu = nr.fetch_menu(FakeClient(), "2026-08-17", "2026-08-17", "w1")
    dd = menu["date_details"][0]
    check("日期与星期", dd["date"] == "2026-08-17" and dd["week"] == "星期一")
    check("2 道菜", len(dd["dishes"]) == 2)
    d0 = dd["dishes"][0]
    check("菜名", d0["dishesName"] == "红烧肉")
    check("图片原样透传", d0["img"] == "/img/a.jpg")
    check("餐次", d0["meals"] == "lunch")
    check("原料", d0["recipe"] == ["猪肉", "白糖"])
    check("分类", d0["categoryName"] == "荤菜")
    check("仓库名", menu["warehouse_name"] == "测试仓")

    print("T2 就餐人数")
    rp = nr.fetch_repast_qty(FakeClient(), "2026-08-17", "2026-08-17")
    check("取到 480", rp["total"] == 480, str(rp))
    check("来源 queryDateGroupStat", rp["source"] == "queryDateGroupStat")

    print("T3 领料出库按商品合并（剔除非领料）")
    goods = nr.fetch_stock_out_by_goods(FakeClient(), "2026-08-17", "2026-08-17", "w1")
    check("2 个商品", len(goods) == 2, str(goods))
    by_name = {g["name"]: g for g in goods}
    check("猪肉 10 斤→5000g", by_name["猪肉"]["weight_g"] == 5000.0)
    check("白菜 20 斤→10000g", by_name["白菜"]["weight_g"] == 10000.0)
    check("猪肉分类 join 肉禽蛋", by_name["猪肉"]["category"] == "肉禽蛋")
    check("白菜分类 join 蔬菜", by_name["白菜"]["category"] == "蔬菜")

    print("T4 营养计算（LLM 每100g × 重量折算）")
    items = nr.compute_nutrition(FakeLLM(), goods)["items"]
    by_name2 = {i["name"]: i for i in items}
    # 猪肉：395kcal/100g × 5000g/100 = 19750 kcal
    check("猪肉能量 19750", abs(by_name2["猪肉"]["energy_kcal"] - 19750.0) < 0.01,
          str(by_name2["猪肉"]))
    # 白菜：17kcal/100g × 10000g/100 = 1700 kcal
    check("白菜能量 1700", abs(by_name2["白菜"]["energy_kcal"] - 1700.0) < 0.01,
          str(by_name2["白菜"]))
    totals = nr.compute_nutrition(FakeLLM(), goods)["totals"]
    check("总能量 21450", abs(totals["energy_kcal"] - 21450.0) < 0.1, str(totals))
    check("总蛋白", abs(totals["protein_g"] - (13.2 * 50 + 1.5 * 100)) < 0.1, str(totals))

    print("T5 报表组装（含人均 + 分类占比）")
    rep = nr.build_nutrition_report(FakeClient(), FakeLLM(), "2026-08-17", "2026-08-17", "w1")
    check("success", rep["success"] is True)
    check("人均能量 21450/480≈44.7", abs(rep["per_capita"]["energy_kcal"] - 44.7) < 0.1,
          str(rep["per_capita"]))
    cat = {c["name"]: c for c in rep["stock_out"]["category_ratio"]}
    check("分类占比 肉禽蛋 33.3%", abs(cat["肉禽蛋"]["ratio"] - 33.3) < 0.2, str(cat))
    check("总重量 15000g", rep["stock_out"]["total_weight_g"] == 15000.0)
    check("营养估算注记", bool(rep["nutrition"]["note"]))

    print("T6 兜底：LLM 返回垃圾时用内置表")
    class BadLLM:
        def chat(self, s, u):
            return "我不是JSON"
    items2 = nr.compute_nutrition(BadLLM(), [{"name": "猪肉", "category": "x", "unit": "斤",
                                              "qty": 1, "weight_g": 500.0, "unit_exact": True}])["items"]
    check("猪肉兜底 395kcal/100g×500g=1975", abs(items2[0]["energy_kcal"] - 1975.0) < 0.1,
          str(items2[0]))
    check("model_based=False", nr.compute_nutrition(BadLLM(), [{
        "name": "猪肉", "category": "x", "unit": "斤", "qty": 1, "weight_g": 500.0,
        "unit_exact": True}])["model_based"] is False)

    print("T7 仓库 uuid 兜底（按用户权限自动取首个）")

    class NoWhClient(FakeClient):
        def dish_menu(self, params=None):
            # 验证：调菜单接口时 warehouseUuid 已被兜底成有权限仓库的 uuid
            check("dish_menu 收到 warehouseUuid", bool(params.get("warehouseUuid")),
                  str(params))
            return super().dish_menu(params)
        def query_warehouses(self, params=None):
            return {"success": True, "data": [
                {"uuid": "auto-wh-1", "warehouseName": "自动兜底仓"}]}

    # 场景1：不传 warehouseUuid —— 应自动用 query_warehouses 返回的第一个仓库
    rep = nr.build_nutrition_report(NoWhClient(), FakeLLM(), "2026-08-17", "2026-08-17",
                                    warehouse_uuid="", warehouse_name="")
    check("自动取首个仓库 uuid", rep["warehouse_uuid"] == "auto-wh-1", rep["warehouse_uuid"])
    check("自动取首个仓库 name", rep["warehouse_name"] == "自动兜底仓", rep["warehouse_name"])
    check("返回可见仓库列表", rep["visible_warehouses"] == ["自动兜底仓"])

    # 场景2：显式传 warehouseUuid —— 应原样透传（不走兜底）
    class WithWhClient(FakeClient):
        def dish_menu(self, params=None):
            check("显式 uuid 透传", params.get("warehouseUuid") == "explicit-wh")
            return super().dish_menu(params)
        def query_warehouses(self, params=None):
            return {"success": True, "data": []}
    rep2 = nr.build_nutrition_report(WithWhClient(), FakeLLM(), "2026-08-17", "2026-08-17",
                                     warehouse_uuid="explicit-wh", warehouse_name="指定仓")
    check("显式 uuid 透传", rep2["warehouse_uuid"] == "explicit-wh")

    # 场景3：当前账号无任何仓库 —— 应明确报错
    class EmptyWhClient(FakeClient):
        def query_warehouses(self, params=None):
            return {"success": True, "data": []}
    raised = False
    try:
        nr.build_nutrition_report(EmptyWhClient(), FakeLLM(), "2026-08-17", "2026-08-17")
    except RuntimeError as e:
        raised = True
        check("无仓库时报错", "无任何可见仓库" in str(e), str(e))
    check("无仓库抛 RuntimeError", raised)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
