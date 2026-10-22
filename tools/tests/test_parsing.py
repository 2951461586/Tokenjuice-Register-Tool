"""解析层测试：token 提取的形态覆盖 + 负对照。"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。



from __future__ import annotations

from tests.support import check

from src import parsing as ps

BODY = (
    "Sign in to Token Juice\n\n"
    "Click the button below to sign in. The link works once and expires in 15 minutes.\n\n"
    "Sign in (https://tokenjuice.ai/auth/verify#token=VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI)\n\n"
    "https://tokenjuice.ai/auth/verify#token=VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI\n"
)


def test_token_extraction() -> None:
    print("\n[parsing] token 提取")
    toks = ps.extract_magic_tokens(BODY)
    check("fragment 形态（#token=）能提取", toks == ["VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI"],
          str(toks))
    check("同一 URL 出现两次只返回一枚（去重）", len(toks) == 1, str(toks))

    q = "https://tokenjuice.ai/auth/verify?token=abcDEF1234567890_-xyz"
    check("查询串形态（?token=）也接受",
          ps.extract_magic_tokens(q) == ["abcDEF1234567890_-xyz"], str(ps.extract_magic_tokens(q)))

    html = ('<a href="https://tokenjuice.ai/auth/verify?token=abcDEF1234567890&amp;'
            'x=1">go</a>')
    check("HTML 实体反转义（&amp; 不能让参数名变成 amp;token）",
          ps.extract_magic_tokens(html) == ["abcDEF1234567890"], str(ps.extract_magic_tokens(html)))

    truncated = "https://tokenjuice.ai/auth/verify#token=short"
    check("长度不足 16 的 token 片段被丢弃（负对照）",
          ps.extract_magic_tokens(truncated) == [], str(ps.extract_magic_tokens(truncated)))

    other = "https://example.com/auth/verify#token=abcDEF1234567890_-xyz"
    check("非 tokenjuice.ai 域名的链接不认（负对照）",
          ps.extract_magic_tokens(other) == [], str(ps.extract_magic_tokens(other)))

    check("空正文返回空列表", ps.extract_magic_tokens("") == [])
    check("extract_magic_token 返回第一枚",
          ps.extract_magic_token(BODY) == "VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI")

    two = (BODY + "\nhttps://tokenjuice.ai/auth/verify#token=secondToken1234567890")
    check("多枚候选按出现顺序返回",
          ps.extract_magic_tokens(two) == [
              "VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI",
              "secondToken1234567890"], str(ps.extract_magic_tokens(two)))
