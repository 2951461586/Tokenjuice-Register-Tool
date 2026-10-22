"""集中配置。凭据一律走环境变量，默认值留空 —— 不把基础设施标识写进仓库。

用 stdlib 实现的 `.env` 加载器（不引 python-dotenv）。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载器：真实环境变量优先级更高。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")

# ── 收信：CF Temp Email Worker ─────────────────────────────────────────
# 三个都不留真实默认值：Worker 子域 + 自有域名合起来是可被针对性打击的基础设施标识。
TEMPMAIL_BASE = os.getenv("TEMPMAIL_BASE", "")
TEMPMAIL_ADMIN_KEY = os.getenv("TEMPMAIL_ADMIN_KEY", "")
# 建邮箱用哪个域名（必须是 Worker /health 的 domains 之一）
TEMPMAIL_DOMAIN = os.getenv("TEMPMAIL_DOMAIN", "")

# ── Token Juice 站点（控制面）──────────────────────────────────────────
# 全部端点都挂在同源 /api/v1 下，认证靠 `session` Cookie（见 tokenjuice.py）。
SITE_ORIGIN = "https://tokenjuice.ai"
SITE_VERIFY_PATH = "/auth/verify"
SEND_LINK_PATH = "/api/v1/send-magic-link"
VERIFY_PATH = "/api/v1/auth/verify"
ME_PATH = "/api/v1/auth/me"
API_KEY_PATH = "/api/v1/api-key"
SIGNOUT_PATH = "/api/v1/auth/signout"

# ── Token Juice 推理网关（数据面）──────────────────────────────────────
# 「验活」的判据在这里：控制面返回的 `tj_...` 只是字符串，
# 只有 api.tokenjuice.ai 接受它，才算这个账号真的可用。
API_ORIGIN = os.getenv("TOKENJUICE_API_ORIGIN", "https://api.tokenjuice.ai/v1")
# 站点当前唯一公开模型（取自 OverviewPage 的示例代码与 GET /v1/models）。
DEFAULT_MODEL = os.getenv("TOKENJUICE_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash-1017")

# ── 网络 ───────────────────────────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# ── 输出 ───────────────────────────────────────────────────────────────
# exports/  运行台账：**全部尝试**（含失败），用于复盘
# result/   交付物：只放拿到且验活的 key（含明文，目录整体 gitignore）
EXPORT_DIR = ROOT / "exports"
LEDGER_PATH = EXPORT_DIR / "ledger.jsonl"

RESULT_DIR = ROOT / "result"
SUCCESS_LEDGER_PATH = RESULT_DIR / "success.jsonl"      # 账号级（按邮箱去重）
KEYS_TXT_PATH = RESULT_DIR / "keys.txt"                 # email----api_key----suffix
KEYS_JSON_PATH = RESULT_DIR / "keys_verified.json"      # 机器可读验收结果
APIKEYS_TXT_PATH = RESULT_DIR / "apikeys.txt"           # 纯 key，一行一个


def validate() -> list[str]:
    """返回缺失的必需配置项。

    刻意不在 import 时抛错 —— 否则连 `--help` 都跑不起来。
    """
    return [k for k, v in (("TEMPMAIL_BASE", TEMPMAIL_BASE),
                           ("TEMPMAIL_ADMIN_KEY", TEMPMAIL_ADMIN_KEY),
                           ("TEMPMAIL_DOMAIN", TEMPMAIL_DOMAIN)) if not v]
