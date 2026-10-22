"""编排层测试：全链路 / 轮换护栏 / 重跑去重 / 并发 / 验活判据。

全部离线：`tests.support.offline()` 把三个网络接缝换掉
（`stages.TokenJuiceClient` / `stages.verify_key` / `runner.make_mail_client`）。
"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from tests.support import _FakeSite, _FakeTempMailClient, check, mail, offline

from src import runner as pl
from src import stages as st
from src.apicheck import verify_key
from src.ledger import Ledger

BODY = ("Sign in to Token Juice\n"
        "https://tokenjuice.ai/auth/verify#token=VkAw1y-oZ13zNgDsQtp02N1ae_"
        "UpsqTScKSdJEDfGkI\n")


def _pipe(td: str, **kw):
    return pl.Pipeline(ledger=Ledger(Path(td) / "ledger.jsonl"),
                       success_ledger=Ledger(Path(td) / "success.jsonl"),
                       verbose=False, **kw)


def test_full_chain() -> None:
    print("\n[orchestration] 全链路：发链接 → 换会话 → 建 key → 验活 → 入库")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeTempMailClient.pending = [mail("fake-1@example-mail.test", body=BODY)]
        pipe = _pipe(td)
        rec = pipe.run_one()

        check("状态 keyed", rec.status == "keyed", f"{rec.status} {rec.error}")
        check("拿到明文 key", rec.api_key.startswith("tj_fake"), rec.api_key)
        check("记录 suffix", rec.api_key_suffix == "0001", rec.api_key_suffix)
        check("阶段齐全", rec.stages == {"mailbox": "ok", "send": "ok", "mail": "ok",
                                        "verify": "ok", "me": "ok", "api_key": "ok"},
              str(rec.stages))
        check("验活结果已留痕",
              rec.user.get("verify", {}).get("authenticated") is True,
              str(rec.user.get("verify")))
        check("登录段留了邮件元数据",
              rec.login.get("mail_subject") == "Your Token Juice sign-in link")
        check("timings 有 total（失败也要留痕的同口径）", "total" in rec.timings)
        check("成功台账写入了这条", len(pipe.success_ledger.load()) == 1)
        check("运行台账没被 stage 层代写（由调用方负责）",
              pipe.ledger.load() == [])


def test_full_chain_without_confirm_session() -> None:
    """`--no-confirm-session` 路径：跳过 /auth/me，但流程仍必须走通。

    这是 2026-10-23 实测抓到的回归：跳过 me 后，末尾日志仍引用局部变量 `me`
    ⇒ 20/20 全部 `UnboundLocalError`，而默认路径（confirm_session=True）完全正常。
    自测必须覆盖**这个开关的两侧**，否则「省 0.24s」的优化会以整批失败为代价。
    """
    print("\n[orchestration] 跳过 /auth/me 的快速路径")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeTempMailClient.pending = [mail("fake-1@example-mail.test", body=BODY)]
        rec = _pipe(td, confirm_session=False).run_one()
        check("状态 keyed", rec.status == "keyed", f"{rec.status} {rec.error}")
        check("me 阶段记为 skipped", rec.stages.get("me") == "skipped",
              str(rec.stages))
        check("me 计时为 0", rec.timings.get("me") == 0.0, str(rec.timings))
        check("me_email 仍有值（用已知邮箱）",
              rec.login.get("me_email") == rec.email, str(rec.login))


def test_send_failure() -> None:
    print("\n[orchestration] 发信失败的分流")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeSite.fail_send = True
        rec = _pipe(td).run_one()
        check("状态 failed", rec.status == "failed", rec.status)
        check("错误文案点出邮箱格式（可执行）",
              "Invalid email address" in rec.error, rec.error)
        check("不发无谓的重试", rec.stages.get("send") == "failed", str(rec.stages))


def test_verify_failure() -> None:
    print("\n[orchestration] 链接失效（一次性/15 分钟）")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeSite.fail_verify = True
        _FakeTempMailClient.pending = [mail("fake-1@example-mail.test", body=BODY)]
        rec = _pipe(td).run_one()
        check("状态 failed", rec.status == "failed", rec.status)
        check("错误文案指向「需重新发信」", "重新发信" in rec.error, rec.error)
        check("不写成功台账", _pipe(td).success_ledger.load() == [])


def test_timeout_resends_once() -> None:
    print("\n[orchestration] 等信超时 → 批内重发一次")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeTempMailClient.pending = []          # 永远收不到
        rec = _pipe(td).run_one()
        sends = [c for c in _FakeSite.calls if c[0] == "send"]
        check("状态 failed", rec.status == "failed", rec.status)
        check("重发了 1 次（共 2 次 send）", len(sends) == 2, str(sends))
        check("超时文案区分「读不出来」与「没发」",
              "5xx" in rec.error and "轮询" in rec.error, rec.error)
        check("重发次数记进台账", rec.login.get("send_retries") == 1, str(rec.login))


def test_rotation_guard() -> None:
    print("\n[orchestration] 轮换护栏：已有 key 且无明文 ⇒ 默认不重建")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeSite.existing["a@b.test"] = {"suffix": "old1",
                                          "createdAt": "2026-10-01T00:00:00Z"}
        _FakeTempMailClient.pending = [mail("a@b.test", body=BODY)]
        rec = _pipe(td).resume(["a@b.test"])[0]
        check("状态 partial（不是 failed，也不是 keyed）",
              rec.status == "partial", f"{rec.status} {rec.error}")
        check("**没有**调用建 key（否则旧 key 会被作废）",
              not any(c[0] == "create_key" for c in _FakeSite.calls),
              str(_FakeSite.calls))
        check("错误文案给出两条可执行出路",
              "rotate-key" in rec.error and "明文" in rec.error, rec.error)
        check("suffix 已记入台账", rec.api_key_suffix == "old1")

    with tempfile.TemporaryDirectory() as td, offline():
        _FakeSite.existing["a@b.test"] = {"suffix": "old1",
                                          "createdAt": "2026-10-01T00:00:00Z"}
        _FakeTempMailClient.pending = [mail("a@b.test", body=BODY)]
        rec = _pipe(td, rotate_key=True).resume(["a@b.test"])[0]
        check("显式 rotate_key=True 才轮换", rec.status == "keyed", rec.status)
        check("轮换确实调了建 key",
              any(c[0] == "create_key" for c in _FakeSite.calls))


def test_resume_skips_keyed() -> None:
    print("\n[orchestration] resume 默认跳过已交付账号")
    with tempfile.TemporaryDirectory() as td, offline():
        pipe = _pipe(td)
        pipe.ledger.append({"key": "a@b.test", "email": "a@b.test",
                            "status": "keyed", "api_key": "tj_live"})
        recs = pipe.resume(["a@b.test"])
        check("返回空（什么都没跑）", recs == [], str(recs))
        check("一次网络调用都没有", _FakeSite.calls == [], str(_FakeSite.calls))
        recs2 = pipe.resume(["a@b.test"], skip_keyed=False)
        check("显式 skip_keyed=False 才重跑", len(recs2) == 1)


def test_apicheck_failure_is_partial() -> None:
    print("\n[orchestration] 网关拒绝 ⇒ 不能算成功，但明文要留档")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeSite.fail_apicheck = True
        _FakeTempMailClient.pending = [mail("fake-1@example-mail.test", body=BODY)]
        rec = _pipe(td).run_one()
        check("状态 partial", rec.status == "partial", rec.status)
        check("明文 key 仍在记录里（EARNED_FIELDS 兜底）",
              rec.api_key.startswith("tj_fake"), rec.api_key)
        check("不写成功台账（交付物只放可用的）",
              _pipe(td).success_ledger.load() == [])


def test_concurrency_no_crosstalk() -> None:
    print("\n[orchestration] 并发不串号")
    with tempfile.TemporaryDirectory() as td, offline():
        _FakeTempMailClient.pending = [
            mail("*", body=BODY), mail("*", body=BODY),
        ]
        recs = _pipe(td).run_batch(count=2, concurrency=2)
        check("两个账号都成功", all(r.status == "keyed" for r in recs),
              str([(r.status, r.error) for r in recs]))
        keys = {r.api_key for r in recs}
        check("两把 key 互不相同", len(keys) == 2, str(keys))
        check("邮箱互不相同", len({r.email for r in recs}) == 2,
              str([r.email for r in recs]))
        check("每把 key 都属于各自的邮箱",
              all(_FakeSite.existing.get(r.email) for r in recs))
        check("台账两条", len(_pipe(td).ledger.load()) == 2)


def test_worker_crash_is_recorded() -> None:
    print("\n[orchestration] 并发 worker 崩溃必须补台账")
    with tempfile.TemporaryDirectory() as td, offline():
        pipe = _pipe(td)

        def boom(_pipe, _key):
            raise RuntimeError("模拟崩溃")

        recs = pipe._fan_out([(0, boom)], concurrency=2)
        check("返回一条记录（提交数 == 结果数）", len(recs) == 1, str(recs))
        check("记录标明是 worker 崩溃",
              recs[0].key == "worker-crash#0" and recs[0].status == "failed",
              f"{recs[0].key} {recs[0].status}")
        check("台账里也有一条", len(pipe.ledger.load()) == 1)


# ── 验活判据（纯函数，用假 session）────────────────────────────────────
class _Resp:
    def __init__(self, status, body="", payload=None):
        self.status_code = status
        self.text = body
        self._payload = payload or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Sess:
    def __init__(self, gets, posts=None):
        self.headers = {}
        self._gets = list(gets)
        self._posts = list(posts or [])

    def get(self, *a, **kw):
        item = self._gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, *a, **kw):
        item = self._posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_apicheck_semantics() -> None:
    print("\n[apicheck] 验活判据（chat 优先）与网络/5xx 重试")
    import requests

    models_ok = _Resp(200, payload={"data": [{"id": "m"}]})
    chat_ok = _Resp(200, payload={"choices": [{"message": {"content": "pong"}}],
                                  "usage": {"total_tokens": 8}})

    # chat 200：最强证据，且**不应再打 models**（gets 队列为空，打了就会 IndexError）
    s: Any = _Sess([], [chat_ok])
    v = verify_key("tj_x", retries=2, session=s)
    check("chat 200 ⇒ ok/authenticated", v["ok"] and v["authenticated"], str(v))
    check("chat 200 时跳过 models（省一次 RTT）", v["models"] == [], str(v["models"]))

    # chat 网络抖动：重试后成功
    s = _Sess([], [requests.ConnectionError("ssl eof"), chat_ok])
    v = verify_key("tj_x", retries=2, session=s)
    check("chat 网络失败会重试（第 2 次成功）",
          v["ok"] and v["attempts"] == 2, str(v))

    # chat 401：确定性失败，不打 models
    s = _Sess([], [_Resp(401, "Unauthorized")])
    v = verify_key("tj_x", retries=2, session=s)
    check("chat 401 是确定性失败，不重试也不打 models",
          not v["authenticated"] and v["status"] == 401 and v["attempts"] == 1,
          str(v))

    # chat 429 + models 200：key 有效，容量问题
    s = _Sess([models_ok], [_Resp(429, "rate limited")])
    v = verify_key("tj_x", session=s)
    check("chat 429 + models 200 ⇒ 已认证（交付判据）", v["authenticated"] is True)
    check("chat 429 = 降级，不是 key 失效",
          v["degraded"] is True and v["ok"] is False, str(v))

    # chat 400（请求体/模型名问题）+ models 200：key 仍然有效
    s = _Sess([models_ok], [_Resp(400, "bad model")])
    v = verify_key("tj_x", session=s)
    check("chat 400 不等于 key 失效（models 200 兜住）",
          v["authenticated"] is True and v["chat_status"] == 400, str(v))

    # chat 403：确定性失败
    s = _Sess([], [_Resp(403, "Forbidden")])
    v = verify_key("tj_x", session=s)
    check("chat 403 是确定性失败",
          v["ok"] is False and v["degraded"] is False and v["authenticated"] is False,
          str(v))

    # 两层都网络失败：不确定，而不是「key 失效」
    s = _Sess([requests.ConnectionError("dns")] * 3,
              [requests.ConnectionError("dns")] * 3)
    v = verify_key("tj_x", retries=2, session=s)
    check("两层都网络失败 ⇒ inconclusive + status=0",
          v["inconclusive"] is True and v["status"] == 0, str(v))

    # 🔴 实测踩过的坑：models 偶发 500，旧实现直接判「不可用」⇒ 可用 key 从交付物消失
    s = _Sess([_Resp(500, "Internal Server Error"), models_ok],
              [_Resp(429, "rate limited")])
    v = verify_key("tj_x", retries=2, session=s)
    check("models 500 会重试（第 2 次 200 ⇒ 已认证）",
          v["authenticated"] and v["attempts"] == 2, str(v))

    s = _Sess([_Resp(503, "unavailable")] * 3, [_Resp(429, "rate limited")])
    v = verify_key("tj_x", retries=2, session=s)
    check("models 持续 5xx ⇒ inconclusive，不是「key 失效」",
          v["inconclusive"] is True and v["status"] == 503, str(v))


def test_chain_uses_single_source_for_verify() -> None:
    """生产阶段与验收工具必须共用 `apicheck.verify_key`（不许各写一份）。"""
    print("\n[orchestration] 验活只有一份实现")
    src = Path(st.__file__).read_text(encoding="utf-8")
    check("stages.py 用的是 apicheck.verify_key",
          "from .apicheck import verify_key" in src)
    tool = (Path(pl.__file__).parents[1] / "tools" / "verify_keys.py").read_text(
        encoding="utf-8")
    check("verify_keys.py 也 import 同一符号",
          "from src.apicheck import verify_key" in tool)
