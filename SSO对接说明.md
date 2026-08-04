# 单点登录（SSO）对接说明

> 面向**后厨管家系统侧**开发同学：本文说明如何从后厨管家页面一键跳转到 AI 问数系统，
> 让已在后厨管家登录的用户**免二次登录**直接进入问数页面。
>
> 问数侧（本项目）已实现完毕，**无需再改问数代码**，只需后厨管家侧按下述格式发起跳转。

---

## 一、跳转地址

```
https://<问数服务地址>/sso?token=<后厨管家登录token>&username=<用户名>&dataVersion=<可选>
```

示例（内网直连 8011 端口）：

```
http://192.168.1.100:8011/sso?token=8f3a...c21&username=at0001
```

### 参数说明

| 参数 | 必填 | 说明 |
|---|---|---|
| `token` | 是 | 后厨管家登录接口返回的会话 token（即请求头 `Access-Token: m_<token>` 中 `m_` 后面的部分）。若你手上的值本身带 `m_` 前缀，直接传也可以，问数侧会自动剥离。 |
| `username` | 建议 | 当前登录用户名，仅用于问数页面右上角展示与日志标识。不传则显示为 `sso-user`，不影响取数。 |
| `dataVersion` | 否 | 后厨管家 `Data-Version` 头的值。若你们环境有多数据版本，请一并传；不传则用问数侧默认值。 |
| `redirect` | 否 | 登录成功后跳转的**站内相对路径**，默认 `/`（问数主页）。出于安全考虑，只接受以单个 `/` 开头的相对路径，`//` 与绝对 URL 会被忽略并回退到 `/`。 |

**参数务必做 URL 编码**（`encodeURIComponent`）。

### 前端示例

```javascript
// 后厨管家页面上的 "AI 问数" 按钮
function openAiQa() {
  const base  = 'http://192.168.1.100:8011';           // 问数服务地址
  const token = getAccessToken().replace(/^m_/, '');   // 取当前登录 token，去掉 m_ 前缀
  const user  = getCurrentUserName();
  const url = `${base}/sso?token=${encodeURIComponent(token)}`
            + `&username=${encodeURIComponent(user)}`;
  window.open(url, '_blank');   // 或 location.href = url
}
```

---

## 二、问数侧的处理流程

1. 接收 `/sso?token=...`；缺 `token` → 302 跳 `/?sso=missing`。
2. 用该 token 调后厨管家真实接口（`queryWarehouses`，pageSize=1，极轻量）做**有效性实测**；
   失败 → 302 跳 `/?sso=invalid`，页面提示"单点登录失效，请重新从后厨管家进入"。
3. 校验通过 → 服务端内存建会话（保存 username / token / dataVersion，TTL 8 小时），
   下发 `HttpOnly` Cookie `aiqa_sid`，再 **302 跳转到问数主页**。
4. 此后所有问数请求都以该用户身份调后厨管家接口，**权限、组织、仓库隔离与后厨管家完全一致**。

关键响应示例：

```
GET /sso?token=<有效token>&username=at0001

HTTP/1.1 302 Found
Set-Cookie: aiqa_sid=<随机sid>; HttpOnly; Path=/; SameSite=Lax
Location: /
```

---

## 三、安全设计

- **token 不落地**：仅存在于问数服务端内存会话中（8 小时过期），不写数据库、不写日志、不返回前端。
- **不可伪造**：不是"带了 token 就放行"，而是拿 token 去后厨管家接口**实测**成功才建会话。
- **token 不留在地址栏**：校验通过后立即 302 到 `/`，浏览器地址栏不再包含 token。
- **防开放重定向**：`redirect` 只允许站内相对路径。
- **Cookie 防护**：`HttpOnly`（JS 读不到）+ `SameSite=Lax`；HTTPS 部署时在 `.env` 设 `SESSION_SECURE=1`，Cookie 自动加 `Secure`。

### 生产环境建议

1. 用 nginx 前置 HTTPS（见 README「反向代理」章节），避免 token 明文出现在 URL 中被中间链路记录。
2. 若问数与后厨管家不同域，`window.open` 新标签打开即可，无跨域 Cookie 问题（Cookie 种在问数自己的域下）。
3. 如需更高安全等级，可后续升级为**一次性 ticket** 方案（后厨管家侧签发短时效一次性票据，问数侧回调换取 token），
   问数侧改造点仅在 `_api_sso`，前端跳转格式不变。

---

## 四、兜底与排错

- **密码登录仍然保留**：直接访问 `http://<问数地址>/`，用后厨管家账号密码登录即可，不依赖 SSO。
- `/?sso=missing`：跳转 URL 里没带 `token`，检查前端拼串。
- `/?sso=invalid`：token 无效或已过期（后厨管家侧退出登录、token 超时），请让用户在后厨管家重新登录后再跳转。
- 跳转后仍停在登录页：确认浏览器未拦截 Cookie；若问数部署在 HTTPS 下，确认 `.env` 已设 `SESSION_SECURE=1`。
- 自检接口：登录后请求 `GET /api/me`，应返回 `{"success": true, "username": "<用户名>"}`。

---

## 五、已验证结论

- 单元测试：合法 token 建会话并种 Cookie 跳主页；token 失效 / 缺参正确回退登录页并带提示；`redirect=//evil.com` 被拦截回退 `/`。
- 真实环境端到端：用后厨管家 demo 账号 `at0001` 取得真实 token，
  `GET /sso?token=<真实token>&username=at0001` → `302` + `Set-Cookie: aiqa_sid=...` + `Location: /`；
  带该 Cookie 请求 `/api/me` → `{"success": true, "username": "at0001"}`；不带 Cookie → `{"success": false, "message": "未登录"}`。
