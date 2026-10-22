# 第二批 50 批次 + 优化记录（2026-10-23）

## 1. 第二批基线（加细分计时，行为未变）

```bash
python tools/run_e2e.py --count 50 --concurrency 3 --json exports/run50b.json
```

- 墙钟 **89 s**，50/50 `keyed`，0 重试，`chat 200` × 50
- 单号 mean **5.21 s**（median 5.11 / p90 5.77）
- 与第一批（101 s / 5.82 s）的差异是站点波动，不是代码改动

细分阶段（mean）：

| 阶段 | mean | 说明 |
| --- | --- | --- |
| `mailbox` | 0.70 s | 建邮箱（Worker + D1） |
| `send` | 0.96 s | 发登录链接（**含新 Session 的 TLS 握手**） |
| `mail_wait` | 1.09 s | 等信（SES 投递 + 轮询粒度） |
| `verify` | 0.25 s | token 换会话 |
| `me` | 0.24 s | 会话确认 |
| `create_key` | 0.48 s | 建 key |
| `verify_key` | 1.48 s | models + chat **两次 RTT** |
| **total** | **5.21 s** | |

## 2. 找到的三个真问题

### 2.1 `_fan_out` 每个任务都 `_clone()` ⇒ 会话复用根本没生效

旧实现：`pool.submit(fn, self._clone(), key)` —— 提交 50 个任务就造 50 个
`Pipeline`，每个 Pipeline 自带新的 `requests.Session`（站点 + Worker 各一套）。
实测冷连接 **~660 ms**、热连接 **~223 ms** ⇒ 每号白付约 0.44 s × 2。

更隐蔽的是：串行路径（`concurrency=1`）**不走这段代码**，所以单号测试永远
复现不出来 —— 典型的「看着优化了，其实没生效」。

**修法**：按 `pos % concurrency` 轮转分片，**每个 worker 一个 clone**，
分片内顺序消费；连接池、站点会话、邮箱客户端都按 worker 复用。
切号时 `TokenJuiceClient.reset()` 清 Cookie（不清就会串号）。

### 2.2 验活先打 `models` 再打 `chat` ⇒ 常规路径多一次 RTT

实测 `models` 452 ms、`chat` 562 ms；而 **chat 200 已同时证明**「key 被接受」
与「能出 token」。`chat` 401/403 也足以确定性判死。
⇒ 改成 **chat 优先**，只有 chat 给出非确定性结果（429/5xx/400/网络失败）时
才回落到 `models` 回答「key 到底有没有被接受」。

交付判据不变：`authenticated`（chat 200 或 models 200）。

### 2.3 `/auth/me` 是冗余的一次 RTT

`stage_create_key` 紧接着就会 `GET /api-key`：会话失效会直接 401。
所以 `/auth/me` 不是必需项，只是「更早、更明确」的失败点。
加 `--no-confirm-session`（默认仍开启，安全优先），跑批时可省 ~0.24 s/号。

### 2.4 试过但**没有收益**的改动

把轮询间隔 0.5 s → 0.3 s：实测 `mail_wait` 没有可测变化（瓶颈是 SES 投递，
不是轮询粒度），反而把 Worker 请求量翻倍 ⇒ **已回退到 0.5 s**。
（这条记录保留，避免下次有人再“优化”一遍。）

## 3. 优化后（50 批次，3 并发）

```bash
python tools/run_e2e.py --count 50 --concurrency 3 --no-confirm-session \
       --json exports/run50opt.json
```

- 墙钟 **63 s**（89 → 63，**-29%**）
- 50/50 `keyed`，0 重试，`chat 200` × 50
- 单号 mean **3.60 s**（median 3.31 / p90 4.78）—— **-31%**
- 吞吐 **47.6 号/分钟**（基线 33.7，首批 29.7）
- 摊到每号墙钟 **1.26 s**（基线 1.78，首批 2.02）

逐阶段对比：

| 阶段 | 基线 | 优化后 | Δ | 原因 |
| --- | --- | --- | --- | --- |
| `mailbox` | 0.70 | **0.24** | -0.46 | Worker Session 按 worker 复用 |
| `send` | 0.96 | **0.41** | -0.55 | 站点 Session 按 worker 复用（省 TLS） |
| `mail_wait` | 1.09 | 1.18 | +0.09 | 站点波动；轮询间隔已回退 0.5 s |
| `verify` | 0.25 | 0.22 | -0.03 | 同连接 |
| `me` | 0.24 | **0.00** | -0.24 | `--no-confirm-session` |
| `create_key` | 0.48 | 0.47 | -0.01 | 不变 |
| `verify_key` | 1.48 | **1.08** | -0.40 | chat 优先，常规路径省掉 models |
| **total** | **5.21** | **3.60** | **-1.61** | |

> 默认（不带 `--no-confirm-session`）仍会打 `/auth/me`，单号约 3.84 s；
> 跑批建议显式关掉，单号调试建议保留。

## 4. 回归

优化过程中真实踩到一个回归，已修并加自测：

```text
UnboundLocalError: cannot access local variable 'me'
```

跳过 `/auth/me` 后，`stage_login` 末尾的日志仍引用局部变量 `me`
⇒ 20/20 全部失败，而默认路径（`confirm_session=True`）完全正常。
教训：**开关的两侧都要有测试**。现在 `test_full_chain_without_confirm_session`
钉住 `me=skipped` + `timings["me"]==0` + 流程仍 `keyed`。

自测：**87 / 87 通过**；`pyright` 0 error；`ruff` clean。

## 5. 还能做什么（按收益/风险排序）

1. **预建邮箱池**（预计 -0.2~0.25 s/号，风险中）：
   `mailbox` 仍在关键路径上。可以在等信（~1.1 s）期间由后台线程预建下一个邮箱，
   把 0.24 s 藏进等待里。代价是引入后台线程与「池里剩几个」的状态，
   失败时还要处理「预建的邮箱没用上」的泄漏。
2. **提高并发**（收益线性，风险未知）：3 → 6 预计墙钟再降 ~45%，
   但站点对「短时间大量发信」的容忍度**没有实测数据**。建议先 10 号 @6 探测，
   观察 429/超时率，再决定是否放开。
3. **`create_key` 0.47 s** 是单次 RTT，无优化空间（服务端写库）。
4. **`mail_wait` 1.18 s** 是 SES 投递，客户端不可控；再降轮询间隔已验证无收益。

## 6. 结论

- 本轮优化是**结构性**的（连接复用 + 去掉冗余 RTT），不是调参：
  单号 -31%，墙钟 -29%，且 50/50 成功率不变。
- 最大的教训是「会话复用写在 `_clone()` 里但每个任务都 clone 一次」——
  这类问题在串行路径上永远看不到，必须用**并发跑批 + 细分计时**才能暴露。
