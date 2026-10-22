"""端到端编排：发链接 → 验证激活 → 创建 API Key → 验活 → 入库。

阶段与可自动化程度（实测结论，不是推断）：

    1. signup   建临时邮箱 + POST /api/v1/send-magic-link            ✅ 全自动
    2. login    收邮件 → 取 token → POST /api/v1/auth/verify         ✅ 全自动
    3. api_key  GET /api/v1/api-key（查重）→ POST /api/v1/api-key     ✅ 全自动
    4. verify   GET /v1/models + POST /v1/chat/completions（网关）    ✅ 全自动
    5. store    exports/ledger.jsonl（全部尝试）+ result/（可用 key） ✅ 全自动

🔴 与参考项目最大的结构性差异：**建 key 会轮换**。

    TypeSafe：重跑建出第二把 key，两把在服务端**都有效**，只是台账按邮箱
              去重会吃掉一把 ⇒ 交付物少一行（不报错）。
    Token Juice：重跑建出新 key 会让**旧 key 立刻失效**，而 `GET /api-key`
              只回 suffix、拿不回明文 ⇒ 重跑可能**销毁**已交付的凭据。

⇒ 本项目的 `resume(skip_keyed=True)` 不只是「省一次请求」，它是**数据完整性护栏**；
  `stages.stage_create_key` 里还有第二道（查到已有 key 且无明文 ⇒ 默认不建）。

两份台账：

    exports/ledger.jsonl    运行台账：**全部尝试**（含失败的），用于复盘
    result/success.jsonl    成功数据：**只记拿到并验活 key 的**，是交付物
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import config
from .ledger import Ledger

# 🔴 `AccountRecord` 定义在 `stages`（不在本模块）：依赖必须单向 runner → stages。
#    这里导入是为了让 `from src.runner import AccountRecord` 继续可用。
from .stages import MAIL_TIMEOUT, AccountRecord, StageMixin
from .tempemail import TempMailClient, TempMailError

#: 并发时多线程会同时 print，不加锁会在一行中间交错，日志直接没法读。
_LOG_LOCK = threading.Lock()

#: 邮箱后端名。当前只有 CF Worker 一个 —— 保留常量是为了让 CLI 的
#: `--mail-backend` 有**唯一真源**，将来加后端时不会漏改某个入口。
MAIL_BACKENDS = ("cf",)
DEFAULT_MAIL_BACKEND = "cf"


def make_mail_client(backend: str = DEFAULT_MAIL_BACKEND) -> Any:
    """按名字造一个邮箱客户端。

    ⚠️ 未知名字**抛 ValueError 而不是回落到默认值**：静默回落会让
    `--mail-backend cff` 这种拼写错误变成「跑了一批默认邮箱」，
    而且因为不报错，要等很久以后对账时才发现。
    """
    name = (backend or DEFAULT_MAIL_BACKEND).strip().lower()
    if name == "cf":
        return TempMailClient()
    raise ValueError(f"未知邮箱后端 {backend!r}（可选：{'、'.join(MAIL_BACKENDS)}）")


class Pipeline(StageMixin):
    def __init__(self, *, mail: Any | None = None,
                 mail_factory: Callable[[], Any] | None = None,
                 backend: str = DEFAULT_MAIL_BACKEND,
                 ledger: Ledger | None = None,
                 success_ledger: Ledger | None = None,
                 domain: str | None = None, verbose: bool = True,
                 rotate_key: bool = False, confirm_session: bool = True):
        self.backend = (backend or DEFAULT_MAIL_BACKEND).strip().lower()
        self._mail_factory: Callable[[], Any] = (
            mail_factory or (lambda: make_mail_client(self.backend)))
        # ⚠️ 判断写成 `is not None` 而不是 `mail or ...`：显式传进来的客户端
        #    必须被采纳。用 `or` 的话，将来任何客户端定义了 `__bool__`/`__len__`
        #    就会被**静默换成默认后端**。
        self.mail = mail if mail is not None else self._mail_factory()
        self.ledger = ledger or Ledger(config.LEDGER_PATH)
        self.success_ledger = (success_ledger if success_ledger is not None
                               else Ledger(config.SUCCESS_LEDGER_PATH))
        self.domain = domain or config.TEMPMAIL_DOMAIN
        self.verbose = verbose
        #: 允许轮换已有 key（会**作废旧 key**）。默认 False —— 见模块头部的 🔴。
        self.rotate_key = rotate_key
        #: 登录后是否再打一次 `/auth/me` 确认会话。默认 True（安全）；
        #: `False` 省 ~0.24s/号，会话有效性由随后的 `GET /api-key`（401）兜住。
        self.confirm_session = confirm_session
        #: 每个 worker 复用的站点连接（省 TLS 握手）。**只在 worker 内串行使用**。
        self._site_session: Any = None

    def log(self, msg: str) -> None:
        if self.verbose:
            with _LOG_LOCK:
                print(msg, flush=True)

    # ── 并发脚手架 ────────────────────────────────────────────────────
    def _clone(self) -> Pipeline:
        """给一个并发 worker 用的**独立**实例。

        独立是硬要求：邮箱客户端持有 `requests.Session`（不保证线程安全），
        `stats` 计数器也会被多线程搅乱。唯一共享的是 `Ledger`（读写都有锁）。

        🔴 **必须显式传 `mail`**：只传 ledger/domain 的话，每个 worker 都会新建
        客户端；串行永远复现不出来，只在并发时暴露成「一半账号用了另一个后端」。
        """
        return Pipeline(mail=self._mail_factory(), backend=self.backend,
                        ledger=self.ledger, success_ledger=self.success_ledger,
                        domain=self.domain, verbose=self.verbose,
                        rotate_key=self.rotate_key,
                        confirm_session=self.confirm_session)

    def _fan_out(
        self, jobs: list[tuple[Any, Callable[[Pipeline, Any], AccountRecord]]],
        *, concurrency: int,
    ) -> list[AccountRecord]:
        """按并发度跑一批任务，返回顺序与传入一致。

        `concurrency <= 1` 时走**串行路径**，且刻意不吞异常（fail-fast）。

        🔴 **每个 worker 一个 clone，而不是每个任务一个**（2026-10-23 实测优化）。
        旧实现是 `pool.submit(fn, self._clone(), key)` —— 提交 N 个任务就造 N 个
        Pipeline ⇒ N 个 `requests.Session`（站点 + Worker 各一套）⇒ 每个账号都重付
        TLS 握手（实测冷连接 ~660ms vs 热连接 ~223ms）。而且它**静默**：
        串行路径（concurrency=1）完全不走这里，所以「会话复用没生效」在单号测试里
        永远看不出来。现在按 `pos % concurrency` 分片，每个 worker 顺序消费自己的
        分片，连接池/站点会话/邮箱客户端都按 worker 复用。

        代价：分片是静态的，单个慢账号会拖住它所在分片的下一个任务（旧实现是
        动态抢占）。本链路单号方差很小（p90/median ≈ 1.1），静态分片的收益
        （每号省 2 次 TLS 握手）远大于尾部延迟损失。
        """
        concurrency = max(1, int(concurrency))
        if concurrency <= 1:
            return [fn(self, key) for key, fn in jobs]

        # 按轮转分片，尽量让每个 worker 的任务数均衡
        buckets: list[list[tuple[int, Any, Callable[..., AccountRecord]]]] = [
            [] for _ in range(concurrency)]
        for pos, (key, fn) in enumerate(jobs):
            buckets[pos % concurrency].append((pos, key, fn))

        out: list[AccountRecord | None] = [None] * len(jobs)

        def work(pipe: Pipeline, bucket: list[tuple[int, Any, Callable[..., AccountRecord]]]) -> None:
            for pos, key, fn in bucket:
                try:
                    out[pos] = fn(pipe, key)
                except Exception as exc:  # noqa: BLE001
                    # 单个任务炸了不该带走整批，但**必须留下一条台账记录**。
                    rec = self._worker_crash_record(key, exc)
                    pipe.ledger.append(rec.to_dict())
                    out[pos] = rec
                    self.log(f"✗ 并发任务异常（已记入台账，不再静默丢弃）: "
                             f"{type(exc).__name__}: {exc}")

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(work, self._clone(), b) for b in buckets if b]
            for fut in as_completed(futs):
                fut.result()          # 异常已在 work() 内兜住；这里只等完成
        return [r for r in out if r is not None]

    @staticmethod
    def _worker_crash_record(key: Any, exc: BaseException) -> AccountRecord:
        """并发 worker 崩溃时补一条台账记录 —— 保证**提交数 == 结果数**。

        ⚠️ `key` 是批次序号而不是邮箱：邮箱是在崩溃的 worker **内部**才建出来的，
        崩了就无从得知。键写成 `worker-crash#<n>`，明确不是邮箱 ——
        `resume` 的调用方应显式跳过它（本项目的 `run_e2e.py` 会跳过）。
        """
        rec = AccountRecord(key=f"worker-crash#{key}", email="")
        rec.status = "failed"
        rec.error = f"worker 崩溃: {type(exc).__name__}: {exc}"
        rec.stages["worker"] = "failed"
        return rec

    # ── 全链路 ────────────────────────────────────────────────────────
    def run_one(self, *, email: str = "", mail_timeout: float = MAIL_TIMEOUT,
                rotate: bool | None = None) -> AccountRecord:
        """跑一个账号：发链接 → 换会话 → 建 key → 验活。

        `email` 为空时由 `stage_login` 现场建一个临时邮箱（默认用法）。

        🔴 记录 `timings["total"]` = **单号端到端**耗时（进函数 → 出函数）。
        必须实测而不是用 `login + create_key` 推导：`create_mailbox` 抛异常时
        根本走不到那两处 ⇒ 推导值在那些记录上直接缺失 ⇒ 统计平均速率时
        会把最慢的账号静默排除（分母变小、速率虚高），且不报错。
        """
        t_all = time.time()
        rec = AccountRecord(key=email, email=email)
        try:
            cl = self.stage_login(rec, mail_timeout=mail_timeout)
            if cl is None:
                return rec
            self.stage_create_key(rec, cl, rotate=rotate)
        except TempMailError as exc:
            rec.status = "failed"
            rec.error = f"邮箱服务异常: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 兜底，保证台账一定写得进去
            rec.status = "failed"
            rec.error = f"{type(exc).__name__}: {exc}"
        finally:
            # 放在 `finally`：失败路径同样要留痕。
            rec.timings["total"] = time.time() - t_all
        return rec

    def run_batch(self, *, count: int = 1, concurrency: int = 1,
                  mail_timeout: float = MAIL_TIMEOUT,
                  rotate: bool | None = None) -> list[AccountRecord]:
        """建 `count` 个新邮箱，各跑一遍全链路。"""

        def job(pipe: Pipeline, i: int) -> AccountRecord:
            pipe.log(f"[{i + 1}/{count}] 开始")
            rec = pipe.run_one(mail_timeout=mail_timeout, rotate=rotate)
            pipe.ledger.append(rec.to_dict())
            pipe.log(f"[{i + 1}/{count}] status={rec.status} {rec.error}")
            return rec

        return self._fan_out([(i, job) for i in range(count)],
                             concurrency=concurrency)

    def resume(self, emails: list[str], *, concurrency: int = 1,
               mail_timeout: float = MAIL_TIMEOUT,
               skip_keyed: bool = True,
               rotate: bool | None = None) -> list[AccountRecord]:
        """对**已知邮箱**跑全链路（不新建邮箱）。

        与 `run_batch` 的唯一区别是邮箱来源：同一个地址可以**反复重跑** ——
        邮件会丢包、链接 15 分钟过期，重跑往往比改请求更有效。

        🔴 `skip_keyed=True`（默认）跳过台账里**已有 api_key** 的地址。
        这不是优化，是数据完整性：Token Juice 建新 key 会让**旧 key 失效**
        （实测轮换后旧 key 变 401），而明文只在创建时出现一次。
        ⇒ 重跑一个已交付的账号 = 销毁一把还能用的凭据。
        要**故意**轮换时显式传 `skip_keyed=False` + `rotate=True`。
        """
        have_key = ({r["email"] for r in self.ledger.load() if r.get("api_key")}
                    if skip_keyed else set())
        todo: list[str] = []
        for e in emails:
            e = (e or "").strip()
            if not e or e.startswith("worker-crash#"):
                continue
            if e in have_key:
                self.log(f"[resume] 跳过 {e}（台账里已有 api_key；"
                         f"重跑会轮换并**作废**旧 key；确需轮换请传 "
                         f"--rotate-key --no-skip-keyed）")
                continue
            todo.append(e)

        def job(pipe: Pipeline, email: str) -> AccountRecord:
            t_all = time.time()
            rec = AccountRecord(key=email, email=email)
            try:
                pipe.log(f"[resume] {email}")
                cl = pipe.stage_login(rec, mail_timeout=mail_timeout)
                if cl is not None:
                    pipe.stage_create_key(rec, cl, rotate=rotate)
            finally:
                # 与 `run_one` 同口径：单号端到端耗时，**失败也留痕**。
                rec.timings["total"] = time.time() - t_all
            pipe.ledger.append(rec.to_dict())
            pipe.log(f"[resume] status={rec.status} {rec.error}")
            return rec

        return self._fan_out([(e, job) for e in todo], concurrency=concurrency)
