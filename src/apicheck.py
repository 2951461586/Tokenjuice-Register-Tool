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
    | `ok` | chat 拿到了 200（最强证据） |
    | `authenticated` | key 被网关接受（chat 200，或 chat 无结论时 models 200） |
    | `degraded` | 已认证，但 chat 非 200（容量/上游/请求体问题）—— **仍算可用** |
    | `inconclusive` | 两层都 429/5xx/网络失败 —— **既不能判活也不能判死** |
    | `status` / `chat_status` | 结论层/chat 的 HTTP 码（0 = 没拿到响应） |
    | `error` | 失败原因（只在确定性失败或不确定时非空） |
    | `attempts` | 结论层的网络/5xx 重试次数 |

    🔴 **chat 优先是实测优化**（2026-10-23）：models 452ms + chat 562ms，
    而 chat 200 已同时证明「key 被接受」与「能出 token」⇒ 常规路径省一次 RTT。
    chat 401/403 同样足以确定性判死；只有 429/5xx/400/网络失败才回落到 models。

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
    # ── 第 1 层：chat（**最强证据**；200 时完全不需要 models）──────────
    #
    # 为什么把 chat 放前面（2026-10-23 实测优化）：
    #   models 452ms + chat 562ms，而 chat 200 已经证明「网关接受这个 key 且能出 token」，
    #   此时 models 是纯冗余的一次 RTT（约 0.45s/号）。反过来，chat 401/403 也
    #   足以确定性判死，同样不需要 models。
    #   ⇒ 只有 chat 给出**非确定性**结果（429/5xx/400/网络）时才回落到 models
    #     去回答「key 到底有没有被接受」。
    chat_status = 0
    chat_err = ""
    for attempt in range(retries + 1):
        try:
            c = s.post(f"{config.API_ORIGIN}/chat/completions", headers=headers,
                       json=body, timeout=timeout)
        except requests.RequestException as exc:
            chat_err = f"chat 网络失败: {type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            break
        el = time.time() - t0
        if c.status_code == 200:
            j: dict[str, Any] = {}
            with contextlib.suppress(ValueError):
                j = c.json() or {}
            choice = (j.get("choices") or [{}])[0]
            return {"ok": True, "authenticated": True, "degraded": False,
                    "inconclusive": False, "status": 200, "chat_status": 200,
                    "model": body["model"], "models": [],
                    "reply": (choice.get("message") or {}).get("content", ""),
                    "usage": j.get("usage") or {}, "elapsed": el,
                    "attempts": attempt + 1}
        if c.status_code in (401, 403):
            # 明确拒绝 ⇒ 确定性结论，不重试、也不打 models
            return {"ok": False, "authenticated": False, "degraded": False,
                    "inconclusive": False, "status": c.status_code,
                    "chat_status": c.status_code,
                    "error": (c.text or "").strip()[:200] or f"HTTP {c.status_code}",
                    "elapsed": el, "attempts": attempt + 1}
        # 429 / 5xx / 400 …：chat 不能给结论 ⇒ 记下来，交给 models 判「认证」
        chat_status = c.status_code
        chat_err = (c.text or "").strip()[:200] or f"HTTP {c.status_code}"
        break

    # ── 第 2 层：models（只在 chat 不能给结论时才打）──────────────────
    last: dict[str, Any] = {"ok": False, "authenticated": False,
                            "degraded": False, "inconclusive": True,
                            "status": 0, "chat_status": chat_status,
                            "error": chat_err or "未执行",
                            "elapsed": time.time() - t0, "attempts": 0}
    for attempt in range(retries + 1):
        try:
            r = s.get(f"{config.API_ORIGIN}/models", headers=headers,
                      timeout=timeout)
        except requests.RequestException as exc:
            last = {"ok": False, "authenticated": False, "degraded": False,
                    "inconclusive": True, "status": 0,
                    "chat_status": chat_status,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed": time.time() - t0, "attempts": attempt + 1}
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return last
        el = time.time() - t0
        # 429 / 5xx = 网关现在读不出来 ⇒ 重试；仍失败则「不确定」，不是「key 失效」。
        if r.status_code == 429 or r.status_code >= 500:
            last = {"ok": False, "authenticated": False, "degraded": False,
                    "inconclusive": True, "status": r.status_code,
                    "chat_status": chat_status,
                    "error": ((r.text or "").strip()[:200]
                              or f"HTTP {r.status_code}"),
                    "elapsed": el, "attempts": attempt + 1}
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return last
        if r.status_code != 200:
            # 401/403 等明确拒绝 ⇒ 确定性结论，不重试
            return {"ok": False, "authenticated": False, "degraded": False,
                    "inconclusive": False, "status": r.status_code,
                    "chat_status": chat_status,
                    "error": ((r.text or "").strip()[:200]
                              or f"HTTP {r.status_code}"),
                    "elapsed": el, "attempts": attempt + 1}
        models = []
        with contextlib.suppress(ValueError):
            models = [m.get("id") for m in (r.json() or {}).get("data") or []]
        # models 200 = key 被接受；chat 失败是容量/上游/请求体问题 ⇒ 降级但可用
        return {"ok": False, "authenticated": True, "degraded": True,
                "inconclusive": False, "status": 200,
                "chat_status": chat_status, "model": body["model"],
                "models": models, "error": chat_err,
                "elapsed": el, "attempts": attempt + 1}
    return last
