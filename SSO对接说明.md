# 后厨管家 AI 问数系统 · 单点登录（SSO）对接说明

> 适用系统：后厨管家 AI 问数系统（端口 8011，纯 stdlib Python + ThreadingHTTPServer）
> 目标：后厨管家后台（https://wms.houchuguanjia.com/）登录后，用户无需再次输入账号即可直接进入 AI 问数，且以**当前登录用户的身份**查数（继承组织/仓库权限隔离）。

---

## 一、接入原理

AI 问数系统**不自有账号体系**，它本身没有"注册/改密"逻辑。它信任后厨管家下发的用户 token，并用这个 token 去调后厨管家接口查数据。

SSO 流程如下：

```
后厨管家后台（已登录）
   │  用户点击「AI 问数」菜单
   ▼
Java 网关拼接带 token 的重定向 URL
   ▼
浏览器跳转到 AI 问数 /sso 端点
   ▼
AI 问数服务端用 token 调 verify_token() 校验有效性
   ▼
校验通过 → 建立会话、种 HttpOnly Cookie → 302 跳转到问数主页（已登录态）
校验失败 → 302 跳回登录页，URL 带 sso=invalid（前端提示重新进入）
```

整个过程中，**token 只出现在跳转瞬间**，成功后 302 重定向会把 token 从地址栏移除，后续请求靠 HttpOnly Cookie 维持会话。

---

## 二、重定向 URL 拼接（Java 侧负责）

后端在「AI 问数」菜单点击时，重定向到以下地址：

```
<AIQA_BASE_URL>/sso?token=<用户token>&username=<用户名>&dataVersion=<dataVersion>&redirect=<跳转路径>
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `token` | ✅ | 后厨管家登录后下发给该用户的 token（原始 token 即可；若带了 `m_` 前缀，AI 问数端会自动剥离，避免双重前缀） |
| `username` | 否 | 用户名，仅用于界面展示。缺省时显示为 `sso-user` |
| `dataVersion` | 否 | 后厨管家接口的 dataVersion（数据版本）。建议带上，使 AI 问数端请求接口时与之对齐 |
| `redirect` | 否 | 登录成功后跳转的相对路径，默认 `/`（问数主页）。必须是以 `/` 开头的同域相对路径，否则会被强制重置为 `/`（防开放重定向） |

### 拼接示例（Java）

```java
String aiqaBase = "http://192.168.3.77:8011";   // 生产替换为实际域名
String token = currentUser.getToken();          // 当前登录用户的后厨管家 token
String username = currentUser.getUsername();
String dataVersion = currentUser.getDataVersion();

String redirectUrl = aiqaBase + "/sso"
    + "?token=" + URLEncoder.encode(token, "UTF-8")
    + "&username=" + URLEncoder.encode(username, "UTF-8")
    + "&dataVersion=" + URLEncoder.encode(dataVersion, "UTF-8")
    + "&redirect=" + URLEncoder.encode("/", "UTF-8");

// 前端：window.location.href = redirectUrl;  （或后端直接 302 重定向）
```

> 测试环境 AI 问数地址：`http://192.168.3.77:8011`
> 生产环境以实际部署域名 / 反向代理地址为准。

---

## 三、AI 问数端处理（`/sso` 端点，已内置）

| 步骤 | 行为 |
|------|------|
| 1. 取参 | 从 URL query 解析 `token / username / dataVersion / redirect` |
| 2. 缺失校验 | 无 `token` → 302 到 `/?sso=missing` |
| 3. 前缀容错 | token 若以 `m_` 开头，自动剥掉（HCGClient 内部会自行拼接 `m_` 前缀） |
| 4. token 校验 | 用该 token 实例化 HCGClient 并调 `verify_token()`，实测接口成功才算有效 |
| 5. 建立会话 | `verify_token()` 通过 → 生成 sid，服务端保存 `{username, token, dataVersion, expire}` |
| 6. 种 Cookie | `Set-Cookie: <COOKIE_NAME>=<sid>; HttpOnly; Path=/; SameSite=Lax` |
| 7. 跳转 | 302 到 `redirect`（默认 `/`）；token 不再停留在地址栏 |

前端 `index.html` 进入时会调 `/api/me` 检查登录态：
- 已登录 → 直接进入问数主界面
- 未登录但带 `?sso=invalid` / `?sso=missing` → 登录框提示「单点登录失效 / 未检测到凭证，请重新从后厨管家进入，或手动输入账号登录」

---

## 四、前端嵌入方式（三种）

AI 问数是一个独立 Web 应用，后厨管家后台可通过以下任一方式接入：

1. **新标签页 / 菜单跳转（推荐）**
   菜单点击 → 打开 `<AIQA_BASE_URL>/sso?...`（新标签或当前页跳转均可）。最简单、最稳，互不影响登录态。

