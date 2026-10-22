"""自测共享夹具：`check()` 计数器 + 离线替身。

⚠️ 刻意**不使用** `conftest.py` 这个名字 —— 那是 pytest 的保留名，
本项目零第三方依赖、跑法仍是 `python tools/selftest.py`。

离线替身只实现 `runner` / `stages` 真正用到的方法，不碰网络：
  · `_FakeMail` / `_FakeTempMailClient` —— 替换邮箱客户端
  · `_FakeSite` / `_FakeTokenJuice` —— 替换 `src.stages.TokenJuiceClient`
  · `fake_verify_key` —— 替换 `src.stages.verify_key`

⚠️ 替身字段要与真实类型一致：多出真实类型没有的字段会掩盖
「某个计数器其实没人读」这类问题。
"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。



from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # tools/
from _bootstrap import ROOT  # noqa: F401  （副作用：把仓库根加进 sys.path）

from src import runner as pl
from src import stages as st
from src.tempemail import Mail
from src.tokenjuice import Result

#: 计数放在可变容器里，跨模块共享（不用 `global`）。
_COUNTS = {"pass": 0, "fail": 0}


def counts() -> tuple[int, int]:
    return _COUNTS["pass"], _COUNTS["fail"]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _COUNTS["pass"] += 1
        print(f"  ✓ {name}")
    else:
        _COUNTS["fail"] += 1
        print(f"  ✗ {name}  {detail}")


def mail(to: str, subject: str = "Your Token Juice sign-in link",
         body: str = "", *, sender: str = "0101@us-west-2.amazonses.com",
         #: 刻意取一个极大的时间戳：`stage_login` 用 `since_ms = now - 5s` 跳过
         #: 历史邮件，写死一个真实日期会让测试在「真实时间超过它」之后开始失败
         #: （表现是「收不到邮件」—— 看起来像代码坏了，其实是夹具过期）。
         received_at: int = 9_999_999_999_999) -> Mail:
    return Mail(id="1", to=to, sender=sender, subject=subject,
                body=body, received_at=received_at)


# ══════════════════════════════════════════════════════════════════════
# 邮箱替身
# ══════════════════════════════════════════════════════════════════════
class _FakeTempMailClient:
    """替换 `src.runner.make_mail_client` 造出来的客户端。

    `pending` 是**类属性**：所有实例共享同一批待收邮件，模拟「同一批邮箱窗口」。
    收件人匹配 `email` 或通配 `"*"`。
    """

    pending: ClassVar[list] = []
    created = 0

    def __init__(self, *a, **kw):
        self.stats = SimpleNamespace(polls=0, http_5xx=0)
        self.last_interval = None
        self.created_emails: list[str] = []

    def create_mailbox(self, domain: str | None = None) -> str:
        _FakeTempMailClient.created += 1
        e = f"fake-{_FakeTempMailClient.created}@example-mail.test"
        self.created_emails.append(e)
        return e

    def wait_for_mail(self, email, match, *, timeout=0, interval=0,
                      since_ms=None):
        self.last_interval = interval
        self.stats.polls += 1
        for m in _FakeTempMailClient.pending:
            if m.to not in (email, "*"):
                continue
            if since_ms is not None and m.received_at < since_ms:
                continue
            if match(m):
                _FakeTempMailClient.pending.remove(m)
                return m
        return None

    def list_mails(self, email=None, limit=None):
        return [m for m in _FakeTempMailClient.pending if m.to in (email, "*")]

    def health(self):
        return {"storage": "fake", "database": "fake", "domains": ["example-mail.test"]}

    def scan_all(self, limit: int = 100):
        return list(_FakeTempMailClient.pending)


# ══════════════════════════════════════════════════════════════════════
# 站点替身
# ══════════════════════════════════════════════════════════════════════
class _FakeSite:
    """所有实例共享一个「服务端状态」，模拟同一个账号在多次运行间的持久状态。"""

    #: email -> 已存在的 key 元数据（None = 没有 key）
    existing: ClassVar[dict[str, dict]] = {}
    #: 每次 create 生成的 key（按顺序）
    created: ClassVar[list[str]] = []
    #: 强制让 verify 失败（模拟链接被用过/过期）
    fail_verify = False
    #: 强制让 send 失败
    fail_send = False
    #: 强制让验活失败
    fail_apicheck = False
    #: 记录每次调用的邮箱，用于并发串号检查
    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, *a, **kw):
        self.email = ""
        self.log: list[str] = []

    def reset(self) -> None:
        """真实客户端会清 Cookie；替身无状态，no-op。"""

    def send_magic_link(self, email: str) -> Result:
        _FakeSite.calls.append(("send", email))
        if _FakeSite.fail_send:
            return Result(ok=False, stage="send_magic_link", status=400,
                          error="发信 HTTP 400: Invalid email address")
        self.email = email
        return Result(ok=True, stage="send_magic_link", status=204)

    def verify(self, token: str, *, retain_data: bool = True) -> Result:
        _FakeSite.calls.append(("verify", self.email))
        if _FakeSite.fail_verify or not token:
            return Result(ok=False, stage="verify", status=400,
                          error="token 已失效（一次性 + 15 分钟）—— 需重新发信")
        return Result(ok=True, stage="verify", status=204)

    def me(self):
        return {"email": self.email} if self.email else None

    def get_api_key(self) -> Result:
        _FakeSite.calls.append(("get_key", self.email))
        meta = _FakeSite.existing.get(self.email)
        if meta:
            return Result(ok=True, stage="get_api_key", status=200,
                          data={**meta, "exists": True})
        return Result(ok=True, stage="get_api_key", status=404,
                      data={"exists": False})

    def create_api_key(self) -> Result:
        _FakeSite.calls.append(("create_key", self.email))
        n = len(_FakeSite.created) + 1
        key = f"tj_fake{n:04d}"
        _FakeSite.created.append(key)
        _FakeSite.existing[self.email] = {"suffix": key[-4:],
                                          "createdAt": "2026-10-22T00:00:00Z"}
        return Result(ok=True, stage="create_api_key", status=200,
                      data={"key": key, "suffix": key[-4:],
                            "createdAt": "2026-10-22T00:00:00Z"})

    def signout(self):
        return Result(ok=True, stage="signout", status=204)


def fake_verify_key(key: str, **_kw):
    if _FakeSite.fail_apicheck:
        return {"ok": False, "authenticated": False, "degraded": False,
                "status": 403, "chat_status": 0, "error": "Forbidden",
                "elapsed": 0.0, "attempts": 1}
    return {"ok": True, "authenticated": True, "degraded": False,
            "status": 200, "chat_status": 200, "model": "fake-model",
            "reply": "pong", "usage": {"total_tokens": 8},
            "elapsed": 0.01, "attempts": 1}


@contextlib.contextmanager
def offline(*, reset: bool = True):
    """把网络接缝换成离线替身。退出时**恢复原对象**（自测之间互不污染）。"""
    if reset:
        _FakeTempMailClient.pending = []
        _FakeTempMailClient.created = 0
        _FakeSite.existing = {}
        _FakeSite.created = []
        _FakeSite.fail_verify = False
        _FakeSite.fail_send = False
        _FakeSite.fail_apicheck = False
        _FakeSite.calls = []

    saved = (st.TokenJuiceClient, st.verify_key, pl.make_mail_client)
    st.TokenJuiceClient = _FakeSite
    st.verify_key = fake_verify_key
    pl.make_mail_client = lambda backend="cf": _FakeTempMailClient()
    try:
        yield
    finally:
        st.TokenJuiceClient, st.verify_key, pl.make_mail_client = saved


__all__ = [
    "Mail", "Result", "_FakeSite", "_FakeTempMailClient", "check",
    "counts", "fake_verify_key", "mail", "offline", "pl", "st",
]
