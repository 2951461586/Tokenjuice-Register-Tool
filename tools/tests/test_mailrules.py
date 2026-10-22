"""收件规则测试：sender + subject 双条件 + 漏网主题诊断。"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。

from __future__ import annotations

from tests.support import check, mail

from src.mailrules import RULES, diagnose

SUBJ = "Your Token Juice sign-in link"


def test_mailrules() -> None:
    print("\n[mailrules] 规则匹配")
    rule = RULES[0]
    m = mail("a@b.test", SUBJ)
    check("SES 发件人 + 主题命中", rule.matches(m))
    check("规则可当谓词直接调用", rule(m))

    check("主题大小写不敏感", rule.matches(mail("a@b.test", SUBJ.upper())))
    check("主题含弯引号也不影响（规则只写无标点片段）",
          rule.matches(mail("a@b.test", "Your Token Juice sign-in link")))

    check("发件人不是 SES ⇒ 不命中（负对照）",
          not rule.matches(mail("a@b.test", SUBJ, sender="mail@example.com")))
    check("主题不是登录链接 ⇒ 不命中（负对照）",
          not rule.matches(mail("a@b.test", "Your Token Juice receipt")))

    d = diagnose([
        mail("a@b.test", SUBJ),
        mail("a@b.test", "Your Token Juice receipt"),      # SES 但主题未认领
        mail("a@b.test", SUBJ, sender="x@example.com"),    # 别人的邮件
    ])
    check("diagnose 命中规则的分桶", len(d["buckets"]["signin_link"]) == 1)
    check("diagnose 统计邻居邮件", d["foreign"] == 1, str(d))
    check("diagnose 列出漏网主题",
          d["unclaimed_subjects"] == ["Your Token Juice receipt"],
          str(d["unclaimed_subjects"]))
