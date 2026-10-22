"""Token Juice 控制面客户端（`tokenjuice.ai/api/v1`）。

链路（全部纯 HTTP，无浏览器、无 Server Action、无验证码）：

    1. POST /api/v1/send-magic-link  {"email": …}                 -> 204
    2. 收邮件 -> 从 https://tokenjuice.ai/auth/verify#token=… 里取 token
    3. POST /api/v1/auth/verify      {"token": …, "retainData": true}
                                                                 -> 204 + Set-Cookie: session=…
    4. GET  /api/v1/auth/me                                       -> {"email": …}
    5. GET  /api/v1/api-key                                       -> 200（已有）| 404 No API key
    6. POST /api/v1/api-key          {}                           -> {"key","suffix","createdAt"}

规格真源：`tokenjuice-2.ai.har`（抓包）+ `assets/api-DoqthcYR.js`（前端 SDK 原文）。
前端 SDK 的关键语义（照抄会踩的坑，已在代码里落地）：

- `GET /api/v1/api-key` 的 **404 不是错误**，是「这个账号还没有 key」
  （`api-DoqthcYR.js`：`if (status===404) return null`）。
  把它当失败会让「新账号 → 建 key」这条主路径在第一步就断。
- `POST /api/v1/api-key` 在**已有 key 时也返回 200，并生成新 key**（实测）。
  ⇒ 旧 key 立刻失效（实测：轮换后第一把 key 在网关变 401）。
  所以「重跑」绝不能无脑再建一次 key —— 见 `stages.stage_create_key` 的轮换护栏。
- 会话是 **`session` HttpOnly Cookie，30 天**；`requests.Session` 自动保存即可。
  没有 Bearer / localStorage token，所以会话不可从 HAR 里「复制请求头」重放。

⚠️ 本模块只做「发请求 / 判状态码 / 组装 Result」，解析在 `parsing.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from . import config


class TokenJuiceError(RuntimeError):
    def __init__(self, msg: str, *, status: int | None = None, code: str = ""):
        super().__init__(msg)
        self.status = status
        self.code = code


@dataclass
class Result:
    ok: bool = False
    stage: str = ""
    error: str = ""
    status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


def _body_text(r: requests.Response) -> str:
    """错误体是**纯文本**（`Invalid or expired sign-in link`），不是 JSON。

    前端 `ApiError` 也是 `await r.text()`，所以这里保持同构；
    万一是 JSON 就原样带上，不做猜测性解析。
    """
    try:
        return (r.text or "").strip()[:300]
    except Exception:  # noqa: BLE001
        return ""


class TokenJuiceClient:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": config.UA})
        self.email = ""
        self.log: list[str] = []

    def reset(self) -> None:
        """切到下一个账号：清 Cookie / 邮箱 / 日志，但**保留连接池**。

        🔴 复用 Session 是跑批优化（实测冷连接 ~660ms、热连接 ~223ms，
        省下的是 TLS 握手），但**不清 Cookie 就会串号** —— 上一号的
        `session` Cookie 会让下一号的 `get_api_key` 查到别人的 key。
        """
        self.s.cookies.clear()
        self.email = ""
        self.log.clear()

    # ── 公共头 ────────────────────────────────────────────────────────
    @staticmethod
    def _headers(referer: str, *, json_body: bool = False) -> dict[str, str]:
        h = {"Origin": config.SITE_ORIGIN, "Referer": f"{config.SITE_ORIGIN}{referer}",
             "Accept": "*/*"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    # ── 1. 发登录链接 ─────────────────────────────────────────────────
    def send_magic_link(self, email: str) -> Result:
        """提交邮箱，触发登录邮件。成功是 **204（无响应体）**。

        错误分流（实测）：
            400 Invalid email address —— 邮箱格式被服务端拒（不要重试，换地址）
            5xx                      —— 站点故障（可重试/稍后重跑）
        """
        r = self.s.post(
            f"{config.SITE_ORIGIN}{config.SEND_LINK_PATH}",
            json={"email": email},
            headers=self._headers("/", json_body=True),
            timeout=30,
        )
        self.email = email
        self.log.append(f"send_magic_link({email}) HTTP {r.status_code}")
        ok = r.status_code == 204
        err = ""
        if not ok:
            body = _body_text(r)
            if r.status_code == 400 and "Invalid email" in body:
                err = f"邮箱格式被拒（HTTP 400: {body}）—— 换地址，不要重试"
            else:
                err = f"发信 HTTP {r.status_code}: {body}"
        return Result(ok=ok, stage="send_magic_link", status=r.status_code,
                      error=err, data={"body": _body_text(r)})

    # ── 2. 用 token 换会话 ────────────────────────────────────────────
    def verify(self, token: str, *, retain_data: bool = True) -> Result:
        """`POST /api/v1/auth/verify` —— 一次性 token 换 `session` Cookie。

        `retainData=true` 是 HAR 里前端真实提交的值（保留账号数据）。
        成功 204 且响应带 `Set-Cookie: session=…; Max-Age=2592000`。

        错误分流（实测，三种码处置完全不同）：
            400 Invalid or expired sign-in link —— token 已被用过 / 超过 15 分钟
                                                  ⇒ **重发信**，不是「链接格式错」
            400 Invalid request body           —— 请求体 schema 变了（缺 token 键）
            401 Unauthorized                   —— 没有/失效的会话（这里不该出现）
        """
        r = self.s.post(
            f"{config.SITE_ORIGIN}{config.VERIFY_PATH}",
            json={"token": token, "retainData": retain_data},
            headers=self._headers(config.SITE_VERIFY_PATH, json_body=True),
            timeout=30,
        )
        self.log.append(f"verify(token=…{token[-6:]}) HTTP {r.status_code}")
        body = _body_text(r)
        ok = r.status_code == 204
        err = ""
        if not ok:
            if r.status_code == 400 and "expired sign-in link" in body:
                err = ("token 已失效（一次性 + 15 分钟）—— 需重新发信，"
                       "不是链接格式问题")
            elif r.status_code == 400 and "Invalid request body" in body:
                err = ("请求体被拒（HTTP 400）—— 站点改了 verify 的 schema，"
                       "改 src/tokenjuice.py::verify 的键集")
            else:
                err = f"verify HTTP {r.status_code}: {body}"
        return Result(ok=ok, stage="verify", status=r.status_code, error=err,
                      data={"body": body})

    def session_cookie(self) -> str:
        return self.s.cookies.get("session", "") or ""

    # ── 3. 会话自检 ───────────────────────────────────────────────────
    def me(self) -> dict[str, Any] | None:
        r = self.s.get(f"{config.SITE_ORIGIN}{config.ME_PATH}",
                       headers=self._headers("/app"), timeout=30)
        if r.status_code != 200:
            self.log.append(f"me() HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def signout(self) -> Result:
        r = self.s.post(f"{config.SITE_ORIGIN}{config.SIGNOUT_PATH}",
                        headers=self._headers("/app"), timeout=30)
        return Result(ok=r.status_code == 204, stage="signout",
                      status=r.status_code)

    # ── 4. API Key ────────────────────────────────────────────────────
    def get_api_key(self) -> Result:
        """查当前会话的 key 元数据。

        🔴 **404 = 没有 key，不是失败**（前端 SDK 同判据）。
        200 的响应**不含明文** —— 只有 `suffix` / `createdAt` / `lastUsedAt`
        （实测：`{"suffix":"ELRI","createdAt":"…","lastUsedAt":null}`）。
        ⇒ 明文只在建 key 那一次出现，丢了就取不回来。
        """
        r = self.s.get(f"{config.SITE_ORIGIN}{config.API_KEY_PATH}",
                       headers=self._headers("/app"), timeout=30)
        self.log.append(f"get_api_key HTTP {r.status_code}")
        if r.status_code == 404:
            return Result(ok=True, stage="get_api_key", status=404,
                          data={"exists": False})
        if r.status_code != 200:
            return Result(ok=False, stage="get_api_key", status=r.status_code,
                          error=f"查 key HTTP {r.status_code}: {_body_text(r)}")
        try:
            data = r.json() or {}
        except ValueError:
            data = {}
        data["exists"] = True
        return Result(ok=True, stage="get_api_key", status=200, data=data)

    def create_api_key(self) -> Result:
        """建 key。请求体是 `{}`（HAR 原文），响应含**唯一一次**明文 `key`。

        🔴 已有 key 时调用**也会 200 并生成新 key**，同时旧 key 在网关失效。
        调用方必须先 `get_api_key()` 判断，见 `stages.stage_create_key`。
        """
        r = self.s.post(f"{config.SITE_ORIGIN}{config.API_KEY_PATH}", json={},
                        headers=self._headers("/app", json_body=True), timeout=40)
        self.log.append(f"create_api_key HTTP {r.status_code}")
        body: dict[str, Any] = {}
        try:
            body = r.json() or {}
        except ValueError:
            body = {}
        if r.status_code != 200:
            text = _body_text(r)
            if r.status_code == 401:
                err = "会话失效（HTTP 401）—— 重新走登录"
            elif r.status_code == 403:
                err = ("API 访问被停用（HTTP 403）—— 站点侧风控/封禁，"
                       "不是代码问题")
            else:
                err = f"建 key HTTP {r.status_code}: {text}"
            return Result(ok=False, stage="create_api_key",
                          status=r.status_code, error=err, data=body)
        if not body.get("key"):
            return Result(ok=False, stage="create_api_key", status=200,
                          error=f"建 key 响应里没有明文 key: {str(body)[:200]}")
        return Result(ok=True, stage="create_api_key", status=200, data=body)
