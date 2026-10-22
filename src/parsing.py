"""邮件正文 → 登录凭据。**纯函数，零第三方依赖。**

Token Juice 的登录邮件形态（2026-10-22 实测，来自 `tokenjuice-2.ai.har` 同批实测）：

    Subject: Your Token Juice sign-in link
    Sign in (https://tokenjuice.ai/auth/verify#token=VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI)
    …copy this address into your browser:
    https://tokenjuice.ai/auth/verify#token=VkAw1y-oZ13zNgDsQtp02N1ae_UpsqTScKSdJEDfGkI

🔴 三个必须记住的形态差异（与参考项目 TypeSafe/Stytch 的魔法链接不同）：

1. token 在 **URL fragment**（`#token=`）里，不是查询串（`?token=`）。
   浏览器不会把 fragment 发给服务端，所以「打开链接」本身没有意义 ——
   真实动作是页面里的 JS 读 `location.hash`，再
   `POST /api/v1/auth/verify {"token": …, "retainData": true}`。
   ⇒ 本项目**直接提取 token**，不做「GET 链接 → 解析 HTML」那一步。
   正则仍同时接受 `?token=`：站点换形态时不该整条链路失效。

2. 正文是 **纯文本**（Worker 里 `raw_html` 为空），但不同邮箱后端可能给 HTML。
   所以提取前统一做一次 HTML 实体反转义（`&amp;` → `&`）——
   对纯文本是无操作，对 HTML 是必需的（否则参数名会变成 `amp;token`）。

3. 链接是**一次性 + 15 分钟**过期（邮件正文原话）。同一 URL 在正文里出现两次
   （按钮 + 纯文本兜底），去重后只剩一条 token。
"""

from __future__ import annotations

import html as html_mod
import re
import unicodedata

from . import config

#: 登录链接的**唯一**形态。用 config 派生，不在正则里再写一遍域名。
#: `[?#]` 同时接受 fragment 与查询串；结尾的 `[^\s"'<>)\]]` 排掉正文里的
#: 右括号（Markdown 形态 `(url)`）与引号。
MAGIC_LINK_RE = re.compile(
    re.escape(config.SITE_ORIGIN) + re.escape(config.SITE_VERIFY_PATH)
    + r"[?#][^\s\"'<>)\]]+"
)

#: token 取值。字符集来自实测：base64url 字母表 + `_` / `-`。
#: 长度下限 16 是**质量门槛**：正文里可能残留被截断的片段，
#: 拿它去换会话只会白消耗一次请求（服务端一律 400，不消耗一次性 token）。
TOKEN_PARAM_RE = re.compile(r"[?#&]token=([A-Za-z0-9_\-]{16,})")


def _norm(text: str) -> str:
    """NFKC 归一化 + HTML 实体反转义。

    NFKC 顺带把全角字符折成 ASCII（邮件客户端偶尔会做这种转换）。
    """
    return html_mod.unescape(unicodedata.normalize("NFKC", text or ""))


def extract_magic_tokens(text: str) -> list[str]:
    """取出正文里**所有** token 候选：去重、保持出现顺序。

    返回列表而不是单个 token：站点可能同时给出多条链接（重发过、
    或正文里既有按钮又有纯文本兜底）。调用方应**按序逐个试**直到
    某一枚真的换到会话 —— 判据是终态，不是「token 看起来对不对」。
    失效的 token 只会拿回 400，不会消耗任何东西，所以逐条试是安全的。
    """
    if not text:
        return []
    seen: dict[str, None] = {}
    for url in MAGIC_LINK_RE.findall(_norm(text)):
        m = TOKEN_PARAM_RE.search(url)
        if m:
            seen.setdefault(m.group(1), None)
    return list(seen)


def extract_magic_token(text: str) -> str:
    """只要最优的一枚 token（没有则空串）。"""
    toks = extract_magic_tokens(text)
    return toks[0] if toks else ""


def visible_text(page: str) -> str:
    """剥掉标签，得到压缩过的可见文案（报错/日志用）。"""
    t = re.sub(r"<script.*?</script>", "", page or "", flags=re.DOTALL)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(html_mod.unescape(t).split())