2. **iframe 内嵌**
   后台某个页面用 `<iframe src="<AIQA_BASE_URL>/sso?...">` 内嵌。注意：
   - Cookie 已设 `SameSite=Lax`，同站点内嵌可正常携带；跨站内嵌需改为 `SameSite=None; Secure` 并启用 HTTPS。
   - 建议 iframe 内再单独放「营养分析报表」等子页时用 `/nutrition.html`（需先完成 SSO 登录，否则会被拦截回登录页）。

3. **统一门户反向代理**
   由网关把 `/aiqa/*` 反代到 AI 问数 8011，菜单使用同源路径，避免跨域 / Cookie 问题。

---

## 五、登出联动（可选）

AI 问数提供 `/api/logout`（清除服务端会话 + 清 Cookie）。可选做法：

- **简单方案**：用户在 AI 问数内点「退出」只清 AI 问数会话；后厨管家主系统退出后，AI 问数下次请求会因 token 失效而要求重新 SSO（自愈）。
- **联动方案**：后厨管家主系统登出时，一并调用 AI 问数 `/api/logout`（需同源/CORS 允许或经网关），实现统一登出。

---

## 六、安全要点

- **token 不落地**：token 仅存在于服务端会话内存，用于向后厨管家发请求；不写入数据库、不写前端可读取的存储。
- **有效性实测**：`/sso` 不是"有 token 就信"，而是用 token 真实调一次接口（`verify_token()`），杜绝伪造会话 / 过期 token。
- **防开放重定向**：`redirect` 仅允许同域相对路径。
- **Cookie 策略**：HttpOnly + SameSite，降低 XSS / CSRF 风险。
- **URL 短暂暴露**：token 仅出现在 302 跳转瞬间，成功后即从地址栏移除；如介意，可改用「后端到后端一次性 ticket」方案（见下）。

### 可选增强：一次性 Ticket（更严谨）

若不想让 token 出现在浏览器 URL 中，可改为：

1. Java 网关用后厨管家内部密钥调 AI 问数内部接口 `POST /api/sso/exchange`，带上 `{ticket, username, dataVersion}`（ticket 由 Java 侧生成、一次性、短时有效）。
2. AI 问数校验 ticket 后返回会话 Cookie，前端再带着 Cookie 访问主页。

当前版本未实现该接口，如需要可后续补充。

---

## 七、常见问题排查

### Q1：本地带 token 能登录，部署到服务器后仍跳回登录页

**典型现象**：浏览器地址栏显示 `http://<服务器IP>:8011/?token=xxxx`，然后页面是登录框。

**根因**：跳转 URL 拼错了路径。AI 问数只在 `/sso` 端点处理 token，根路径 `/` 不会自动用 token 登录。

**正确 URL**：
```
http://<服务器IP>:8011/sso?token=xxxx&username=xxx&dataVersion=xxx&redirect=/
```

**错误 URL**（截图中常见）：
```
http://<服务器IP>:8011/?token=xxxx   ← 缺少 /sso
```

**修复**：检查后厨管家侧「AI 问数」菜单的跳转地址，确保 base URL 后接 `/sso`，不要只拼到根路径。

> 自 `2026-08-18` 起，`index.html` 已加兼容兜底：根路径带 `?token=` 且不带 `?sso=` 时会自动重定向到 `/sso`。但**正确做法仍是直接跳 `/sso`**，不要依赖兜底。

### Q2：token 明明有效，却提示「单点登录失效」

可能原因：
1. 服务器 `.env` 里的 `HCG_BASE_URL` 与 token 所属环境不一致（例如用生产的 token 去调测试环境接口）。
2. token 在传输中被截断或 URL 编码错误（注意 `+`、空格、`/` 等字符必须 `URLEncoder.encode`）。
3. token 已经过期（后厨管家 token 有有效期，过期后需重新进入）。

排查：在服务器上执行 `curl -v "http://127.0.0.1:8011/sso?token=<token>&redirect=/"`，观察是 `302 Location: /`（成功）还是 `302 Location: /?sso=invalid`（失败）。

### Q3：内嵌 iframe 中登录态丢失

- 检查浏览器控制台是否有 Cookie 被拦截的警告。
- 若 AI 问数与后厨管家**跨域**且用 HTTP，需将 `app.py` 中 `_set_cookie` 的 `SameSite=Lax` 改为 `SameSite=None; Secure`，并启用 HTTPS。
- 最简单方案：菜单用「新标签页打开 `/sso`」，避开 iframe Cookie 策略问题。

## 八、联调检查清单

- [ ] 后厨管家后台能拿到当前用户的 `token` / `dataVersion`
- [ ] 菜单跳转 URL 正确拼接为 `<AIQA_BASE_URL>/sso?token=...`（注意是 `/sso` 不是 `/`）
- [ ] 浏览器跳转 `/sso` 后能被正确 302 到问数主页（地址栏不再含 token）
- [ ] 问数主页能正常以该用户身份查数（权限与后厨管家一致）
- [ ] token 失效 / 过期时，重新从后厨管家进入可恢复
- [ ] （如内嵌）确认 Cookie 的 SameSite 策略与部署协议（HTTP/HTTPS）匹配
