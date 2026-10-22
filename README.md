# Tokenjuice-Register-Tool

Token Juice（`tokenjuice.ai`）全链路注册机：**CF Worker 私域邮箱建号 → 发登录链接 →
收信取 token → 验证激活 → 创建 API Key → 网关验活 → 入库**。纯 HTTP，无浏览器、
无验证码、无第三方依赖（只要 `requests`）。

规格真源：原始抓包（`tokenjuice-1.ai.har` / `tokenjuice-2.ai.har`，已于 2026-10-23
按安全审计删除，见 `docs/security-audit.md`）+ 站点前端 SDK 原文
（`assets/api-DoqthcYR.js`）。所有端点、Cookie、错误码都经过 2026-10-22 的
实测复核，不是从 HAR 猜的。

---

## 1. 链路

```text
① 建邮箱        POST {TEMPMAIL_BASE}/api/mailboxes           Authorization: Bearer <admin key>
                 → oai-xxxxxxxx@liziai.cloud
② 发登录链接    POST https://tokenjuice.ai/api/v1/send-magic-link   {"email": ...}   → 204
③ 收信取 token  GET  {TEMPMAIL_BASE}/api/inbox?email=...（索引端点，只读 0~2 行）
                 邮件主题：Your Token Juice sign-in link
                 正文链接：https://tokenjuice.ai/auth/verify#token=<token>
④ 验证激活      POST https://tokenjuice.ai/api/v1/auth/verify
                 {"token": "<token>", "retainData": true}       → 204 + Set-Cookie: session=…
⑤ 会话自检      GET  https://tokenjuice.ai/api/v1/auth/me      → {"email": "..."}
⑥ 查重          GET  https://tokenjuice.ai/api/v1/api-key
                 200 = 已有 key（只回 suffix）| 404 = 没有 key
⑦ 建 key        POST https://tokenjuice.ai/api/v1/api-key  {}  → {"key","suffix","createdAt"}
⑧ 验活          GET  https://api.tokenjuice.ai/v1/models             Bearer <key>
                 POST https://api.tokenjuice.ai/v1/chat/completions  （max_tokens=8）
⑨ 入库          exports/ledger.jsonl（全部尝试） + result/（可用凭据）
```

实测单号端到端：默认 **~5.2s**；`--no-confirm-session` + 连接复用后 **~3.6s**。
50 号 @3 并发：**63s**（47.6 号/分钟）。优化过程见 `docs/optimization-2026-10-23.md`。

## 2. 快速开始

```bash
cp .env.example .env        # 填 TEMPMAIL_BASE / TEMPMAIL_ADMIN_KEY / TEMPMAIL_DOMAIN

python tools/run_e2e.py --doctor          # 体检：Worker /health、建邮箱、站点与网关可达性
python tools/run_e2e.py --count 1         # 跑一个账号
python tools/run_e2e.py --count 5 --concurrency 2
python tools/run_e2e.py --count 50 --concurrency 3 --no-confirm-session   # 跑批（省 /auth/me）
python tools/run_e2e.py --mode resume --email a@b.com      # 对已知邮箱重跑（丢包/过期）
python tools/run_e2e.py --mode scan       # 只读诊断：窗口内邮件按规则分桶 + 漏网主题
python tools/verify_keys.py               # 验收全部 key 并导出 result/
python tools/selftest.py                  # 离线自测（81 项，不碰网络）
```

产物：

| 路径 | 内容 |
| --- | --- |
| `exports/ledger.jsonl` | 运行台账：**全部尝试**（含失败），按邮箱合并、等级保护 |
| `exports/run*.json` | `--json` 指定的单次结果 |
| `result/success.jsonl` | 成功账号（账号级，每邮箱一行） |
| `result/keys.txt` | `email----api_key----suffix`（人可读） |
| `result/keys_verified.json` | 机器可读验收结果（models/chat 两层） |
| `result/apikeys.txt` | 纯 `api_key`，一行一个 |

`result/` 与 `exports/` 都被 `.gitignore` 覆盖（含明文 key）。

## 3. 从 HAR 读出来、但必须实测确认的四件事

1. **token 在 URL fragment 里**：`https://tokenjuice.ai/auth/verify#token=…`。
   浏览器不会把 `#` 后面的内容发给服务端，所以「打开链接」没有意义 ——
   真实动作是页面 JS 读 `location.hash`，再 `POST /api/v1/auth/verify`。
   ⇒ 本项目直接提取 token，**不做**「GET 链接 → 解析 HTML」那一步。

2. **会话是 `session` Cookie**（`HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`）。
   没有 Bearer / localStorage token ⇒ 必须用 `requests.Session` 保存 Cookie；
   手工拼请求头拿不到会话。HAR 里看不到 Cookie 是因为导出时被省略了，
   实测 `auth/verify` 的 204 响应带 `Set-Cookie`。

