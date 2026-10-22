# 密钥与隐私暴露面审计（2026-10-23）

范围：本仓库工作区 + git 历史 + 被 gitignore 的运行产物 + 两个 HAR
（HAR 已于 2026-10-23 删除，见 §5）。
方法：`git ls-files` / `git grep HEAD` / `git log -p --all` / 全盘正则扫描 /
`icacls` / 对 HAR 里的凭据做**只读**验活（`GET /v1/models`）。

## 结论（按严重度）

| # | 级别 | 发现 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 🔴 高 | **HAR 里有 1 把仍然有效的明文 key** | `tokenjuice-2.ai.har` 含 `tj_<redacted>`，`GET /v1/models` 返回 **200** | 文件已删除；**key 仍有效 → 轮换**（§3） |
| 2 | 🔴 高 | **Worker Admin Key 已在本对话中明文出现** | 用户消息 + `.env` | 建议轮换（§3） |
| 3 | 🟠 中 | `.env` / `result/` / `exports/` / HAR 的 ACL 为 `BUILTIN\Users:(F)`，本机任意用户可读写 | `icacls` | ✅ 已收紧为 `LIZI\29514:(F)` |
| 4 | 🟠 中 | 运行日志打印 **key 前 20 字符**（50 条） | `exports/run50.log` | ✅ 代码改为只打 suffix；旧日志已脱敏 |
| 5 | 🟡 低 | 已跟踪文档含真实邮箱 + key 前 20 字符 + Worker 子域 | `README.md` / `docs/run-50-batch.md` | ✅ 已改为占位符 / 只留 `.env` |
| 6 | 🟡 低 | git 提交作者邮箱是个人 QQ 邮箱 | `git log` | 仅提示：不想公开可改 `user.email` |
| 7 | ⚪ 信息 | git 历史/已跟踪文件**没有** admin key、完整 key、session | `git log -p --all` 零命中 | ✅ |
| 8 | ⚪ 信息 | `result/` 与 `exports/` 按设计含 51 把明文 key + 邮箱 | 交付物 | 必须当凭据保管（§4） |

## 1. 已跟踪文件

- 27 个文件，全部为源码/文档/配置；`.env`、`*.har`、`result/`、`exports/`、
  `__pycache__/` 均被 `.gitignore` 覆盖（`git check-ignore -v` 逐条确认）。
- 代码里**没有任何真实默认值**：`config.TEMPMAIL_*` 默认空串，
  `SITE_ORIGIN` / `API_ORIGIN` 是公开地址。
- 历史提交扫描（`git log -p --all`）对 `<admin key>`、`tj_…`、
  `session=…` 零命中 ⇒ 没有「提交后才删」的历史泄漏。

## 2. 运行产物（gitignored，但在磁盘上）

| 文件 | 内容 | 风险 |
| --- | --- | --- |
| `.env` | Worker Admin Key | 可建任意邮箱、读全表邮件 |
| `exports/ledger.jsonl` | 51 条账号 + 明文 key + 邮件元数据 | 凭据 |
| `exports/run50.json` | 50 条完整记录（含明文 key） | 凭据 |
| `exports/*.log` | 邮箱、耗时；**已脱敏** | 低 |
| `result/keys.txt` / `apikeys.txt` / `keys_verified.json` / `success.jsonl` | 51 把明文 key | 交付凭据 |
| `tokenjuice-2.ai.har` | **1 把有效 key** + 邮箱 + 一次性 token | ✅ 已于 2026-10-23 删除（key 仍有效，见 §3） |
| `tokenjuice-1.ai.har` | 邮箱 + 一次性 token（已过期） | ✅ 已于 2026-10-23 删除 |

> 两个 `.har` 已按用户要求于 2026-10-23 **删除**。
> ⚠️ 删除文件**不等于吊销 key** —— `tj_<redacted>` 仍然有效，见 §3。
> 但它是「分享抓包」场景里最容易被忽略的凭据泄漏点：响应体里就有明文 key。

## 3. 待处置（需要人决定，工具不自动做）

1. **HAR 里那把 key**：`tj_<redacted>`（账号 `epsoft01@liziai.cloud`）。
   HAR 文件已删除，但**这把 key 仍然有效**；站点没有独立吊销接口，只能轮换：
   `python tools/run_e2e.py --mode resume --email epsoft01@liziai.cloud --no-skip-keyed --rotate-key`
   ⚠️ 如果这把 key 正被其它系统使用，轮换前先改下游配置。
   ⚠️ 如果这把 key 正被其它系统使用，轮换前先改下游配置。
2. **Worker Admin Key**：已在聊天中明文出现 ⇒ 按「已泄漏」处理，去 Cloudflare 侧
   重建/轮换，更新 `.env` 后跑 `python tools/run_e2e.py --doctor` 复验。
3. 本机若有其它用户账号，确认 `LIZI\29514` 是唯一需要访问本目录的账号。

## 4. 长期建议

- `result/` 与 `exports/` 视为**凭据库**：备份加密、不投递到共享盘/聊天工具；
  交付时只发 `apikeys.txt`（纯 key）或按需发 `keys.txt`。
- 不要把 HAR 提交或发给第三方；需要留证时先删掉 `response.content.text`
  里的 `key` 字段与邮箱。
- 跑批日志只应出现 `suffix`（末 4 位）—— 本项目已按此实现并有自测钉住
  （`test_chain_uses_single_source_for_verify` 之外，日志格式在
  `stages.stage_create_key` 里只有 suffix）。
- 若要把本仓库推到远端：先 `git grep -n -E "tj_|Zhy\.|session="` 自查，
  再确认 `.env` / `*.har` / `result/` / `exports/` 仍在 ignore 列表里。

## 5. 本次已执行的修复

```text
src/stages.py            日志从 key[:20] 改为只打 suffix
README.md                真实邮箱 → oai-xxxxxxxx@…；key 前缀 → key 已建 suffix=xxxx
docs/run-50-batch.md     Worker 子域 → 指向 .env
icacls .env *.har                继承移除 + 仅 LIZI\29514 完全控制
icacls result exports /T         同上；⚠️ 实测 `/T /inheritance:r` 会把子文件的继承
                                 ACE 删掉却不补显式 ACE（DACL 变空 ⇒ 连属主都读不了）。
                                 必须再对**每个文件**执行
                                 `icacls <file> /grant:r "LIZI\29514:(F)"`。
exports/*.log            50 处 key 前缀 → tj_<redacted>…
```
