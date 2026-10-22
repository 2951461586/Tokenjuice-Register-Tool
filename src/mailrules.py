"""收件过滤规则表 —— Token Juice 侧。

判据与参考项目一致：**信封发件人子串做第一道，主题子串做第二道**。
只按发件人过滤是不够的 —— SES 给很多站点代发（同一台 Worker 还被邻居项目共用），
只按主题过滤也不够 —— 主题文案会变，而发件人域是稳定的。

2026-10-22 实测（两封独立邮件 + 一次重发）：

    from:   010101a1…-000000@us-west-2.amazonses.com
    subject: Your Token Juice sign-in link
    body:   Sign in to Token Juice … The link works once and expires in 15 minutes.

⇒ 一条规则就够。**不要凭印象加规则**：窗口里出现新主题时，`--mode scan`
会把它列进「漏网主题」（这正是 `diagnose()` 的用途），届时按实际文案补。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: 发件人域常量。改这里就够了，不要在规则里散落字符串。
SENDER_SES = "amazonses.com"


def _norm(s: str) -> str:
    """NFKC 归一化 + 小写。

    主题里可能出现弯引号/全角字符，规则只写无标点片段 + 归一化兜底，
    这样 `You're` 与 `You’re` 都能命中。
    """
    return unicodedata.normalize("NFKC", s or "").lower()


@dataclass(frozen=True)
class MailRule:
    """一条收件规则。字段都是**大小写不敏感的子串**，不是正则。"""

    name: str
    stage: str
    sender_contains: str
    subject_contains: str
    subject_excludes: tuple[str, ...] = ()
    note: str = ""

    def matches(self, mail: Any) -> bool:
        sender = _norm(getattr(mail, "sender", "") or "")
        subject = _norm(getattr(mail, "subject", "") or "")
        if self.sender_contains and _norm(self.sender_contains) not in sender:
            return False
        if self.subject_contains and _norm(self.subject_contains) not in subject:
            return False
        return not any(_norm(x) in subject for x in self.subject_excludes)

    def __call__(self, mail: Any) -> bool:
        """让规则本身可直接当 `wait_for_mail` 的谓词用。"""
        return self.matches(mail)


RULES: tuple[MailRule, ...] = (
    MailRule(
        name="signin_link",
        stage="1-login",
        sender_contains=SENDER_SES,
        subject_contains="token juice sign-in link",
        note="登录魔法链接邮件。**全链路唯一凭据来源**："
             "正文里的 token 一次性、15 分钟过期，提交到 "
             "POST /api/v1/auth/verify 换 `session` Cookie。"
             "⚠️ 主题只写 `sign-in link` 也可，但保留完整短语可避免"
             "将来「sign-in code」类邮件被同一条规则误吃。",
    ),
)

_BY_NAME = {r.name: r for r in RULES}

#: 等信时使用的规则集合（当前只有一条，但保留分组常量：
#: 将来加「验证码」邮件时，调用方不用改）。
LINK_RULES: tuple[MailRule, ...] = (_BY_NAME["signin_link"],)


def any_of(*rules: MailRule):
    """把多条规则合成一个谓词（OR）。参数是 `MailRule` 对象，不是规则名。"""
    return lambda m: any(r.matches(m) for r in rules)


def sender_ok(mail: Any, sender_contains: str = SENDER_SES) -> bool:
    return _norm(sender_contains) in _norm(getattr(mail, "sender", "") or "")


def classify(mail: Any) -> str:
    for r in RULES:
        if r.matches(mail):
            return r.name
    return "unknown"


def diagnose(mails: Iterable[Any]) -> dict[str, Any]:
    """按规则分桶 + 列出「发件人是 SES 但主题没规则认领」的漏网主题。"""
    buckets: dict[str, list[Any]] = {r.name: [] for r in RULES}
    buckets["unknown"] = []
    foreign = 0
    for m in mails:
        if not sender_ok(m):
            foreign += 1
            continue
        buckets[classify(m)].append(m)
    unclaimed = sorted({(getattr(m, "subject", "") or "").strip()
                        for m in buckets["unknown"]})
    return {"buckets": buckets, "foreign": foreign,
            "unclaimed_subjects": unclaimed}
