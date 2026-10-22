"""推理网关验活：`https://api.tokenjuice.ai/v1`。

为什么必须做这一步：控制面返回 `tj_...` 只证明「创建接口返回了一个字符串」，
不证明它在**推理网关**上可用（可能被风控、停用、或 key 已被轮换）。

判据分两层（实测 2026-10-22）：

    GET  /v1/models            Authorization: Bearer <key>
        200 -> key 被网关接受；401 -> key 无效；403 -> 账号被停用
    POST /v1/chat/completions  最小请求（max_tokens=8）
        200 -> 真的能出 token（唯一「可用」的硬证据）

🔴 **网络层失败必须重试，不能判成「key 失效」**：`status == 0`（没拿到 HTTP
响应：SSL 抖动 / DNS / 连接被拒）与 `401`（站点明确拒绝）是两类完全不同的失败。
混在一起会让**可用**的 key 从交付清单里静默消失 —— 参考项目实测过一把 key
因 `SSLError`（0.096s，连接立即失败）被误判，手工复验第一次就 200。
判据是「**有没有拿到 HTTP 响应**」：有响应 ⇒ 确定性结论，不重试。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import requests

from . import config

#: 验活用的最小对话请求。`max_tokens=8` 是为了把成本压到最低 ——
#: 我们要的是「网关接受这个 key 并返回了 completion」，不是模型质量。
CHAT_BODY: dict[str, Any] = {
    "model": config.DEFAULT_MODEL,
    "max_tokens": 8,
    "messages": [{"role": "user", "content": "ping"}],
}


def verify_key(key: str, *, model: str | None = None,
               timeout: float = 90.0, retries: int = 2,
               session: requests.Session | None = None) -> dict[str, Any]:
    """验证一把 key。返回结构化结果，`ok` 只在**网关明确接受**时为 True。

    返回字段：

    | 字段 | 含义 |
    |---|---|
    | `ok` | key 被网关接受（models 200）且 chat 拿到了 200 |
    | `authenticated` | models 接口 200（key 有效） |
    | `degraded` | key 有效，但 chat 非 200（容量/上游问题）—— **仍算可用** |
    | `inconclusive` | models 429/5xx 重试后仍失败 —— **既不能判活也不能判死** |
    | `status` / `chat_status` | 两层各自的 HTTP 码（0 = 没拿到响应） |
    | `error` | 失败原因（只在确定性失败或不确定时非空） |
    | `attempts` | 网络层/5xx 重试了几次 |

    🔴 **`inconclusive` 是实测加出来的，不是理论洁癖**（2026-10-22 真实验收）：
    `GET /v1/models` 偶发返回 **500**（同一个 key 几分钟后连续 3 次 200）。
    旧实现把任何非 200 都当「确定性失败」⇒ 一把**完全可用**的 key 被标成不可用、
    从交付清单里消失，屏幕上只显示 `HTTP 500`。
    ⇒ 429/5xx 属于「网关现在读不出来」，与 401/403（明确拒绝）必须分开处置：
      前者重试，重试仍失败就报 `inconclusive`，绝不写进「不可用」。
    """
    s = session or requests.Session()
    s.headers.update({"User-Agent": config.UA})
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    body = dict(CHAT_BODY)
    if model:
        body["model"] = model

    t0 = time.time()
    # ── 第 1 层：models（便宜、确定性最强）──────────────────────────
    last: dict[str, Any] = {"ok": False, "authenticated": False, "status": 0,
                            "error": "未执行", "elapsed": 0.0, "attempts": 0}
    for attempt in range(retries + 1):
        try:
            r = s.get(f"{config.API_ORIGIN}/models", headers=headers,
                      timeout=timeout)
        except requests.RequestException as exc:
            last = {"ok": False, "authenticated": False, "inconclusive": False,
                    "status": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed": time.time() - t0, "attempts": attempt + 1}
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))     # 线性退避 1s / 2s
                continue
            return last
        el = time.time() - t0
        # 429 / 5xx = 网关现在读不出来 ⇒ 重试；仍失败则「不确定」，不是「key 失效」。
        if r.status_code == 429 or r.status_code >= 500:
            last = {"ok": False, "authenticated": False, "inconclusive": True,
                    "status": r.status_code,
                    "error": (r.text or "").strip()[:200] or f"HTTP {r.status_code}",
                    "elapsed": el, "attempts": attempt + 1}
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return last
        if r.status_code != 200:
            # 401/403 等明确拒绝 ⇒ 确定性结论，不重试（重试一万次还是 401）
            return {"ok": False, "authenticated": False, "inconclusive": False,
                    "status": r.status_code,
                    "error": (r.text or "").strip()[:200] or f"HTTP {r.status_code}",
                    "elapsed": el, "attempts": attempt + 1}
        models = []
        with contextlib.suppress(ValueError):
            models = [m.get("id") for m in (r.json() or {}).get("data") or []]
        break
    else:  # pragma: no cover —— 上面的分支必然 break 或 return
        return last

    # ── 第 2 层：chat（真打一次，拿 completion）──────────────────────
    out: dict[str, Any] = {
        "ok": False, "authenticated": True, "degraded": False,
        "status": 200, "chat_status": 0,
        "model": body["model"], "models": models, "elapsed": el,
        "attempts": attempt + 1,
    }
    try:
        c = s.post(f"{config.API_ORIGIN}/chat/completions", headers=headers,
                   json=body, timeout=timeout)
    except requests.RequestException as exc:
        # 已认证但 chat 网络失败：key 本身没问题，标降级而不是判死
        out.update(degraded=True, chat_status=0,
                   error=f"chat 网络失败: {type(exc).__name__}: {exc}")
        return out

    out["chat_status"] = c.status_code
    out["elapsed"] = time.time() - t0
    if c.status_code == 200:
        try:
            j = c.json() or {}
        except ValueError:
            j = {}
        choice = (j.get("choices") or [{}])[0]
        out.update(ok=True, degraded=False,
                   reply=(choice.get("message") or {}).get("content", ""),
                   usage=j.get("usage") or {})
        return out
    # 429 / 5xx = 网关接受了 key，但容量或上游有问题 ⇒ 不算 key 失效
    if c.status_code == 429 or c.status_code >= 500:
        out.update(degraded=True,
                   error=(c.text or "").strip()[:200] or f"HTTP {c.status_code}")
        return out
    # 401 / 403 等 ⇒ 确定性失败
    out.update(error=(c.text or "").strip()[:200] or f"HTTP {c.status_code}")
    return out
