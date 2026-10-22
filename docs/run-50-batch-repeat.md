# 50 批次复跑（2026-10-23，默认配置）

## 配置

```bash
python tools/run_e2e.py --count 50 --concurrency 3 --json exports/run50c.json
```

与上一轮「优化后 50 号」的区别：**这次不传 `--no-confirm-session`**，
即走仓库默认路径（登录后仍 `GET /auth/me` 确认会话）。
代码为优化后的版本（每 worker 一个 clone、chat 优先验活）。

## 结果

- **50 / 50 `keyed`**，0 失败、0 重试
- `chat 200` × 50（每把 key 都在推理网关验活通过）
- 墙钟 **67 s**
- 单号 mean **3.85 s**（median 3.67 / p90 4.44 / min 2.88 / max 5.83）
- 吞吐 **44.8 号/分钟**；摊到每号墙钟 **1.34 s**

阶段分解（mean）：

| 阶段 | mean | 说明 |
| --- | --- | --- |
| `mailbox` | 0.24 s | Worker 连接按 worker 复用 |
| `send` | 0.45 s | 站点连接复用（省 TLS） |
| `mail_wait` | 0.99 s | SES 投递 |
| `verify` | 0.23 s | token 换会话 |
| `me` | 0.24 s | 默认路径的会话确认 |
| `create_key` | 0.50 s | 建 key |
| `verify_key` | 1.21 s | chat 优先，常规路径不打 models |
| **total** | **3.85 s** | |

## 与前三轮对比

| 轮次 | 墙钟 | 单号 mean | 说明 |
| --- | --- | --- | --- |
| 首批（旧代码） | 101 s | 5.82 s | 无细分计时 |
| 第二批（旧代码 + 计时） | 89 s | 5.21 s | 定位到连接未复用 |
| 优化后（`--no-confirm-session`） | 63 s | 3.60 s | 跳过 `/auth/me` |
| **本轮（默认，含 `/auth/me`）** | **67 s** | **3.85 s** | 与 3.60 + 0.24 吻合 |

> 3.85 − 3.60 = **0.25 s**，与 `me` 阶段实测 0.238 s 基本相等 ⇒
> `--no-confirm-session` 的收益是真实且可复现的，不是站点波动。
> `mail_wait` / `verify_key` 的 ±0.2 s 差异属于站点波动（两轮都是 50/50）。

## 结论

- 默认配置在 50 号规模下稳定：67 s、3.85 s/号、0 失败。
- 追求跑批吞吐时用 `--no-confirm-session`（~3.6 s/号）；
  单号排障时保留默认（多一次 `/auth/me`，失败点更早更明确）。
