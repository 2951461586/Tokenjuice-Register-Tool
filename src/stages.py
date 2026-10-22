"""阶段层：单个账号从「发链接」到「拿到并验活 Key」的全部出网动作。

与 `runner.py` 的分工：

    runner.py   调度：批量 / 并发 / 台账写入
    stages.py   单账号：send-link → verify → api-key → 验活

链路（与参考项目 TypeSafe 的对照）：

    TypeSafe（参考）                      Token Juice（本项目）
    ─────────────────────────────────    ──────────────────────────────────
    建临时邮箱                            建临时邮箱
    GET /login 抓 Server Action            ——（没有 Server Action，纯 JSON API）
    POST /login（表单）                    POST /api/v1/send-magic-link {"email"}
    收「确认邮件」                         收「Your Token Juice sign-in link」
    魔法链接 → Stytch dfp → callback       **直接取 token** → POST /api/v1/auth/verify
    onboarding（ToS / 改名）               ——（无 onboarding）
    POST /api/api-keys                     POST /api/v1/api-key
    ——（另跑 verify_keys）                 **建完当场验活**（GET /v1/models + chat）
    写 success.jsonl                       写 success.jsonl

🔴 本链路独有的三个约束（都是实测，不是推断）：

1. **token 一次性 + 15 分钟**（邮件正文原话）。所以「等不到信」的处置是
   **重发**，不是「等久一点」；重发不会让旧链接失效（实测：两次发信后用第一封
   的 token 仍能换到会话），所以批内重发是安全的。
2. **会话是 `session` Cookie（HttpOnly，30 天）**，没有 Bearer token。
   `requests.Session` 负责保存；任何「手工拼 header」的写法都拿不到会话。
3. **建 key 会轮换**：已有 key 的账号再 POST 一次会 200 + 新 key，**旧 key 立刻失效**
   （实测：轮换后第一把在网关变 401）。而 `GET /api/v1/api-key` 只回 `suffix`，
   **拿不回明文**。⇒ 重跑必须先查再决定，见 `stage_create_key` 的护栏。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .apicheck import verify_key
from .mailrules import LINK_RULES, any_of
from .parsing import extract_magic_tokens

# ⚠️ `TokenJuiceClient` 必须是**模块级名字**：`tools/tests/support.py` 会替换
#    `src.stages.TokenJuiceClient` 做离线测试。写成 `tokenjuice.TokenJuiceClient()`
#    会让 patch 静默落空（自测全线失败，而生产代码看起来毫无问题）。
from .tokenjuice import Result, TokenJuiceClient, TokenJuiceError

MATCH_LINK = any_of(*LINK_RULES)

#: 等登录邮件的秒数。
#:
#: 实测延迟（2026-10-22，3 次独立发信）：**1~3 秒**必到（SES → Worker）。
#: 90s 对实测分布有 ~30 倍余量，同时把「丢包」的判定成本压在可接受范围。
#: ⚠️ 不要照抄参考项目的 60s 或 300s —— 那是别的站点的分布，与本链路无关。
MAIL_TIMEOUT = 90.0

#: 等信轮询间隔。打的是**我们自己的** Worker（索引端点，读 0~2 行），
#: 不碰站点，所以 0.5s 的请求量是安全的。
MAIL_POLL_INTERVAL = 0.5

#: 邮件里明写的链接有效期（秒）。只用于诊断文案 —— 不做本地过期判断：
#: 判据永远是「服务端接不接受」，本地时钟不可信。
LINK_TTL_SECONDS = 15 * 60

#: 超时后是否在**同一批次内**重发一次再等一轮。
#: 依据：正常 1~3s 必到 ⇒ 90s 还没有几乎必然是丢包；重发的成本是一次
#: `POST send-magic-link`，收益是把成功率从「丢包率」拉回接近 100%。
RETRY_SEND_ON_TIMEOUT = True
#: 重发次数上限。只给 1 次：站点对「短时间重复发信」的容忍度未知，
#: 且重发不会作废旧链接，账号落到下一轮 `resume` 仍然有效。
MAX_SEND_RETRIES = 1

# 本模块写入的 `status` 字面量（started / registered / keyed / partial / failed）
# 必须在 `ledger.RANK` 里有登记 —— 自测 `test_status_vocabulary` 会用 AST 扫本文件。


@dataclass
class AccountRecord:
    key: str
    email: str
    status: str = "started"
    error: str = ""
    created_at: float = field(default_factory=time.time)
    stages: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    api_key: str = ""
    api_key_suffix: str = ""
    api_key_created_at: str = ""
    #: 登录段的元数据（收到的邮件主题 / 到达时间 / 候选 token 数）。
    login: dict[str, Any] = field(default_factory=dict)
    #: 站点侧快照（`/auth/me` 的 email、验活结果）。
    user: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把 `key` / `email` 的首尾空白去掉 —— 台账的键就是它。

        邮箱常从 CLI/清单文件批量传入，Windows 上很容易带 `\\r`。站点侧会 trim
        掉它、照常发信（**不报错**），但台账会把同一账号写成两条 ⇒
        交付数字虚高、「按邮箱去重」失效。属于不报错的静默数据损坏，
        只能在写入边界堵死。
        """
        self.key = str(self.key or "").strip()
        self.email = str(self.email or "").strip()

    def to_dict(self) -> dict[str, Any]:
        # 不输出 `last_error` —— 那是台账合并时按需加的（只在降级时写入）。
        return {
            "key": self.key, "email": self.email, "status": self.status,
            "error": self.error, "created_at": self.created_at,
            "stages": self.stages, "timings": self.timings,
            "api_key": self.api_key, "api_key_suffix": self.api_key_suffix,
            "api_key_created_at": self.api_key_created_at,
            "login": self.login, "user": self.user,
        }


