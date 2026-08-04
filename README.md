# 后厨管家 · AI 问数（独立部署服务）

一个**独立可部署**的自然语言问数应用：用户用账号登录 → 绑定自己的后厨管家账号 → 用中文提问（如"7月采购入库多少金额"）→ 系统调用后厨管家真实接口取数 → 大模型生成结论 + 表格。

> 本应用**不依赖你现有的业务系统**，自带账号体系与权限隔离，可单独部署在内网/云服务器，多人通过浏览器访问。

## 特性
- **直接用后厨管家账号登录**：输入后厨管家用户名 / 密码 → 调用后厨管家登录接口校验，校验通过即建立会话，无需额外注册应用账号。
- **单点登录（SSO）**：已在后厨管家登录的用户，从后厨管家页面跳转 `/sso?token=...&username=...` 即可**免二次登录**直接进入问数页面（token 实测校验，不可伪造；成功后 302 跳主页，token 不留地址栏）。对接格式见 [SSO对接说明.md](SSO对接说明.md)。
- **天然多用户**：每个后厨管家账号各自登录、各自取其组织 / 仓库权限下的数据，互不越权。
- **真实数据**：所有数字 100% 来自后厨管家接口返回，无编造；金额=单价×数量为估算值。
- **图表输出**：问"折线图/柱状图"时，自动用 ECharts 渲染趋势折线图、排行柱状图、库存预警饼图（ECharts 已随包本地化，无需访问外网 CDN）。
- **大模型**：默认走腾讯混元（推荐用 MaaS tokenhub 网关调 `hy3`，纯标准库 `urllib` 实现，无需 TC3 签名）；也支持原生混元 TC3 签名；未配置密钥时自动走本地 MockLLM 演示。
- **并发**：`ThreadingHTTPServer` + 服务端内存会话 + HttpOnly Cookie。
- **零外网依赖**：纯 Python 标准库 + 本地内置 ECharts，`python app.py` 即可运行，**无需 `pip install`、无需访问任何外网 CDN**，可完全离线部署在内网 / 云服务器。

## 快速开始
```bash
cd ai_qa_system
cp .env.example .env          # 按需填写 APP_SECRET、MAAS_API_KEY（真实模型）等
chmod +x run.sh
./run.sh                      # 或直接 python3 app.py
```
浏览器打开 `http://<服务器IP>:8011`

> 整个过程**不依赖外网**：页面引用的 ECharts 已从 CDN 改为本地 `libs/echarts.min.js`，即使服务器完全断网也能正常出图。

- 使用你的**后厨管家账号**直接登录即可开始问数（例如 `at0001` 及其对应密码）。
- 多用户场景：不同同事各自用各自的后厨管家账号登录，数据天然隔离，互不越权。

### 示例问法
- 汇总：`7月采购入库多少金额？` / `本月入库多少笔？`
- 排行：`哪个供应商供货金额最多？` / `本月各仓库采购入库排行`
- 趋势图：`用折线图展示7月份每日采购金额`
- 排行图：`用柱状图展示7月各供应商采购金额排行`
- 预警：`库存有哪些临期预警？`
- 按仓库 / 按分类汇总（新增）：
  - 库存按仓库：`各仓库当前库存多少？` / `库存按仓库分布`
  - 库存分类占比：`库存分类占比` / `各分类库存多少` / `哪些分类库存最多`（一级商品分类，含数量占比饼图）
  - 采购入库：`7月各仓库采购入库多少？` / `按仓库看进货`
  - 出库：`7月各仓库出库多少？` / `领料出库按仓库汇总`（采购越库已并入领料出库）

> 库存汇总工具（inventory_by_warehouse / inventory_by_category）**只统计库存数量>0 的有效库存**，
> 数量为 0 的无效记录已剔除（服务端 zeroQty=False + 客户端 qty<=0 兜底）。分类名来自
> queryGoodsCategory（库存记录本身分类名为空、仅含分类 uuid，需 join 分类树）。
> 库存全量约 6.8 万条，沙箱 MAX_RECORDS=8000 会触发"数据量过大"友好提示；服务器用默认
> 200000 可跑（约 35 页 pageSize=2000 流式聚合，内存安全）。
>
> 三组"按仓库分类汇总"工具（inventory_by_warehouse / purchase_inbound_by_warehouse /
> stock_out_by_warehouse）专门回答"按仓库"类问题：库存按仓库汇总为时点快照（无需日期）；
> 采购入库按仓库**含越库**（purchaseIn + purchaseCrossIn，越库在入库侧即记为采购入库）；
> 出库按仓库会按出库类型拆分（如领料出库），**采购越库在出库侧归入「领料出库」，不单列**。

