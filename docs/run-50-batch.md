# 50 批次全链路压测记录（2026-10-23）

## 配置

| 项 | 值 |
|---|---|
| 命令 | `python tools/run_e2e.py --count 50 --concurrency 3 --json exports/run50.json` |
| 邮箱后端 | CF Temp Email Worker（`temp-email-worker.zhuhaoyi181.workers.dev`） |
| 邮箱域名 | `liziai.cloud` |
| 登录模式 | 魔法链接（`#token=`，一次性 / 15 分钟） |
| 验活 | `GET /v1/models` + `POST /v1/chat/completions`（`max_tokens=8`） |
| 结果文件 | `exports/run50.json`（gitignored，含凭据） |

## 结果

- **50 / 50 `keyed`**：全部建出 key 且网关验活通过（`chat_status=200` × 50）。
- 0 次发信重试（`send_retries=0` × 50）⇒ 这批没有丢包。
- 50 把 key / 50 个 suffix 全部唯一，无串号。
- 台账 51 条（含此前 1 条单号验证）。

## 速率

| 指标 | 数值 |
|---|---|
| 墙钟总耗时 | **101 s** |
| 吞吐 | **0.495 号/秒 ≈ 29.7 号/分钟** |
| 摊到每号的墙钟 | **2.02 s/号** |
| 单号全链路耗时（mean） | **5.82 s** |
| 单号 median / p90 / min / max | 5.82 / 6.24 / 4.99 / 7.22 s |
| 并发加速比 | 5.82 ÷ 2.02 ≈ **2.88×**（3 并发） |

阶段分解（单号均值）：

| 阶段 | mean | median | max |
|---|---|---|---|
| `mail_wait`（等信） | 0.98 s | 0.99 s | 1.74 s |
| `login`（发信 + 等信 + 换会话 + me） | 3.65 s | 3.63 s | 4.42 s |
| `create_key` | 0.45 s | 0.44 s | 0.51 s |
| `verify_key`（models + chat） | 1.73 s | 1.66 s | 2.90 s |
| `total` | **5.82 s** | 5.82 s | 7.22 s |

> `total` 是端到端实测（进 `run_one` → 出函数），不是各阶段相加推导：
> 建邮箱失败/等信超时等路径也必须留痕（本项目台账铁律）。

## 独立复验

```
python tools/verify_keys.py     # 51 条记录，80 s
可用 51 / 不确定 0 / 不可用 0 / 合计 51
```

即 50 把新 key + 此前 1 把，全部在推理网关上 `models=200`、`chat=200`。

## 结论

- 3 并发下，50 号 101 秒完成，单号摊薄 2.02 秒；瓶颈是**串行的单号等待**
  （`login` 占 63%，其中等信 0.98 s 已是实测下限），不是站点限流。
- 这批 0 丢包、0 重试、0 降级 ⇒ `MAIL_TIMEOUT=90s` 的余量充足，
  但重发机制仍然保留（丢包是概率事件，不是这批没发生就不存在）。
- 轮换护栏未被触发：50 个邮箱都是新建，`GET /api-key` 均为 404 后才建 key。

## 注意

`exports/` 与 `result/` 含明文 key，已被 `.gitignore` 覆盖；
HAR 文件含会话/一次性 token，同样不入库。