class StageMixin:
    """阶段方法集合。**只能**被 `runner.Pipeline` 继承 —— 它依赖宿主提供的
    `self.mail` / `self.log` / `self.success_ledger` / `self.rotate_key`。
    """

    #: 宿主（`runner.Pipeline`）必须提供的属性 —— 这里只做**类型声明**，不赋值。
    #: 不声明的话，静态检查会把每个 `self.mail` 都报成「未知属性」，
    #: 而真正的约束（谁是宿主）反而没人看。
    mail: Any
    domain: str
    success_ledger: Any
    rotate_key: bool
    confirm_session: bool
    _site_session: Any
    log: Callable[[str], None]

    # ── 站点客户端（每 worker 复用连接池）──────────────────────────────
    def _new_site_client(self) -> TokenJuiceClient:
        """造一个「复用连接池、但 Cookie 干净」的站点客户端。

        实测：到 tokenjuice.ai 的冷连接 ~660ms、热连接 ~223ms ⇒ 每个账号新建
        Session 会白付 ~440ms 的 TLS 握手。这里每个 worker 只建一次 Session，
        切号时 `reset()` 清 Cookie（**不清就串号**，见 `TokenJuiceClient.reset`）。

        ⚠️ 必须留在本模块：`TokenJuiceClient` 是自测的 patch 接缝
        （`tests/support.py::offline()` 替换 `src.stages.TokenJuiceClient`）。
        写到 `runner` 里会让 patch 落空，而生产代码看起来毫无问题。
        """
        if self._site_session is None:
            import requests
            self._site_session = requests.Session()
        cl = TokenJuiceClient(session=self._site_session)
        cl.reset()
        return cl

    # ── 失败登记 ──────────────────────────────────────────────────────
    def _fail(self, rec: AccountRecord, stage: str, msg: str, *,
              status: str | None = "failed", mark: str = "failed") -> bool:
        """登记一次阶段失败，并返回 `False`（供 `return self._fail(...)` 用）。

        三件事必须**同时**发生：写 `stages[stage]`、写 `rec.error`、改 `rec.status`。
        分散在 10 处人肉复制粘贴，漏一处就会出现「有 error 但 stages 显示 ok」
        这种自相矛盾的台账记录。`status=None` 表示不改 status。
        """
        rec.stages[stage] = mark
        rec.error = msg
        if status is not None:
            rec.status = status
        return False

    def _mail_timeout_msg(self, mail_timeout: float) -> str:
        """等信超时的诊断文案 —— 必须把「读不出来」与「没发/丢包」分开。"""
        polls, bad = self.mail.stats.polls, self.mail.stats.http_5xx
        return (f"未在 {mail_timeout:.0f}s 内收到登录邮件"
                f"（邮箱接口轮询 {polls} 次，5xx {bad} 次）—— "
                f"邮件正常 1~3s 必到，超时几乎必然是丢包；"
                f"链接一次性且 {LINK_TTL_SECONDS // 60} 分钟过期，"
                f"重跑 `--mode resume` 会重发新链接（旧链接不会因此失效）")

    # ── 等信 + 批内重发 ───────────────────────────────────────────────
    def _wait_with_retry(self, rec: AccountRecord, cl: TokenJuiceClient, *,
                         mail_timeout: float, since_ms: int,
                         allow_retry: bool = True) -> Any:
        """等一封登录邮件；超时后按 `RETRY_SEND_ON_TIMEOUT` 重发再等。

        `allow_retry=False` 用于「同一轮里回捞」的场景（例如先等码、再回捞链接），
        那里不是丢包，再发信既无必要、又白白增加站点压力。
        """
        t_wait = time.time()
        tries = 0
        while True:
            m = self.mail.wait_for_mail(rec.email, MATCH_LINK,
                                        timeout=mail_timeout,
                                        interval=MAIL_POLL_INTERVAL,
                                        since_ms=since_ms)
            if m is not None:
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return m
            if (not allow_retry or not RETRY_SEND_ON_TIMEOUT
                    or tries >= MAX_SEND_RETRIES):
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return None
            tries += 1
            self.log(f"  [login] ⚠ {mail_timeout:.0f}s 未收到登录邮件"
                     f"（正常 1~3s 必到 ⇒ 疑似丢包），批内重发第 {tries} 次")
            since_ms = int(time.time() * 1000) - 5_000
            try:
                r = cl.send_magic_link(rec.email)
            except TokenJuiceError as exc:
                self.log(f"  [login] ⚠ 重发异常: {exc}")
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return None
            if not r.ok:
                self.log(f"  [login] ⚠ 重发失败 HTTP {r.status}: {r.error}")
                rec.timings["mail_wait"] = (rec.timings.get("mail_wait", 0.0)
                                            + time.time() - t_wait)
                return None
            rec.login["send_retries"] = tries
            self.log(f"  [login] 重发成功，再等 {mail_timeout:.0f}s")

    # ── 阶段 1：发链接 + 收信 + 换会话（唯一入口）─────────────────────
    def stage_login(self, rec: AccountRecord, *,
                    mail_timeout: float = MAIL_TIMEOUT) -> TokenJuiceClient | None:
        """建邮箱（若缺）→ 发链接 → 收信 → 换 `session` Cookie。成功返回 client。

        `rec.email` 为空时本方法自己建一个临时邮箱（`run_batch` 的默认用法）；
        `resume` 传已知邮箱，走同一条路。

        🔴 返回 client 而不是挂 `self.client`：挂实例字段在串行时看不出问题，
        并发时会**串号**（A 账号的 key 建在 B 账号的会话上）。
        """
        t0 = time.time()
        if not rec.email:
            t_mb = time.time()
            rec.email = self.mail.create_mailbox(self.domain)
            rec.timings["mailbox"] = time.time() - t_mb
            rec.key = rec.email
            rec.stages["mailbox"] = "ok"
        self.log(f"  [signup] 邮箱 {rec.email}")

        cl = self._new_site_client()
        since = int(time.time() * 1000) - 5_000
        t_send = time.time()
        try:
            r = cl.send_magic_link(rec.email)
        except TokenJuiceError as exc:
            rec.timings["send"] = time.time() - t_send
            rec.timings["login"] = time.time() - t0
            self._fail(rec, "send", f"发信失败: {exc}")
            return None
        rec.timings["send"] = time.time() - t_send
        if not r.ok:
            rec.timings["login"] = time.time() - t0
            self._fail(rec, "send", r.error)
            return None
        rec.stages["send"] = "ok"

        m = self._wait_with_retry(rec, cl, mail_timeout=mail_timeout,
                                  since_ms=since)
        if m is None:
            # 失败路径也要写 timings（否则「白等了多久」在台账里查不到）
            rec.timings["login"] = time.time() - t0
            self._fail(rec, "login", self._mail_timeout_msg(mail_timeout))
            return None
        rec.stages["mail"] = "ok"
        rec.login["mail_subject"] = m.subject
        rec.login["mail_at"] = m.received_at

        tokens = extract_magic_tokens(m.body)
        rec.login["token_candidates"] = len(tokens)
        if not tokens:
            rec.timings["login"] = time.time() - t0
            self._fail(rec, "login",
                       f"邮件正文里没找到登录 token（主题={m.subject!r}）—— "
                       f"站点可能改了链接形态；改 src/parsing.py 的 MAGIC_LINK_RE")
            return None

        last: Result | None = None
        t_verify = time.time()
        for tok in tokens:
            last = cl.verify(tok)
            if last.ok:
                break
        rec.timings["verify"] = time.time() - t_verify
        if last is None or not last.ok:
            rec.timings["login"] = time.time() - t0
            suffix = f"（已试 {len(tokens)} 枚候选）" if len(tokens) > 1 else ""
            self._fail(rec, "verify", f"{last.error if last else '未执行'}{suffix}")
            return None
        rec.stages["verify"] = "ok"

        t_me = time.time()
        if self.confirm_session:
            me = cl.me()
            rec.timings["me"] = time.time() - t_me
            if not me or not me.get("email"):
                rec.timings["login"] = time.time() - t0
                self._fail(rec, "verify",
                           "verify 返回 204，但 /auth/me 拿不到会话 —— "
                           "Cookie 没保存？站点改了会话形态？")
                return None
            rec.user["profile"] = me
            rec.login["me_email"] = me.get("email")
            rec.stages["me"] = "ok"
        else:
            # 省 ~0.24s/号。会话有效性由下一步 `GET /api-key` 的 401 兜住
            # （`stage_create_key` 会把 401 原样报出来，不会静默继续）。
            rec.timings["me"] = 0.0
            rec.stages["me"] = "skipped"
            rec.login["me_email"] = rec.email
        rec.status = "registered"
        rec.timings["login"] = time.time() - t0
        self.log(f"  [login] 会话建立 ({rec.timings['login']:.1f}s) "
                 f"me={rec.login.get('me_email')}")
        return cl

    # ── 阶段 2：建 key + 当场验活 ────────────────────────────────────
    def stage_create_key(self, rec: AccountRecord, cl: TokenJuiceClient, *,
                         rotate: bool | None = None) -> bool:
        """建 API Key，并**立刻在推理网关验活** —— 这才是「这个账号成不成」的判据。

        🔴 轮换护栏（本链路最重要的一条，实测依据）：

            · `POST /api/v1/api-key` 在**已有 key** 时也返回 200 并生成新 key；
            · 新 key 生效的同时，**旧 key 在网关立刻变 401**（实测）；
            · 而 `GET /api/v1/api-key` 只回 `suffix`，**拿不回明文**。

        ⇒ 如果台账里没有明文（换机器、台账被清、上一轮崩在写盘前），
          「重跑」会**静默销毁**上一把还能用的 key，而交付物里那把 key 立刻失效。
          所以：查到已有 key 且没有明文时**默认不建**，记 `partial` 并说明原因；
          只有显式 `--rotate-key` 才允许轮换。

        ⚠️ 已知 key（`rec.api_key` 非空）时同样不重建：那把 key 就在台账里，
           重建只会把它变成废字符串。
        """
        rotate = self.rotate_key if rotate is None else rotate
        t0 = time.time()

        g = cl.get_api_key()
        if not g.ok:
            return self._fail(rec, "api_key", g.error, status="partial")

        if g.data.get("exists"):
            rec.api_key_suffix = str(g.data.get("suffix") or "")
            rec.api_key_created_at = str(g.data.get("createdAt") or "")
            if rec.api_key:
                rec.stages["api_key"] = "existing"
                rec.status = "keyed"
                rec.error = ""
                self.log(f"  [api_key] 账号已有 key（suffix={rec.api_key_suffix}），"
                         f"台账里也有明文 —— 不重建（重建会让旧 key 失效）")
                return True
            if not rotate:
                return self._fail(
                    rec, "api_key",
                    f"账号已有 API Key（suffix={rec.api_key_suffix or '?'}，"
                    f"createdAt={rec.api_key_created_at or '?'}），但**明文无法从服务端取回**；"
                    f"重建会使旧 key 立刻失效 ⇒ 默认不重建。"
                    f"找回明文后补录台账，或显式传 --rotate-key 接受轮换",
                    status="partial")
            self.log(f"  [api_key] ⚠ --rotate-key：即将轮换 suffix="
                     f"{rec.api_key_suffix}（旧 key 会立刻失效）")

        r = cl.create_api_key()
        if not r.ok:
            return self._fail(rec, "api_key", r.error, status="partial")

        rec.api_key = str(r.data.get("key") or "")
        rec.api_key_suffix = str(r.data.get("suffix") or "")
        rec.api_key_created_at = str(r.data.get("createdAt") or "")
        rec.stages["api_key"] = "ok"
        rec.timings["create_key"] = time.time() - t0

        # ── 验活：控制面给的是字符串，网关接受才算数 ──────────────────
        t_v = time.time()
        v = verify_key(rec.api_key)
        rec.timings["verify_key"] = time.time() - t_v
        rec.user["verify"] = {k: v.get(k) for k in
                              ("ok", "authenticated", "degraded", "status",
                               "chat_status", "model", "error", "attempts")}
        if not v.get("authenticated"):
            # key 建出来了但网关**不认** ⇒ 不能算成功，但明文必须留在台账里
            # （EARNED_FIELDS 保证后续失败不会覆盖它）。
            return self._fail(
                rec, "verify_key",
                f"建 key 成功但**验活失败**（网关拒绝）：{v.get('error') or '未知'}"
                f"（HTTP {v.get('status')}/{v.get('chat_status')}）；"
                f"明文已记入台账（suffix={rec.api_key_suffix}）",
                status="partial")
        if v.get("degraded"):
            # 网关接受了 key，但 chat 非 200（429/5xx/网络）—— **key 本身可用**，
            # 容量问题不该让账号落到下一轮重跑（重跑会轮换、作废旧 key）。
            self.log(f"  [api_key] ⚠ key 已认证，但 chat 未拿到 200"
                     f"（{v.get('chat_status')}）—— 记为降级，不影响交付")

        rec.status = "keyed"
        rec.error = ""
        # 🔴 唯一产出「可用 key」的地方 ⇒ 成功数据挂在这里写，
        # 就自动覆盖 run_batch / resume 全部调用路径。
        self.success_ledger.append(rec.to_dict())
        self.log(f"  [api_key] key 已建 suffix={rec.api_key_suffix} "
                 f"验活={'chat' if v.get('chat_status') == 200 else 'models（降级）'}"
                 f" ({rec.timings['create_key']:.1f}s + "
                 f"{rec.timings['verify_key']:.1f}s)")
        return True
