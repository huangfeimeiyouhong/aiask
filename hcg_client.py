#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后厨管家（HouChuGuanJia）API 客户端 —— token 感知版

与 houchuguanjia_mcp/server.py 同一套接口契约，但改为「按调用方传入的
token / dataVersion」发起请求，支持多用户 Web 场景（每个会话用自己的账号
登录态，天然继承后厨管家的组织/仓库权限隔离）。

仅依赖标准库，零外部包。
"""

import json
import hashlib
from urllib.parse import urlencode
import urllib.request

# 后厨管家接口基地址：优先跟随 config.SETTINGS["HCG_BASE_URL"]（单一事实来源，
# 默认即测试环境地址），仅在脱离 ai_qa_system 独立使用时回退到下方字面量。
try:
    from config import SETTINGS as _CFG
    DEFAULT_BASE_URL = _CFG.get("HCG_BASE_URL", "http://hcgj-test-merchant.zou-yun.com/")
except Exception:
    DEFAULT_BASE_URL = "http://hcgj-test-merchant.zou-yun.com/"

import urllib.error

# 直连出站：后端是服务端调用后厨管家接口，绕过本地出口代理（沙箱代理可能不通
# 外部测试域名，导致登录/问数卡死）。已验证直连测试域名返回 200，更可靠。
_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ResponseTooLarge(Exception):
    """HTTP 响应体超过 max_bytes 阈值时抛出，用于避免把超大响应读入内存导致 OOM。

    典型场景：进销存库存快照等报表接口可能返回全公司库存（几万~十几万行），
    本地/沙箱内存有限，一次性读入会内存溢出（exit 137）。流式读取并在超阈值时
    立即中断连接，可优雅降级而非崩溃。
    """

    def __init__(self, size, max_bytes, path):
        self.size = size
        self.max_bytes = max_bytes
        self.path = path
        super().__init__(
            f"接口 {path} 响应过大（约 {size} 字节 > 上限 {max_bytes} 字节），已中断下载避免内存溢出")


def _norm(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return v


class HCGClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = None,
                 data_version: str = None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token
        self.data_version = data_version

    # ---- 内部请求 ----
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Access-Token"] = "m_" + self.token
        if self.data_version:
            headers["Data-Version"] = self.data_version
        return headers

    def _post(self, path: str, body: dict) -> dict:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers=self._headers())
        with _no_proxy_opener.open(req, timeout=90.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, params: dict | None = None, max_bytes: int | None = None,
             timeout: int | None = None) -> dict:
        url = self.base_url + path
        if params:
            clean = {k: _norm(v) for k, v in params.items() if v is not None}
            if clean:
                url = url + "?" + urlencode(clean, doseq=True)
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        with _no_proxy_opener.open(req, timeout=timeout or 90.0) as resp:
            if max_bytes:
                cl = resp.headers.get("Content-Length")
                if cl and int(cl) > max_bytes:
                    raise ResponseTooLarge(int(cl), max_bytes, path)
                chunks = []
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLarge(total, max_bytes, path)
                    chunks.append(chunk)
                body = b"".join(chunks)
            else:
                body = resp.read()
            return json.loads(body.decode("utf-8"))

    # ---- 登录（返回并保存 token / dataVersion）----
    def login(self, userName: str, password: str, userIdentity: str = "1",
              skip_md5: bool = False) -> dict:
        pw = password if skip_md5 else hashlib.md5(password.encode("utf-8")).hexdigest()
        data = self._post("/hcgj-portal/api/sso/login",
                          {"userName": userName, "password": pw,
                           "userIdentity": userIdentity})
        if not data.get("success"):
            return data
        d = data.get("data", {})
        self.token = d.get("token")
        self.data_version = d.get("dataVersion")
        return data

    def logout(self) -> dict:
        try:
            data = self._post("/hcgj-portal/api/sso/logout", {})
        finally:
            self.token = None
            self.data_version = None
        return data

    def verify_token(self) -> bool:
        """轻量鉴权探测：用当前 token 调一个廉价接口，确认 token 仍有效。

        用于 SSO 回调时校验跳转带过来的 token，避免伪造会话。
        返回 True 表示 token 有效（接口成功返回）；任何异常或 success=false 均返回 False。
        """
        try:
            r = self.query_warehouses({"pageNo": 1, "pageSize": 1})
        except Exception:
            return False
        if not isinstance(r, dict):
            return False
        return bool(r.get("success"))

    # ---- 查询类接口（与 server.py 保持一致）----
    def page_stock_in(self, params: dict | None = None) -> dict:
        return self._get("/hcgj-portal/api/wms/stock/pageStockIn", params)

    def page_stock(self, params: dict | None = None) -> dict:
        return self._get("/hcgj-portal/api/wms/stock/pageStock", params)

    def page_stock_out(self, params: dict | None = None) -> dict:
        return self._get("/hcgj-portal/api/wms/stock/pageStockOut", params)

    # ---- 菜单 / 就餐人数（营养报表）----
    def dish_menu(self, params: dict | None = None) -> dict:
        """查询菜单（按日期区间）。路径实测：带 /api。"""
        return self._get("/hcgj-portal/api/dish/menu/list", params)

    def meals_query_date_group_stat(self, params: dict | None = None) -> dict:
        """按日期获取实际就餐人数统计。路径实测：不带 /api（前缀混用，见记忆）。"""
        return self._get("/hcgj-portal/cost/meals/queryDateGroupStat", params)

    def meal_record_stat(self, params: dict | None = None) -> dict:
        """就餐统计（备用接口，含 repastQty 就餐人数）。"""
        return self._get("/hcgj-portal/api/repast/mealRecord/stat", params)


    def query_warehouses(self, params: dict | None = None) -> dict:
        """查询用户可见仓库列表。

        注意：filterType 是**仓库类型**过滤（store/distribution/branch/depart），
        不是用户权限过滤；session token 本身已做权限隔离。默认不传 filterType，
        以返回当前账号有权访问的全部仓库。若调用方明确需要只查某类仓库（如门店），
        可在 params 里显式传入 `filterType`。
        """
        p = dict(params or {})
        # 不再默认注入 filterType=store，因为仓库类型≠权限，误注会导致
        # 非门店类型仓库被过滤掉，触发"当前账号无任何可见仓库"假阴性。
        p.setdefault("pageNo", 1)
        p.setdefault("pageSize", 200)
        return self._get("/hcgj-portal/api/wms/com/queryWarehouses", p)

    def query_suppliers(self, params: dict | None = None) -> dict:
        return self._get("/hcgj-portal/api/wms/com/querySuppliers", params)

    def page_goods(self, params: dict | None = None) -> dict:
        """分页查询商品主数据（含营养字段）。路径实测：带 /api。

        营养报表用它读取商品每 100g 营养（能量/蛋白质/脂肪/碳水），
        避免回退大模型估算，提速且口径稳定。
        """
        p = dict(params or {})
        p.setdefault("pageNo", 1)
        p.setdefault("pageSize", 200)
        return self._get("/hcgj-portal/api/wms/com/pageGoods", p)

    def query_goods_category(self, params: dict | None = None) -> dict:
        return self._get("/hcgj-portal/api/wms/com/queryGoodsCategory", params)


def extract_warehouses(resp: dict | None) -> list:
    """从 queryWarehouses 响应里稳健提取仓库列表。

    该接口可能返回两种结构：① data 直接是 list；② data 是分页对象
    {"records":[...], "total":N} / {"list":[...]}。这里统一归一化，
    避免解析错把整页 dict 当成一条记录。
    """
    if not isinstance(resp, dict) or not resp.get("success"):
        return []
    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("records") or data.get("list") or []
    return []

    # ---- 报表统计（服务端聚合，金额准确，无需翻页估算）----
    def get_purchase_ledger(self, params: dict | None = None) -> dict:
        """采购台账：返回区间采购入库明细(details, 含 subtotal 真实小计) + 顶层汇总。"""
        return self._get("/hcgj-portal/api/wms/reportStat/getPurchaseLedger", params)

    def page_pur_stat(self, params: dict | None = None) -> dict:
        """采购统计(按天/按供应商)：顶层返回服务端算好的入库/越库/出库/结余金额与数量。"""
        return self._get("/hcgj-portal/api/wms/reportStat/pagePurStatDayOrSupplier", params)

    def page_stock_snapshot(self, params: dict | None = None, max_bytes: int | None = None,
                             timeout: int | None = None) -> dict:
        """进销存库存快照(指定日期)：顶层返回期初期末/采购入库/领料出库/盘盈盘亏等汇总 + 分页明细。

        max_bytes：响应体大小上限（字节），超过则抛 ResponseTooLarge 避免 OOM。
        timeout：本接口服务端计算较慢，可单独放宽超时（秒）。
        """
        return self._get("/hcgj-portal/api/wms/reportStat/pageStockSnapshotReport", params,
                         max_bytes=max_bytes, timeout=timeout)

    # ---- Phase 1：供应链管理扩展（供应商/配送/成本利润/退货/领料/申购）----

    def page_supplier_settle(self, params: dict | None = None) -> dict:
        """供应商采购结算统计：按客户(供应商)返回入库/结算/实退总金额合计。"""
        return self._get("/hcgj-portal/api/wms/purchaseBill/pageSupplierPurchaseSettleStatistics", params)

    def page_delivery_details_stat(self, params: dict | None = None) -> dict:
        """配送明细 + 履约状态统计：顶层返回待分拣/待发货/待验收/已验收数，records 为配送明细。"""
        return self._get("/hcgj-portal/api/dm/deliveryBill/pageDetailsAndStat", params)

    def profit_chart_stat(self, params: dict | None = None) -> dict:
        """利润图表统计（单类型）：date + dateType(1周2月3年) + type(1收入2支出) 必填。"""
        return self._get("/hcgj-portal/api/cost/profitChartStat", params)

    def page_purchase_return(self, params: dict | None = None) -> dict:
        """退货单分页查询：records 为退货单（含应退/实退金额、明细、财务状态）。"""
        return self._get("/hcgj-portal/api/wms/purchaseReturnBill/page", params)

    def page_picking_bill(self, params: dict | None = None) -> dict:
        """领料单分页查询：records 为领料单（含计划/实际出库金额、去向、状态）。"""
        return self._get("/hcgj-portal/api/wms/pickingBill/page", params)

    def page_apply_bill_count_status(self, params: dict | None = None) -> dict:
        """申购明细按状态数量统计：返回 已采购/待采购/已驳回 数量。"""
        return self._get("/hcgj-portal/api/wms/applyBill/countLineByStatus", params)

    def page_apply_bill(self, params: dict | None = None) -> dict:
        """申购单分页查询：records 为申购单（含总金额、状态、品项数）。"""
        return self._get("/hcgj-portal/api/wms/applyBill/page", params)

    # ---- Phase 2：食堂食安管理域（健康证/巡检/留样/晨检/检测报告/添加剂）----

    def page_health_certificate_stat(self, params: dict | None = None) -> dict:
        """健康证统计分页：顶层返回 normalQty/aboutToExpireQty/overdueQty/disableQty，
        records 为健康证明细（含 fullName/post/dueDate/status/warehouseNames）。"""
        return self._get("/hcgj-portal/api/wms/healthCertificate/pageAndStat", params)

    def inspect_page_stat(self, inspect_type: str, params: dict | None = None) -> dict:
        """食安巡检统计分页：inspect_type 取 day/week/month，分别调
        inspectDay/inspectWeek/inspectMonth 的 pageAndStat。
        顶层返回 auditedQty(已审核)/initialQty(待审核)，records 为巡检单
        （含 inspectDate/status/itemNcQty/itemQty/warehouseName/prodSituation）。"""
        path = {
            "day": "/hcgj-portal/api/fs/inspectDay/pageAndStat",
            "week": "/hcgj-portal/api/fs/inspectWeek/pageAndStat",
            "month": "/hcgj-portal/api/fs/inspectMonth/pageAndStat",
        }.get(inspect_type, "day")
        return self._get(path, params)

    def sample_count_by(self, params: dict | None = None) -> dict:
        """留样按类型计数：type 0待存入/1待取出/2留样中/3已取出；单值直接返回 long 数量。"""
        return self._get("/hcgj-portal/api/wms/sampleBill/countBy", params)

    def morning_check_page_stat(self, params: dict | None = None) -> dict:
        """晨检统计分页：顶层返回 qualifiedYesQty(合格)/qualifiedNoQty(不合格)/totalQty(在岗)，
        records 为晨检记录（含 checkTime/qualified/temperature/sick/type/post/warehouseName）。"""
        return self._get("/hcgj-portal/api/data/morningCheck/pageAndStat", params)

    def detection_page(self, params: dict | None = None) -> dict:
        """检测报告分页：records 为检测报告（含 checkDate/qualified(是否合格)/checkItem/
        goodsNames/supplierName/type(检测方式)/warehouseNames）。"""
        return self._get("/hcgj-portal/api/wms/detection/page", params)

    def food_additive_page(self, params: dict | None = None) -> dict:
        """食品添加剂分页：records 为添加剂使用记录（含 additiveName/usagePerKg(使用量)/
        standardUsagePerKg(标准使用量)/flourUsageKg(面粉用量)/remainingQty/status/warehouseName）。"""
        return self._get("/hcgj-portal/api/fs/foodAdditive/page", params)

    # ---- Phase 3：综合预警 + 环境设备告警 ------------------------------------
    def page_early_warn_stat(self, params: dict | None = None) -> dict:
        """综合预警中心（分页+统计）：records 为预警明细（含 category 分类 / status 状态 /
        content 内容 / warehouseName / createTime / startDate / endDate / handleList 处理记录）。
        顶层同时返回 waitRectifyQty(待整改) / rectifiedQty(已整改) / ignoreQty(已忽略) /
        confirmedQty(已确认)。category 取值：inquiry 询比价 / pricing 定价 / purchase 采购 /
        accept 验收 / dish 排菜 / fs 食安 / certificate 证照 / stock 仓储。"""
        return self._get("/hcgj-portal/data/earlyWarn/pageAndStat", params)

    def get_early_warn_stat_item(self, params: dict | None = None) -> dict:
        """食安预警聚合统计项：直接返回给定过滤条件下的四态合计（不含明细），
        字段 confirmedQty(已确认)/ignoreQty(已忽略)/rectifiedQty(已整改)/waitRectifyQty(待整改)。
        支持 category / startDate / endDate / status / type / warehouseUuidList / cycle / keyword 等。
        用于获取准确四态聚合（比从分页顶层读取更可靠，且能验证日期区间生效）。"""
        return self._get("/hcgj-portal/data/earlyWarn/getStatItem", params)

    # ---- Phase 5：报表体系（驾驶舱 / 采购价对比 / 库存月报 / 食安总览） ----
    def wms_report_index(self, params: dict | None = None) -> dict:
        """经营驾驶舱·进销存总览：返回今日采购金额(purchaseAmount)/验收金额(stockInAmount)/
        留样项数(sampleCount)，每项含 dayRatio(日同比%) / todayValue / yesterdayValue。无入参。
        注意：进销存指标卡在 /wms 命名空间（不是 /data，/data 的 getIndex 只返回食安晨检）。"""
        return self._get("/hcgj-portal/wms/reportStat/getIndex", params)

    def wms_report_wait_processed(self, params: dict | None = None) -> dict:
        """经营驾驶舱·待处理单据汇总：需 beginDate/endDate。返回 adjustBillCount 调整单数量 /
        applyCount 申购数量 / applyTotalAmount 申购金额 / flowBillCount 二级审核单数量 / purCount 采购数量 /
        purReturnCount 退货数量 / purReturnTotalAmount 退货金额 / purTotalAmount 采购金额。"""
        return self._get("/hcgj-portal/wms/reportStat/getWaitProcessedReport", params)

    def page_pur_price_compare(self, params: dict | None = None) -> dict:
        """采购价对比：records 为逐条采购明细对比，含 goodsName 商品 / goodsSpec 规格 / unit 单位 /
        warehouseName 仓库 / supplierName 供应商 / price 采购单价 / highPrice 平台价格 /
        outOfProp 超出比例(%) / hasStockInQty 入库数量 / deliveryTime 发货时间 / dataSource 数据来源。
        支持 beginDate/endDate/warehouseUuidList/orderBy(如 outOfProp_desc)/pageNo/pageSize。"""
        return self._get("/hcgj-portal/wms/reportStat/pagePurPriceCompare", params)

    def page_stock_month_report(self, params: dict | None = None) -> dict:
        """库存月报：需 reportDate(月报日期 yyyy-MM-dd，取当月首日)。records 为按商品汇总的当月
        进销存金额/数量（服务端已聚合，金额准确非估算）：purchaseInAmount/Qty 采购入库、purchaseCrossInAmount/Qty 采购越库、
        pickingOutAmount/Qty 领料出库、stockInAmount/Qty 入库、stockOutAmount/Qty 出库、stockAmount 期末金额、stockQty 期末数量、
        beginStockAmount/Qty 期初，另含 goodsName/spec/unit/warehouseName/firstCategoryName。
        支持 goodsCategoryUuid/keyword/level/warehouseUuid/warehouseUuidList/pageNo/pageSize。"""
        return self._get("/hcgj-portal/wms/reportStat/pageStockMonthReport", params)

    def data_report_index(self, params: dict | None = None) -> dict:
        """食安驾驶舱·总览：返回今日晨检人数(morningCheckCount) 含 dayRatio/todayValue/yesterdayValue。无入参。"""
        return self._get("/hcgj-portal/data/reportStat/getIndex", params)

    def data_report_overview(self, params: dict | None = None) -> dict:
        """食安概况：返回概况项列表，每项 {name 名称, status 状态}（如晨检/巡检/消杀/培训等模块执行概况）。
        可选 warehouseUuid 过滤。"""
        return self._get("/hcgj-portal/data/reportStat/getOverviewData", params)

    # ---- 排菜管理 DISH（P1：排菜成本率 / 出品口碑 / 营养 NRV）----
    def dish_menu_list(self, params: dict | None = None) -> dict:
        """排菜菜单列表（聚合对象，非扁平分页）：必填 beginDate/endDate/warehouseUuid；
        isComment=true 时返回菜品评价。返回 data=DishMenuListVo：
          - costTotal 成本合计 / costRatio 成本占比(字符串) / mealTotal 餐标合计
          - dateDetails[]: {date, costPriceTotal, dishDetails[]}
          - dishDetails[]: dishesName/categoryName/costPrice/costRatio/commentCount/scoreCount/
            menuUuid/stdExpAmount(标准伙食费)/stdRepastQty(计划就餐人数)/meals/mealStand/qty
          - dishDetails[].dishDishesRecipeDetails[].goodsNutritionDto 含逐原料营养（dbzG蛋白质等）
        注意：warehouseUuid 必填，无仓库维度时需按仓库逐个调用后合并。"""
        return self._get("/hcgj-portal/api/dish/menu/list", params)

    def dish_menu_nutrition(self, params: dict | None = None) -> dict:
        """单菜单营养 NRV：必填 uuid(菜单uuid)。返回 data=DishMenuNutritionVo，
        含能量/蛋白质/脂肪/碳水/钠/钙/铁/锌/各维生素等 原始值 + NRV + Rate(占比%)，
        字段如 nlKcal能量、dbzG蛋白质、zfG脂肪、tshhwG碳水、naMg钠、gaiMg钙、
        nlKcalNrv能量NRV、dbzGNrv蛋白质NRV、dbzGRate蛋白质占比 等。"""
        return self._get("/hcgj-portal/api/dish/menu/nutrition", params)

    # ---- 询比价 PMS（P1：询比价成效）----
    def pms_quote_bill_page(self, params: dict | None = None) -> dict:
        """报价单分页（询比价成效）：records 为报价单 PmsQuoteBillPageVo，
        status 1待报价 2已报价；isClose 是否截止；matCount 品项数 / quoteMatCount 已报价品项数；
        amount 总金额；inquiryBillNo 询价单单号；supplierName 供应商；type 1采购 2定价。
        支持 beginDate/endDate/status/type/isClose/isOpen/keyword 过滤。"""
        return self._get("/hcgj-portal/api/pms/quoteBill/page", params)

    def get_third_device_warn_target(self, params: dict | None = None) -> dict:
        """环境设备告警指数：返回各类告警的累计总数（data 对象）
        tempWarnTotal 温度 / humWarnTotal 湿度 / smokeWarnTotal 烟雾 / gasWarnTotal 燃气 /
        floodWarnTotal 水浸 / aiWarnTotal AI巡检 / dataLineMap 设备告警。可按 warehouseUuid 过滤。"""
        return self._get("/hcgj-portal/api/data/thirdDeviceWarn/getTarget", params)

    def page_third_device_warn(self, params: dict | None = None) -> dict:
        """环境设备告警明细分页：records 为设备告警（含 warnType/warnValue/warnContent/
        status 0未处理 1已处理 2已忽略 / typeText 设备类型 / warehouseName / warnTime /
        eviUrl 取证URL / result 结果记录）。支持 beginDate/endDate、status、warehouseUuid、appType。"""
        return self._get("/hcgj-portal/api/data/thirdDeviceWarn/page", params)


if __name__ == "__main__":
    # 快速自检：用环境变量 HCG_USER/HCG_PWD 测试登录
    import os
    c = HCGClient()
    r = c.login(os.environ.get("HCG_USER", "at0001"),
                os.environ.get("HCG_PWD", "at123456@"))
    print(json.dumps(r, ensure_ascii=False, indent=2)[:500])
