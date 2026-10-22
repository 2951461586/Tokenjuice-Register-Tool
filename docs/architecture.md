# 架构

## 1. 链路与状态机

```
                     ┌──────────────────────────────┐
                     │  Pipeline.run_one()          │
                     │  （runner.py：调度 + 台账）   │
                     └──────────────┬───────────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │ stage_login()                             │
              │  1. mail.create_mailbox()                 │  ← CF Worker（免费）
              │  2. cl.send_magic_link(email)             │  ← 控制面 204
              │  3. mail.wait_for_mail(MATCH_LINK)        │  ← 索引端点轮询
              │  4. parsing.extract_magic_tokens(body)    │  ← #token=
              │  5. cl.verify(token) 逐枚试                │  ← 204 + session Cookie
              │  6. cl.me()                               │  ← {"email"}
              └─────────────────────┬─────────────────────┘
                                    │ 返回已建会话的 client
              ┌─────────────────────▼─────────────────────┐
              │ stage_create_key()                        │
              │  7. cl.get_api_key()  ← 404 = 没有，继续   │
              │     └─ 200 = 已有：无明文则不建（护栏）    │
              │  8. cl.create_api_key()                   │  ← 唯一一次明文
              │  9. apicheck.verify_key(key)              │  ← models + chat
              │ 10. success_ledger.append(rec)            │
              └───────────────────────────────────────────┘
```

状态：`started → registered → keyed`，分支 `partial`（会话已建立但没交付可用 key）
与 `failed`。`keyed` 的判据是**网关接受 key**，不是「创建接口返回了字符串」。

## 2. 模块边界与依赖方向

```
tools/run_e2e.py ─┐
tools/verify_keys.py ─┼─→ src.runner ─→ src.stages ─→ { tokenjuice, apicheck,
                      │                              tempemail, parsing,
                      │                              mailrules, ledger }
                      └─→ src.ledger / src.apicheck（只读工具）
```

- `stages` **不** import `runner`（`AccountRecord` 定义在 `stages`，`runner` 导入它）。
- `parsing` / `mailrules` 只依赖 `config`，可脱离 `requests` 单独测试。
- `apicheck` 是验活的**唯一实现**：生产阶段与 `verify_keys.py` 共用。
  自测 `test_chain_uses_single_source_for_verify` 钉住这一点。

## 3. 数据模型

`AccountRecord`（`src/stages.py`）：

| 字段 | 说明 |
|---|---|
| `key` / `email` | 主键 = 邮箱（写入边界削首尾空白，防 CR 污染） |
| `status` | `started` / `registered` / `keyed` / `partial` / `failed` |
| `stages` | 每步结果（`mailbox` / `send` / `mail` / `verify` / `me` / `api_key`） |
| `timings` | `login` / `mail_wait` / `create_key` / `verify_key` / `total`（**失败也写**） |
| `api_key` | 明文（只在建 key 时出现一次） |
| `api_key_suffix` / `api_key_created_at` | 服务端可查回的元数据 |
| `login` | 邮件主题/到达时间/候选 token 数/重发次数 |
| `user` | `/auth/me` 快照 + 验活结果 |

台账合并（`src/ledger.py`）：

- 同键**并集**合并，`RANK` 决定升级/降级；低等级记录不覆盖高等级记录。
- `EARNED_FIELDS`（`api_key` / `api_key_suffix` / `api_key_created_at` / `email`）
  一旦有值，**不允许被空值覆盖**。
- `DICT_FIELDS`（`stages` / `timings` / `login` / `user`）做并集。
- JSONL 只写 LF（`newline=""`），否则 Windows 上每行尾部带 `\r`。

## 4. 为什么这些阈值/护栏是这样

| 决策 | 依据 |
|---|---|
| `MAIL_TIMEOUT = 90s` | 实测 1~3s 必到；90s 有 ~30 倍余量 |
| `MAIL_POLL_INTERVAL = 0.5s` | 打自己的 Worker（索引端点），不碰站点 |
| 超时后批内**重发 1 次** | 丢包比「慢」常见；重发不作废旧链接（实测） |
| `skip_keyed=True` 默认 | 建 key 会轮换并作废旧 key（实测） |
| 已有 key + 无明文 ⇒ 不建 | 明文取不回，重建 = 销毁交付凭据 |
| 验活交付判据 = `models 200` | chat 的 429/5xx 是容量问题，不是 key 失效 |
| 429/5xx 重试后记 `inconclusive` | 实测 `/v1/models` 偶发 500，旧判据会误杀可用 key |
| 收信走 `/api/inbox?email=` | Worker 全表只有 100 行窗口，拉列表会被邻居项目挤掉 |
| 每个 worker 一个 clone（不是每任务） | 每号省 2 次 TLS 握手（冷 660ms vs 热 223ms） |
| 验活 chat 优先 | chat 200 已证明「被接受 + 能出 token」，models 是冗余 RTT |
| `/auth/me` 可关（默认开） | 随后的 `GET /api-key` 401 已能兜住会话失效 |
| 轮询 0.5s（试过 0.3s） | 实测无收益：瓶颈是 SES 投递，不是轮询粒度 |

## 5. 测试策略

`tools/selftest.py` 是聚合入口，`tools/tests/` 分四块：

- **parsing**：fragment/查询串/HTML 实体/截断 token/跨域负对照。
- **mailrules**：sender + subject 双条件、漏网主题诊断。
- **ledger**：并集、等级保护、已赚字段、状态词汇 AST 扫描、LF。
- **orchestration**：全链路、发信/验证失败分流、超时重发、轮换护栏、
  `resume` 去重、并发不串号、worker 崩溃补台账、验活判据（含 5xx 重试）。

全部离线：`tests/support.py::offline()` 替换三个接缝
（`stages.TokenJuiceClient` / `stages.verify_key` / `runner.make_mail_client`）。