3. **链接一次性 + 15 分钟**（邮件正文原话）。所以「等不到信」的正确处置是
   **重发**（重发不会作废旧链接，实测两次发信后第一封的 token 仍可用）；
   「链接失效」的正确处置也是**重发**，不是「改请求格式」。

4. 🔴 **建 key 会轮换**：已有 key 的账号再 `POST /api/v1/api-key` 会 **200 + 新 key**，
   而**旧 key 在网关立刻变 401**（实测）。`GET /api/v1/api-key` 只回
   `suffix/createdAt/lastUsedAt`，**拿不回明文**。
   ⇒ 重跑一个已交付账号 = 销毁一把还能用的凭据。本项目有两道护栏：
   - `resume` 默认 `skip_keyed=True`（台账里已有 `api_key` 的直接跳过）；
   - `stage_create_key` 查到「已有 key 但台账无明文」时**默认不建**，记 `partial`；
     只有显式 `--rotate-key` 才允许轮换。

## 4. 错误码分流（每一条都实测过）

| 场景 | 响应 | 处置 |
| --- | --- | --- |
| 发信邮箱格式错 | `400 Invalid email address` | 换地址，不要重试 |
| 链接已用/过期 | `400 Invalid or expired sign-in link` | 重新发信（15 分钟窗口） |
| verify 请求体不对 | `400 Invalid request body` | 站点改了 schema，改 `tokenjuice.verify` 的键集 |
| 没会话 | `401 Unauthorized` | Cookie 丢了，重走登录 |
| 查 key（无 key） | `404 No API key` | **不是错误**，继续建 key |
| 建 key 被停用 | `403` | 站点侧风控/封禁，不是代码问题 |
| 网关 key 无效 | `/v1/models` `401` | 确定性失败，不重试 |
| 网关读不出来 | `/v1/models` `429/5xx` | 重试；仍失败记 `inconclusive`（**不是** key 失效） |
| 容量不足 | chat `429/5xx` | key 仍算可用（`degraded`），照常交付 |

## 5. 模块

```text
src/config.py       .env 加载 + 常量 + validate()
src/parsing.py      邮件正文 → token（纯函数，零第三方依赖）
src/mailrules.py    收件规则（SES 发件人 + 主题双条件）+ 漏网主题诊断
src/tempemail.py    CF Temp Email Worker 客户端（5xx 重试、索引端点收信）
src/tokenjuice.py   控制面客户端（send-magic-link / verify / me / api-key）
src/apicheck.py     网关验活（models + chat；网络/5xx 与 401/403 分流）
src/ledger.py       JSONL 台账（并集合并 / 等级保护 / 已赚字段 / 只写 LF）
src/stages.py       单账号阶段（`StageMixin`）+ `AccountRecord`
src/runner.py       `Pipeline`：批量 / 并发 / 台账写入
tools/run_e2e.py    主入口（full / resume / scan / --doctor）
tools/verify_keys.py 验收与交付物导出
tools/selftest.py   离线自测入口（测试本体在 tools/tests/）
```

依赖方向单向：`tools → runner → stages → 叶子 → config`，`stages` 不反向引用
`runner`。详见 `docs/architecture.md`。

## 6. 已验证的真实运行

单号（2026-10-22）：

```text
[signup] 邮箱 oai-xxxxxxxx@liziai.cloud
[login]  会话建立 (3.1s) me=oai-xxxxxxxx@liziai.cloud
[api_key] key 已建 suffix=xxxx 验活=chat (0.5s + 1.4s)
status=keyed
```

50 号 @3 并发（2026-10-23，两次）：

| 版本 | 墙钟 | 单号 mean | 50/50 | 备注 |
| --- | --- | --- | --- | --- |
| 首批 | 101 s | 5.82 s | ✅ | 无细分计时 |
| 优化前基线 | 89 s | 5.21 s | ✅ | 加细分计时 |
| 优化后 | **63 s** | **3.60 s** | ✅ | 连接复用 + chat 优先 + 跳过 `/auth/me` |

- `verify_keys.py`：`models=200 chat=200 model=deepseek-ai/DeepSeek-V4.1-Flash-1017`
- 轮换护栏实测：对同一账号 `--no-skip-keyed` 重跑 → `status=partial`，
  未调用建 key，原 key 继续可用（再次验收仍 200）。
- 期间真实遇到 `GET /v1/models` 偶发 **500**，据此把「429/5xx」从「不可用」
  改成「重试 → 仍失败记 `inconclusive`」——否则一把可用 key 会被静默剔除。

## 7. 已知限制

- 站点对「短时间大量发信」的容忍度未知 ⇒ 默认串行（`--concurrency 1`）。
- Worker 的 retention 是「全表最近 100 行」，邮件要当场读走；`--mode scan`
  是纯诊断（会占窗口读取额度），不要拿它做常规收信。
- 明文 key 只在建 key 那一次出现。台账丢失 = 该账号的 key 永久不可恢复
  （只能轮换，而轮换会作废旧 key）。**请把 `result/` 与 `exports/` 当凭据备份。**
