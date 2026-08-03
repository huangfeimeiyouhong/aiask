#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后厨管家 AI 问数 · 业务口径注册表（Metrics Caliber Registry）—— 单一事实来源（SSOT）

把所有易踩坑的业务口径、35 个工具的元数据集中定义，工具描述文案与 LLM 系统提示词口径说明
统一从这里生成，避免口径散落在 semantic_tools.TOOL_SCHEMAS、semantic_layer.GLOSSARY、
各工具函数内部三处导致「A 工具含越库、B 工具漏了」之类的漂移 bug。

分层：
  ── 全局口径（GLOBAL_CALIBERS）：跨多个工具生效的通用规则（越库双向、库存零剔、金额估算…）
  ── 工具口径（METRICS）：每个工具的 label/domain/source_interface/measures/dimensions/
     fixed_filters/formula/exclusions/date_boundary/aliases/scope_note/description
  ── 生成器：build_caliber_spec() 渲染给 LLM 的口径说明；normalize_out_type() 供代码引用
"""

# ===========================================================================
# 一、采购入库口径（越库也是采购）
# ===========================================================================
# 默认（采购含越库）：入库侧同时统计采购入库与采购越库
PURCHASE_INBOUND_TYPES = ("purchaseIn", "purchaseCrossIn")
# 仅采购入库（用户明确「不含越库/只要进库的」）
ONLY_INBOUND_TYPES = ("purchaseIn",)

# ===========================================================================
# 二、库存口径（剔除无效库存）
# ===========================================================================
# 服务端过滤参数：False 表示过滤掉 qty<=0 的无效库存（接口层直接剔除，不占传输/内存）
INVENTORY_ZERO_QTY = False
# 客户端兜底阈值：库存数量 <= 此值视为无效，剔除（与 INVENTORY_ZERO_QTY 同一口径）
INVENTORY_VALID_QTY_MIN = 0

# ===========================================================================
# 三、金额口径（估算）
# ===========================================================================
# 金额 = 单价 × 数量（接口无金额/小计字段，故为估算值）
AMOUNT_EST_FORMULA = "单价(price) × 数量(qty)"
AMOUNT_EST_NOTE = "amount_est = 单价×数量 的估算值；不同计量单位不可跨单位直接相加。"

# ===========================================================================
# 四、出库类型归一化（采购越库在出库侧归一为「领料出库」）
# ===========================================================================
# 含这些关键字的出库类型，在【出库】侧统一归并为「领料出库」（与入库侧「越库=采购」对应）
OUT_TYPE_CROSS_KEYWORDS = ("越库", "cross")


def normalize_out_type(raw):
    """出库类型归一化：采购越库在【出库】侧按业务口径归入「领料出库」，不再单列。

    与 semantic_tools._out_type_label 行为完全一致，集中在此作为 SSOT，供代码引用。
    """
    if not raw:
        return "未知"
    s = raw if isinstance(raw, str) else str(raw)
    if any(k in s for k in OUT_TYPE_CROSS_KEYWORDS):
        return "领料出库"
    return s


# ===========================================================================
# 五、食安/预警日期口径（按推送日期查询）
# ===========================================================================
# True：food_safety_alert 按推送日期(创建时间 createTime)查询，用 startDate/endDate。
#   beginDate/endDate 会被该接口忽略，导致返回全量历史。
# 注意：warning_center 当前仍用 startDate/endDate 业务周期口径（非推送日期）；
#   若用户要求食安类也走推送日期，需同步改 warning_center（见 MEMORY 口径约定）。
FOOD_SAFETY_USE_PUSH_DATE = True

# ===========================================================================
# 六、全局口径字典（GLOBAL_CALIBERS）—— 跨工具生效的通用规则
# ===========================================================================
GLOBAL_CALIBERS = [
    {
        "key": "purchase_cross_in",
        "text": (
            "采购越库双向口径：采购越库(purchaseCrossIn)在【入库】侧记为采购入库的一部分，"
            "因此采购入库类工具默认同时统计 purchaseIn 与 purchaseCrossIn；"
            "在【出库】侧则归入「领料出库」，不在出库按类型拆分中单列\"采购越库\"。"
        ),
    },
    {
        "key": "inventory_zero_qty",
        "text": (
            "库存口径：库存数量为 0 的记录是无效脏数据，必须剔除"
            "（服务端 zeroQty=False 过滤 + 客户端 qty<=0 兜底）。"
        ),
    },
    {
        "key": "amount_est",
        "text": (
            "金额估算口径：接口无金额/小计字段时，金额 = 单价(price) × 数量(qty) 的估算值，"
            "回答须注明\"估算\"；不同计量单位不可跨单位直接相加。"
        ),
    },
    {
        "key": "unit_not_addable",
        "text": (
            "计量单位：斤/只/件/公斤/克/份等，跨单位的数量不可直接相加，"
            "涉及数量汇总须说明按单位分别统计。"
        ),
    },
    {
        "key": "count_def",
        "text": "笔数：一条入库/出库记录算一笔（一单）。",
    },
    {
        "key": "server_side_accurate",
        "text": (
            "金额准确性优先级：服务端聚合工具（purchase_stat / purchase_ledger / stock_snapshot / "
            "supplier_settlement / delivery_fulfillment / cost_profit / purchase_return / picking_out / "
            "stock_month_report 等）金额由服务端真实返回，准确非估算，应优先用于金额统计类问题；"
            "旧翻页估算工具（purchase_inbound_summary / rank_by_dimension / daily_trend / "
            "inventory_by_* / stock_out_by_warehouse / purchase_price_compare）金额为单价×数量估算，"
            "仅在聚合工具无法覆盖的极细粒度场景使用，且须注明\"估算\"。"
        ),
    },
    {
        "key": "food_safety_push_date",
        "text": (
            "食安/预警日期口径：food_safety_alert 与 warning_center 按【推送日期(创建时间 createTime)】"
            "查询，使用 startDate/endDate 参数（系统推送/生成预警的时间窗口）；不要用 beginDate/endDate"
            "（会被忽略，导致返回全量历史）。注意 warning_center 当前仍用 startDate/endDate 业务周期口径——"
            "若用户也要求食安类走推送日期，需同步改 warning_center。"
        ),
    },
    {
        "key": "cost_profit_org",
        "text": (
            "成本利润(cost_profit)为组织级口径，无仓库过滤；其余带 warehouse_name 的工具均支持"
            "按仓库筛选（模糊匹配，含子串即可），不传则统计全部仓库。"
        ),
    },
    {
        "key": "inventory_snapshot_no_date",
        "text": (
            "库存时点快照口径：inventory_by_warehouse / inventory_by_category / stock_warning 是时点数据，"
            "不传日期；stock_snapshot 用 report_date 指定某天时点快照（非区间）。"
        ),
    },
    {
        "key": "warehouse_uuid_case",
        "text": (
            "仓库过滤参数大小写：采购/入库/出库接口用 wareHouseUuid（大写 H），库存接口用 warehouseUuid"
            "（小写 h），系统已统一处理，模型只需给仓库名即可。"
        ),
    },
]

# 兼容旧引用：CALIBER_NOTES 由 GLOBAL_CALIBERS 派生（key→text）
CALIBER_NOTES = {c["key"]: c["text"] for c in GLOBAL_CALIBERS}


def build_caliber_spec():
    """渲染给 LLM 的「业务口径统一说明」权威块（语义层对齐用）。

    返回纯文本，每行一条全局口径；system prompt 注入时作为最高优先级口径依据。
    """
    lines = ["【业务口径统一说明（以下口径以口径注册表为准，优先级高于上方通用说明）】"]
    for c in GLOBAL_CALIBERS:
        lines.append(f"- {c['text']}")
    return "\n".join(lines)


# ===========================================================================
# 七、工具口径注册表（METRICS）—— 35 个工具的结构化元数据
# ===========================================================================
# 字段说明：
#   label            中文名（同 TOOL_LABELS）
#   domain           业务域 purchase/inventory/outbound/supply_chain/food_safety/
#                    warning/dashboard/report/dish/inquiry
#   source_interface 对应 HCGClient 方法（数据真实来源，可追溯）
#   measures         指标种类 amount_est/qty/count/income/expense/profit/status_count ...
#   dimensions       可分析维度 warehouse/category(goods_first_category)/supplier/goods/time
#   fixed_filters    固化口径（如入库类型、日期口径）；None 表示无
#   formula          金额/数量计算规则；None 表示无（计数或接口直出）
#   exclusions       剔除规则；None 表示无
#   date_boundary    日期边界 range(区间)/point(时点)/none(无日期)/optional(可选)
#   aliases          自然语言触发词（供后续意图召回/P2 使用）
#   scope_note       权限/组织隔离说明
#   description      给 LLM 的 function-calling 描述（与 TOOL_SCHEMAS 完全一致，单一来源）
METRICS = {
    "purchase_inbound_summary": {
        "label": "采购入库汇总",
        "domain": "purchase",
        "source_interface": "HCGClient.page_stock_in",
        "measures": ["amount_est", "qty", "count"],
        "dimensions": ["warehouse", "supplier", "unit"],
        "fixed_filters": {"stock_in_types": list(PURCHASE_INBOUND_TYPES)},
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["采购", "采购数据", "采购入库", "买了多少", "入库汇总"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """查询某时间段内【采购数据】的汇总：入库笔数、估算总金额、合计数量（按单位拆分）。越库(purchaseCrossIn)也是一种采购，默认同时计入采购入库与采购越库；仅当用户明确说"只要进库的/不含越库"时才将 only_inbound 设为 true。""",
    },
    "rank_by_dimension": {
        "label": "维度排行",
        "domain": "purchase",
        "source_interface": "HCGClient.page_stock_in + query_goods_category",
        "measures": ["amount_est", "qty", "count"],
        "dimensions": ["goods", "goods_category", "warehouse", "supplier"],
        "fixed_filters": {"stock_in_types": list(PURCHASE_INBOUND_TYPES)},
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["排行", "TOP", "分类分析", "品类占比", "采购分类"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """按维度（goods商品 / goods_category商品一级分类 / warehouse仓库 / supplier供应商）对【采购数据】做金额/数量/笔数排行，返回 TOP N。当用户问"采购入库 商品分类分析/各分类采购金额/采购品类占比"时，必须选 dimension='goods_category'。默认采购含越库；仅当用户明确说"只要进库的/不含越库"时才将 only_inbound 设为 true。""",
    },
    "daily_trend": {
        "label": "按日趋势",
        "domain": "purchase",
        "source_interface": "HCGClient.page_stock_in",
        "measures": ["amount_est", "qty", "count"],
        "dimensions": ["time"],
        "fixed_filters": {"stock_in_types": list(PURCHASE_INBOUND_TYPES)},
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["趋势", "走势", "按日", "每天变化"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """按日统计【采购数据】的【金额/数量/笔数】趋势序列，用于看随时间变化。默认采购含越库；仅当用户明确说"只要进库的/不含越库"时才将 only_inbound 设为 true。""",
    },
    "stock_warning": {
        "label": "库存预警",
        "domain": "inventory",
        "source_interface": "HCGClient.page_stock（库存） + 业务计算",
        "measures": ["qty", "count"],
        "dimensions": ["warehouse", "goods", "category"],
        "fixed_filters": {"zeroQty": INVENTORY_ZERO_QTY},
        "formula": None,
        "exclusions": "库存数量为 0 的无效记录已剔除（qty<=0 剔除）",
        "date_boundary": "none",
        "aliases": ["库存预警", "临期", "过期", "快到期", "库存分析"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """查询库存情况（临期/过期预警、当前库存分析）：返回已过期数量、临期预警中数量，及样例明细。""",
    },
    "inventory_by_warehouse": {
        "label": "库存按仓库汇总",
        "domain": "inventory",
        "source_interface": "HCGClient.page_stock",
        "measures": ["amount_est", "qty", "count"],
        "dimensions": ["warehouse"],
        "fixed_filters": {"zeroQty": INVENTORY_ZERO_QTY},
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": "库存数量为 0 的无效记录已剔除（qty<=0 剔除）",
        "date_boundary": "none",
        "aliases": ["各仓库库存", "仓库库存分布", "库存按仓库"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """查询【当前库存】商品按【仓库】分类汇总：每个仓库的商品种类数、合计数量、估算金额。这是库存时点快照，无需日期范围；可按仓库名称筛选。用于回答"各仓库库存了多少/各仓库存了什么/库存按仓库分布"。""",
    },
    "inventory_by_category": {
        "label": "库存分类占比",
        "domain": "inventory",
        "source_interface": "HCGClient.page_stock + query_goods_category",
        "measures": ["amount_est", "qty", "count", "qty_ratio"],
        "dimensions": ["goods_category"],
        "fixed_filters": {"zeroQty": INVENTORY_ZERO_QTY},
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": "库存数量为 0 的无效记录已剔除（qty<=0 剔除）；分类名来自商品分类树，库存记录本身分类名为空仅含 uuid",
        "date_boundary": "none",
        "aliases": ["库存分类占比", "各分类库存", "库存按分类", "分类库存"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离；仅用于“当前/现有库存”语境的分类占比",
        "description": """查询【当前库存】商品按【一级商品分类】分类汇总与占比：每个分类的商品种类数、合计数量、估算金额，及数量占比(qty_ratio)。分类名来自商品分类树(queryGoodsCategory)，库存数量为 0 的无效记录已剔除。用于回答"库存分类占比/各分类库存多少/哪些分类库存最多/按分类看库存"。""",
    },
    "purchase_inbound_by_warehouse": {
        "label": "采购入库按仓库汇总",
        "domain": "purchase",
        "source_interface": "HCGClient.page_stock_in",
        "measures": ["amount_est", "qty", "count"],
        "dimensions": ["warehouse"],
        "fixed_filters": {"stock_in_types": list(PURCHASE_INBOUND_TYPES)},
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["各仓库采购", "按仓库看采购", "进货按仓库"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """查询某时间段内【采购入库】按【仓库】分类汇总：每个仓库的入库笔数、合计数量、估算金额。口径：采购入库含采购越库(purchaseCrossIn)——越库在入库侧即记为采购入库，因此本工具默认同时统计 purchaseIn 与 purchaseCrossIn。用于回答"各仓库采购入库多少/各仓库进货多少/按仓库看采购"。【限制】必须指定具体仓库（warehouse_name 必填，禁止跨全部仓库全量拉取以免打爆后端数据库）；查询区间最长 1 个月（≤31 天）。""",
    },
    "stock_out_by_warehouse": {
        "label": "出库按仓库汇总",
        "domain": "outbound",
        "source_interface": "HCGClient.page_stock_out",
        "measures": ["amount_est", "qty", "count"],
        "dimensions": ["warehouse", "out_type"],
        "fixed_filters": None,
        "formula": AMOUNT_EST_FORMULA,
        "exclusions": "采购越库在出库侧归一为「领料出库」（见 normalize_out_type）",
        "date_boundary": "range",
        "aliases": ["出库", "领料出库", "各仓库出库", "按仓库看出库"],
        "scope_note": "继承登录用户 token 的仓库/组织隔离",
        "description": """查询某时间段内【出库记录】按【仓库】分类汇总，并拆分出库类型（如领料出库）：每个仓库的出库笔数、合计数量、估算金额，及按出库类型的拆分。口径：采购越库在出库侧归入「领料出库」，不会单列。用于回答"各仓库出库多少/领料出库多少/按仓库看出库"。stock_out_types 可选（出库类型编码列表），不传则统计全部出库类型。【限制】必须指定具体仓库（warehouse_name 必填，禁止跨全部仓库全量拉取以免打爆后端数据库）；查询区间最长 1 个月（≤31 天）。""",
    },
    "purchase_stat": {
        "label": "采购统计(服务端聚合)",
        "domain": "purchase",
        "source_interface": "HCGClient.page_pur_stat (/api/wms/reportStat/pagePurStatDayOrSupplier)",
        "measures": ["amount", "qty", "count"],
        "dimensions": ["warehouse", "supplier", "time"],
        "fixed_filters": {"含越库": True},
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["采购额", "采购金额", "采购总额", "入库金额", "结余", "采购统计"],
        "scope_note": "服务端聚合金额准确；继承登录用户 token 的仓库/组织隔离",
        "description": """【采购统计·区间汇总】查询某时间段内采购/入库/出库/越库/结余的真实金额与数量。金额由服务端聚合返回（准确，非估算），是回答"采购额/采购金额/采购总额/入库多少金额/采购入库统计/采购含越库多少"的首选工具。支持 warehouse_name、supplier_name 过滤。本工具返回金额/数量汇总，不含逐笔明细；若需逐笔明细或按商品/供应商排行，请用 purchase_ledger。""",
    },
    "purchase_ledger": {
        "label": "采购台账(服务端聚合)",
        "domain": "purchase",
        "source_interface": "HCGClient.get_purchase_ledger (/api/wms/reportStat/getPurchaseLedger)",
        "measures": ["amount", "qty", "count"],
        "dimensions": ["goods", "supplier", "goods_category"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["采购台账", "采购明细", "哪些商品采购最多", "供应商采购额", "采购分类排行"],
        "scope_note": "服务端 subtotal 小计金额准确；仓库过滤仅单仓库（模糊匹配取首个命中）",
        "description": """【采购台账·明细排行】查询某时间段内采购入库的逐笔台账，并给出按商品/供应商/一级分类的采购额(subtotal真实小计)排行 TOP N，以及台账总览(采购总额/采购次数/入库项数/供应商数)。金额准确（服务端 subtotal 小计，非估算）。仓库过滤仅支持单仓库（模糊匹配取首个命中）。用于回答"采购台账/哪些商品采购最多/哪个供应商采购额最高/采购分类排行/采购明细"。""",
    },
    "stock_snapshot": {
        "label": "进销存库存快照(服务端聚合)",
        "domain": "inventory",
        "source_interface": "HCGClient.page_stock_snapshot (/api/wms/reportStat/pageStockSnapshotReport)",
        "measures": ["amount", "qty", "count"],
        "dimensions": ["goods_category", "warehouse", "goods"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "point",
        "aliases": ["某天库存", "期末库存", "进销存", "库存分类金额", "库存按仓库"],
        "scope_note": "服务端聚合金额准确；分类名/仓库名/商品名接口自带，无需额外关联",
        "description": """【进销存库存快照·指定日期】查询某一天(快照日期)的进销存全貌：期初/采购入库/领料出库(含越库)/盘盈盘亏/调拨/加工/采购退货/领料退库/期末库存的金额与数量，以及按分类/仓库/商品的库存金额分布。金额准确（服务端聚合，非估算），且分类名/仓库名/商品名均为接口自带（无需额外关联）。用于回答"某天库存金额/期末库存/进销存/库存分类金额/库存按仓库/库存按分类"。report_date 必填(默认今天)，是时点快照不是区间。""",
    },
    "supplier_settlement": {
        "label": "供应商结算统计",
        "domain": "supply_chain",
        "source_interface": "HCGClient.page_supplier_settle",
        "measures": ["amount", "count"],
        "dimensions": ["supplier", "warehouse"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["供应商结算", "供应商绩效", "各供应商结算金额"],
        "scope_note": "服务端聚合金额准确；支持 warehouse_name/supplier_name 过滤",
        "description": """【供应商绩效·采购结算统计】查询某时间段内各供应商(客户)的入库总金额、结算总金额、实退总金额，并给出按结算金额排行 TOP N。金额由服务端返回（准确，非估算），是回答"供应商结算/供应商绩效/各供应商结算金额/供应商采购排行(结算口径)"的首选工具。支持 warehouse_name、supplier_name 过滤；不传则统计全部。""",
    },
    "delivery_fulfillment": {
        "label": "配送履约与验收差异",
        "domain": "supply_chain",
        "source_interface": "HCGClient.page_delivery_details_stat",
        "measures": ["amount", "count"],
        "dimensions": ["supplier", "goods_category", "warehouse"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["配送履约", "验收差异", "配送完成", "各供应商配送金额"],
        "scope_note": "服务端聚合金额准确；支持 warehouse_name/supplier_name 过滤",
        "description": """【配送履约与验收差异】查询某时间段内配送单据的履约状态（待分拣/待发货/待验收/已验收）以及采购金额、入库金额、验收差异金额、报废金额的聚合，并按供应商/分类/仓库拆分，给出验收状态分布。金额准确（非估算）。用于回答"配送履约/验收差异/配送完成情况/各供应商配送金额/采购验收差异"。支持 warehouse_name、supplier_name 过滤。""",
    },
    "cost_profit": {
        "label": "成本利润",
        "domain": "supply_chain",
        "source_interface": "HCGClient.profit_chart_stat",
        "measures": ["income", "expense", "profit"],
        "dimensions": ["time"],
        "fixed_filters": {"org_level": True},
        "formula": "profit = income - expense",
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["利润", "收支", "盈亏", "收入多少", "支出多少", "成本利润"],
        "scope_note": "组织级口径，无仓库过滤",
        "description": """【成本利润】查询某周期(date + dateType)的收入/支出/利润。利润=收入−支出，金额准确（服务端返回）。dateType: 1按周 2按月 3按年；metric: income 收入 / expense 支出 / profit 利润(默认，同时查收支并算利润)。用于回答"利润/收支/盈亏/收入多少/支出多少/成本利润"。该工具无仓库过滤（成本利润为组织级口径）。date 默认今天；dateType 默认 2(按月)。""",
    },
    "purchase_return": {
        "label": "退货统计",
        "domain": "supply_chain",
        "source_interface": "HCGClient.page_purchase_return",
        "measures": ["amount", "count"],
        "dimensions": ["supplier", "goods_category"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["退货金额", "退货多少", "各供应商退货", "退货类型", "退货财务状态"],
        "scope_note": "服务端聚合金额准确；支持 warehouse_name/supplier_name 过滤",
        "description": """【退货统计】查询某时间段内退货单的应退/实退金额、笔数，按供应商/分类排行，按退货类型(正常/冲销)与财务状态拆分。金额准确（非估算）。用于回答"退货金额/退货多少/退货明细/各供应商退货/退货类型/退货财务状态"。支持 warehouse_name、supplier_name 过滤。""",
    },
    "picking_out": {
        "label": "领料出库统计",
        "domain": "supply_chain",
        "source_interface": "HCGClient.page_picking_bill",
        "measures": ["amount", "qty", "count"],
        "dimensions": ["warehouse", "dest_type", "status"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["领料出库", "领用多少", "各仓领料", "领料去向"],
        "scope_note": "服务端聚合金额准确；支持 warehouse_name/dest_type/status 过滤",
        "description": """【领料出库统计】查询某时间段内领料单的计划/实际出库金额、数量，按仓库/去向类型(组织/员工/指定仓库)/状态拆分与排行。金额准确（非估算）。用于回答"领料出库/领用多少/各仓领料/领料去向/领料完成情况"。支持 warehouse_name、dest_type、status 过滤。""",
    },
    "requisition_status": {
        "label": "申购验收状态",
        "domain": "supply_chain",
        "source_interface": "HCGClient.page_apply_bill_count_status",
        "measures": ["amount", "count"],
        "dimensions": ["warehouse", "supplier", "status"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["申购多少", "待采购多少", "已采购多少", "已驳回多少", "申购金额"],
        "scope_note": "支持 warehouse_name/supplier_name 过滤",
        "description": """【申购验收状态】查询某时间段内申购明细按状态(已采购/待采购/已驳回)的数量，以及申购单总金额与单据数（按仓库/供应商）。用于回答"申购多少/待采购多少/已采购(转采购)多少/已驳回多少/申购金额"。支持 warehouse_name、supplier_name 过滤。""",
    },
    "health_certificate": {
        "label": "健康证合规预警",
        "domain": "food_safety",
        "source_interface": "HCGClient.page_health_certificate_stat",
        "measures": ["status_count"],
        "dimensions": ["warehouse", "status"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "none",
        "aliases": ["健康证", "证照合规", "健康证预警", "证照到期"],
        "scope_note": "计数/合规口径，无金额；支持 warehouse_name/status 过滤",
        "description": """【健康证合规预警】查询食堂员工健康证状态分布（正常/即将到期/已过期/已停用）及临期/过期明细清单。用于回答"健康证快到期/过期的有几人/哪些人健康证过期/证照合规情况/健康证预警"。支持 warehouse_name 过滤；status 可选(0禁用1启用2即将到期3已到期)。无金额口径。""",
    },
    "food_inspect": {
        "label": "食安巡检(日/周/月)",
        "domain": "food_safety",
        "source_interface": "HCGClient.inspect_page_stat",
        "measures": ["status_count", "rate"],
        "dimensions": ["warehouse", "inspect_type"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["巡检", "日管控", "周排查", "月调度", "不符合项"],
        "scope_note": "计数/合规口径，无金额；支持 start_date/end_date、warehouse_name 过滤",
        "description": """【食安巡检·日管控/周排查/月调度】查询某时间段内巡检完成率（已审核/待审核）、不符合项数量与分布。inspect_type 取 day(日管控)/week(周排查)/month(月调度)。用于回答"巡检完成率/食安巡检/日管控/周排查/月调度/不符合项多少/巡检情况"。支持 start_date/end_date、warehouse_name 过滤。无金额口径。""",
    },
    "sample_retention": {
        "label": "留样管理",
        "domain": "food_safety",
        "source_interface": "HCGClient.sample_count_by",
        "measures": ["status_count"],
        "dimensions": ["warehouse", "status"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["留样", "留样情况", "待取出", "留样中", "食品留样"],
        "scope_note": "计数口径，无金额；支持 start_date/end_date、warehouse_name 过滤",
        "description": """【留样管理】查询留样各状态数量（待存入/待取出/留样中/已取出）及合规留存口径。用于回答"留样多少/留样情况/待取出几单/留样中几单/食品留样"。支持 start_date/end_date、warehouse_name 过滤。无金额口径。""",
    },
    "morning_check": {
        "label": "晨检记录",
        "domain": "food_safety",
        "source_interface": "HCGClient.morning_check_page_stat",
        "measures": ["status_count", "rate"],
        "dimensions": ["warehouse", "check_type", "qualified"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["晨检", "晨检合格率", "体温异常", "员工健康晨检"],
        "scope_note": "计数口径，无金额；支持 start_date/end_date、warehouse_name、check_type、qualified 过滤",
        "description": """【晨检记录】查询某时间段内晨检（含午/晚检）的合格/不合格数量、在岗数量、不合格原因分布与按仓库分布。用于回答"晨检合格率/有多少人晨检不合格/晨检异常/体温异常/员工健康晨检"。支持 start_date/end_date、warehouse_name、check_type(5晨检10午检15晚检)、qualified(0不合格1合格)过滤。无金额口径。""",
    },
    "detection_report": {
        "label": "检测报告",
        "domain": "food_safety",
        "source_interface": "HCGClient.detection_page",
        "measures": ["status_count", "rate"],
        "dimensions": ["supplier", "goods", "warehouse"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["检测合格率", "农残检测", "检测报告", "检测不合格"],
        "scope_note": "计数口径，无金额；支持 start_date/end_date、warehouse_name、supplier_name 过滤",
        "description": """【检测报告】查询食材/环境检测合格率、不合格数量，以及按供应商/商品的不合格分布。用于回答"检测合格率/食材检测合格吗/哪些检测不合格/检测报告/农残检测"。支持 start_date/end_date、warehouse_name、supplier_name 过滤。无金额口径。""",
    },
    "food_additive": {
        "label": "食品添加剂",
        "domain": "food_safety",
        "source_interface": "HCGClient.food_additive_page",
        "measures": ["count", "over_limit_count"],
        "dimensions": ["warehouse", "additive"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["添加剂", "添加剂超标", "添加剂台账", "添加剂用量预警"],
        "scope_note": "计数/合规口径，无金额；支持 start_date/end_date、warehouse_name 过滤",
        "description": """【食品添加剂】查询添加剂使用台账与限量预警：按添加剂聚合使用次数、平均用量、超标(使用量>标准量)次数，及按仓库分布。用于回答"添加剂使用/添加剂超标/添加剂台账/添加剂用量预警"。支持 start_date/end_date、warehouse_name 过滤。无金额口径。""",
    },
    "warning_center": {
        "label": "综合预警中心",
        "domain": "warning",
        "source_interface": "HCGClient.page_early_warn_stat (startDate/endDate 业务周期)",
        "measures": ["status_count"],
        "dimensions": ["category", "warehouse", "status"],
        "fixed_filters": {"date_field": "startDate/endDate（业务周期，非推送日期）"},
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["综合预警", "待整改", "预警看板", "证照快到期", "库存过期预警"],
        "scope_note": "计数/状态口径，无金额；支持 category/status/start_date/end_date/warehouse_name 过滤",
        "description": """【综合预警中心】查询各类预警（证照到期/库存过期/食安巡检不符合项/采购验收等）的待整改/已整改/已忽略/已确认状态聚合，及按分类、待整改明细 TOP。用于回答"有哪些预警/哪些待整改/证照快到期/库存过期预警/巡检不符合项/预警看板"。支持 category(fs食安/certificate证照/stock仓储/purchase采购/accept验收)、status(0待整改1已整改2已忽略4已确认)、start_date/end_date、warehouse_name 过滤。无金额口径。""",
    },
    "device_alarm_index": {
        "label": "环境设备告警指数",
        "domain": "warning",
        "source_interface": "HCGClient.get_third_device_warn_target",
        "measures": ["count"],
        "dimensions": ["warehouse", "device_type"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "none",
        "aliases": ["环境告警", "温度告警", "燃气告警", "设备告警指数", "消杀环境看板"],
        "scope_note": "计数口径，无金额；支持 warehouse_name 过滤",
        "description": """【环境设备告警指数】查询厨房环境设备（温度/湿度/烟雾/燃气/水浸/AI巡检）的累计告警总数，做指数看板。用于回答"环境告警多少/温度告警/燃气告警/烟雾告警/设备告警指数/消杀环境看板"。支持 warehouse_name 过滤。无金额口径。""",
    },
    "device_alarm_detail": {
        "label": "环境设备告警明细",
        "domain": "warning",
        "source_interface": "HCGClient.page_third_device_warn",
        "measures": ["count"],
        "dimensions": ["warehouse", "status", "app_type"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["设备告警明细", "未处理告警", "消杀告警", "温度超标明细"],
        "scope_note": "计数口径，无金额；支持 start_date/end_date、status、warehouse_name、app_type 过滤",
        "description": """【环境设备告警明细】查询消杀/环境设备告警明细（温度/湿度/烟雾/燃气/水浸/AI巡检等），含告警类型/内容/数值/状态/取证。用于回答"设备告警明细/未处理告警/消杀告警/环境设备告警记录/温度超标明细"。支持 start_date/end_date、status(0未处理1已处理2已忽略)、warehouse_name、app_type(1物联网2AI巡检) 过滤。无金额口径。""",
    },
    "period_compare": {
        "label": "周期对比·趋势",
        "domain": "report",
        "source_interface": "复用 purchase_stat / cost_profit（page_pur_stat / profit_chart_stat）",
        "measures": ["amount", "income", "expense", "profit"],
        "dimensions": ["time"],
        "fixed_filters": {"底层工具金额准确": True},
        "formula": "相邻周期环比 = (本期−上期)/上期",
        "exclusions": None,
        "date_boundary": "range",
        "aliases": ["趋势", "走势", "环比", "同比", "每月对比", "逐月", "X月比Y月"],
        "scope_note": "复用金额准确的底层工具，金额均来自服务端聚合",
        "description": """【周期对比 / 趋势（问数增强）】把同一个【金额准确的底层工具】在多个周期上分别执行，串成时间序列并自动计算相邻周期环比（差值与百分比）。当用户问「趋势 / 走势 / 比上个月 / 环比 / 同比 / 每月对比 / 上半年走势 / 一季度各月 / 7月比6月多多少 / 近半年采购额变化 / 各月利润对比」时使用本工具。base_tool 可选：purchase_stat（采购统计，主对比指标=采购总额含越库；支持 warehouse_name/supplier_name 过滤）；cost_profit（成本利润，metric=income 收入 / expense 支出 / profit 利润，组织级无仓库过滤）。periods 为周期列表，元素格式支持："YYYY-MM"（自然月）、"YYYY"（自然年）、"YYYY-MM-DD~YYYY-MM-DD"（显式区间）。周期按列表顺序串联。金额均来自服务端聚合，准确非估算。""",
    },
    "dashboard_overview": {
        "label": "经营驾驶舱总览",
        "domain": "dashboard",
        "source_interface": "wms_report_index + wms_report_wait_processed + data_report_index + data_report_overview",
        "measures": ["amount", "count", "rate"],
        "dimensions": ["warehouse"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "none",
        "aliases": ["经营总览", "驾驶舱", "今日概览", "今日经营情况", "食安概况"],
        "scope_note": "指标为接口直接返回，无金额估算；可选 warehouse_name 过滤",
        "description": """【经营驾驶舱总览】食堂管理者每天进系统的第一眼：一站式汇总今日关键经营指标 + 待处理单据 + 食安概况。当用户问「经营总览 / 驾驶舱 / 今天经营情况 / 今日概览 / 待办单据 / 今天采购了多少 / 晨检多少人 / 食安概况」时优先用本工具。返回：今日采购金额/验收金额/留样项数/晨检人数（均含日同比）、本月待处理单据（调整单/申购/采购/退货数量与金额）、食安各模块概况清单。指标为接口直接返回，无金额估算。可选 warehouse_name 过滤。""",
    },
    "purchase_price_compare": {
        "label": "采购价对比",
        "domain": "report",
        "source_interface": "HCGClient.page_pur_price_compare",
        "measures": ["amount_est", "count", "rate"],
        "dimensions": ["goods", "warehouse", "supplier"],
        "fixed_filters": None,
        "formula": "超价采购额估算 = 采购单价 × 入库数量",
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["采购价对比", "比平台价高", "买贵了", "超价", "采购价异常"],
        "scope_note": "金额口径为估算（price×入库数量），回答须注明“估算”；默认当前自然月",
        "description": """【采购价对比】逐笔对比采购单价与平台参考价，找出买贵了的单子（食堂成本把控核心）。当用户问「采购价对比 / 比平台价高多少 / 哪里买贵了 / 超价 / 采购单价 vs 市场价 / 新发地价对比 / 采购价异常」时使用。返回超价统计（超价笔数、超价采购额估算）与明细 TOP（按超出比例降序，含商品/规格/仓库/供应商/采购单价/平台价/超出比例/入库数量）。默认当前自然月；支持 start_date/end_date/warehouse_name 过滤。price×入库数量 为采购额估算，回答须注明「估算」。""",
    },
    "stock_month_report": {
        "label": "库存月报",
        "domain": "report",
        "source_interface": "HCGClient.page_stock_month_report (/wms/reportStat/pageStockMonthReport)",
        "measures": ["amount", "qty", "count"],
        "dimensions": ["goods", "goods_category"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "point",
        "aliases": ["库存月报", "某月库存", "期末库存", "当月进销存", "各商品库存金额"],
        "scope_note": "服务端聚合金额准确非估算；report_date 默认上一完整月；支持 warehouse_name 过滤",
        "description": """【库存月报】按月复盘进销存（食堂月度经营复盘）。按商品汇总当月期初/入库/出库/期末金额与数量，金额由服务端聚合准确非估算。当用户问「库存月报 / 某月库存 / 期末库存多少 / 当月进销存 / 库存月度汇总 / 各商品库存金额」时使用。返回月度汇总（期初/采购入库/采购越库/领料出库/入库/出库/期末金额与数量）+ 商品明细 TOP（按期末金额降序）。report_date 为月报日期（取当月首日，默认上一完整月）；支持 warehouse_name 过滤。【限制】必须指定具体仓库（warehouse_name 必填，禁止跨全部仓库全量拉取以免打爆后端数据库）；月报本身按月，无需区间参数。""",
    },
    "food_safety_alert": {
        "label": "预警中心总览",
        "domain": "warning",
        "source_interface": "HCGClient.page_early_warn_stat + get_early_warn_stat_item (startDate/endDate=createTime 推送日期)",
        "measures": ["status_count"],
        "dimensions": ["category", "warehouse", "status"],
        "fixed_filters": {"date_field": "startDate/endDate（推送日期 createTime）"},
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["预警中心", "预警总览", "各类预警", "待整改多少", "预警明细"],
        "scope_note": "计数/状态口径，无金额；默认全部分类；默认当前自然月",
        "description": """【预警中心总览】统一查看各类预警（食安fs/证照certificate/定价pricing/采购purchase/验收accept/排菜dish/仓储stock/询比价inquiry）的红线看板。默认查**全部分类**（对齐生产系统「预警中心」默认视图），展示待整改/已整改/已忽略/已确认四态数量、按仓库状态分布与处置完成率、按预警类型汇总，并列出待整改明细 TOP。当用户问「预警中心 / 预警总览 / 各类预警 / 待整改多少 / 预警明细 / 哪些问题没改」时使用；若明确问某分类（如「食安预警」「证照到期」「定价预警」）可传 category=fs/certificate/pricing 等只看该类。默认当前自然月；支持 start_date/end_date/category/warehouse_name/status(0待整改 1已整改 2已忽略 4已确认) 过滤。无金额口径。""",
    },
    "dish_cost_rate": {
        "label": "排菜成本率",
        "domain": "dish",
        "source_interface": "HCGClient.dish_menu_list",
        "measures": ["rate", "amount"],
        "dimensions": ["warehouse", "dish"],
        "fixed_filters": None,
        "formula": "成本率 = 成本 / 标准伙食费",
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["排菜成本率", "菜单成本", "餐标", "超标准菜品"],
        "scope_note": "数据来自 dish/menu/list（按仓库合并）；默认本月",
        "description": """【排菜成本率】分析某时间段菜单成本占标准伙食费的比例（成本率=成本/标准伙食费），找出超成本的菜品。数据来自 dish/menu/list（按仓库合并）。用于回答"排菜成本率/菜单成本/哪些菜超标准/餐标"。可选 warehouse_name、top_n。""",
    },
    "dish_reputation": {
        "label": "出品口碑",
        "domain": "dish",
        "source_interface": "HCGClient.dish_menu_list (isComment=true)",
        "measures": ["count", "score"],
        "dimensions": ["warehouse", "dish"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["菜品评价", "口碑", "评分", "受欢迎", "差评"],
        "scope_note": "数据来自 dish/menu/list(isComment=true)；默认本月",
        "description": """【出品口碑】分析菜品的评价数与评分，找出评价最多、评分偏低的菜品。数据来自 dish/menu/list(isComment=true)。用于回答"菜品评价/口碑/评分/哪些菜受欢迎/差评"。可选 warehouse_name、top_n。""",
    },
    "dish_nutrition": {
        "label": "营养 NRV",
        "domain": "dish",
        "source_interface": "HCGClient.dish_menu_nutrition",
        "measures": ["nrv_ratio"],
        "dimensions": ["warehouse", "dish", "nutrient"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["营养", "热量", "蛋白质", "钠", "健康膳食"],
        "scope_note": "数据来自 dish/menu/nutrition（按菜单 uuid）；默认本月",
        "description": """【营养 NRV】查看菜单营养素占每日参考摄入量(NRV)的比例（能量/蛋白质/脂肪/碳水/钠/钙等）。数据来自 dish/menu/nutrition（按菜单 uuid）。用于回答"营养/热量/蛋白质/钠/健康膳食"。可选 warehouse_name、top_n。""",
    },
    "inquiry_effect": {
        "label": "询比价成效",
        "domain": "inquiry",
        "source_interface": "HCGClient.pms_quote_bill_page",
        "measures": ["rate", "count", "amount"],
        "dimensions": ["warehouse", "inquiry"],
        "fixed_filters": None,
        "formula": None,
        "exclusions": None,
        "date_boundary": "optional",
        "aliases": ["询比价成效", "报价率", "中标", "比价", "降本"],
        "scope_note": "数据来自 pms/quoteBill/page；默认本月",
        "description": """【询比价成效】分析询比价的报价参与率、截止情况与金额，按询价单分组看报价率。数据来自 pms/quoteBill/page。用于回答"询比价成效/报价率/中标/比价/降本"。可选 warehouse_name、top_n。""",
    },
}


def get_metric(name):
    """按工具名取口径元数据；不存在返回 None。"""
    return METRICS.get(name)


def all_tool_names():
    """返回所有已注册工具名（按注册顺序）。"""
    return list(METRICS.keys())