> 当问题含"折线图/趋势图/走势图"时，系统会渲染折线图；含"柱状图/排行图"时渲染柱状图。

## 配置项（.env）
见 `.env.example`。关键：
- `APP_SECRET`：会话签名密钥（生产必填强随机值）。
- `HUNYUAN_SECRET_ID/KEY`：原生混元 TC3 签名密钥；不填则走 MockLLM。
- `MAAS_API_KEY`：腾讯 MaaS tokenhub 等 OpenAI 兼容网关 Key（**推荐**，设了即用真实模型 `hy3`，无需 TC3 签名）。`MAAS_BASE_URL` / `MAAS_MODEL` 一般不用改。
- `HOST/PORT`：监听地址端口（部署用 `HOST=0.0.0.0`）。
- `SESSION_SECURE`：HTTPS 反向代理后为 `1`。
- `HCG_BASE_URL`：后厨管家接口地址。

## 反向代理（生产建议）
用 nginx 前置提供 HTTPS 与域名访问：
```nginx
server {
    listen 443 ssl;
    server_name aiqa.your-domain.com;
    ssl_certificate     ...; ssl_certificate_key ...;
    location / {
        proxy_pass http://127.0.0.1:8011;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
并在 `.env` 设 `SESSION_SECURE=1`。

## 目录结构
```
ai_qa_system/
├── app.py            # Web 服务（登录/问数/登出，并发，单层后厨管家登录）
├── config.py         # .env 配置加载
├── hcg_client.py     # 后厨管家 API 客户端（token 感知）
├── semantic_tools.py # 语义聚合工具（汇总/排行/趋势/预警/按仓库汇总）
├── semantic_layer.py # 系统提示词与口径字典
├── agent.py          # 意图→工具→真实数据→自然语言 编排
├── hunyuan.py        # 大模型适配器（MaaS 网关 / 原生混元 / 本地 MockLLM）
├── index.html        # 前端单页应用（登录 + 对话）
├── libs/
│   └── echarts.min.js # 本地内置 ECharts（离线可用，不依赖 CDN）
├── .env.example      # 配置模板
└── run.sh            # 启动脚本
```

> 注：`crypto.py` / `db.py` 为早期"应用账号 + 绑定"方案遗留，当前单层登录已不依赖它们，可保留或删除，不影响运行。

## 安全说明
- 后厨管家凭据**不落地存储**：仅在登录时用密码调登录接口校验，服务端仅保存返回的会话 token（内存，8 小时过期），不以任何形式保存密码。
- 会话 Cookie 为 HttpOnly，仅存放随机 `sid`；敏感 token 不出现在前端。
- 问数全程只读后厨管家接口，无写操作。
- 建议生产环境：配置 `APP_SECRET`、启用 HTTPS（`SESSION_SECURE=1`）、及时停用默认 admin、按需禁用不需要的接口维度。

## 已知口径
- 越库(`purchaseCrossIn`)也是一种采购，统计"采购/采购数据/采购入库"时**默认同时计入** `purchaseIn` 与 `purchaseCrossIn`；仅当用户明确说"只要进库的/不含越库/仅入库"时才只取 `purchaseIn`。
- 金额无接口字段，按 `单价×数量` 估算，结论中已注明"估算"。
- 跨计量单位（斤/只/件…）的数量不可直接相加。

## 超大区间保护（防止超时 / 内存不足）
问数区间过大时（如整月、整年），后端不会一次性全量拉取，而是两级自适应保护：
1. **自动按月切片**：区间跨多月时，自动按自然月拆成子区间，逐月拉取聚合后合并，内存峰值 = 单月，整年也能稳定跑（服务器内存充足时）。
2. **`total` 预估友好提示**：拉取首月即读取接口返回的 `total` 条数，若超过 `MAX_RECORDS`（单区间安全上限），立即返回友好提示而非硬拉，避免 OOM / 超时。提示会引导用户缩小区间或指定维度。

配置：`MAX_RECORDS`（`.env`，默认 200000 适合服务器；沙箱 / 低配环境可调小，如 8000，使大区间直接走友好提示）。
# aiask
