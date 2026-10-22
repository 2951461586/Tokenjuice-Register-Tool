#!/usr/bin/env python
"""离线自测入口 —— 聚合与调度，测试本体在 `tools/tests/`。

跑法（零第三方依赖，不是 pytest）：

    python tools/selftest.py

覆盖：

    tests/test_parsing.py        登录 token 提取（fragment / 查询串 / HTML 实体 / 负对照）
    tests/test_mailrules.py      收件规则（sender + subject 双条件 / 漏网主题）
    tests/test_ledger.py         台账（并集 / 等级保护 / 已赚字段 / 状态词汇 / LF）
    tests/test_orchestration.py  编排（全链路 / 轮换护栏 / 重跑去重 / 并发 / 验活判据）

⚠️ 全部离线：`tests/support.py::offline()` 替换三个网络接缝。
"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/

from tests import support
from tests.test_ledger import (
    test_identity_whitespace_normalization,
    test_ledger_union,
    test_rank_protection,
    test_status_vocabulary,
)
from tests.test_mailrules import test_mailrules
from tests.test_orchestration import (
    test_apicheck_failure_is_partial,
    test_apicheck_semantics,
    test_chain_uses_single_source_for_verify,
    test_concurrency_no_crosstalk,
    test_full_chain,
    test_resume_skips_keyed,
    test_rotation_guard,
    test_send_failure,
    test_timeout_resends_once,
    test_verify_failure,
    test_worker_crash_is_recorded,
)
from tests.test_parsing import test_token_extraction

TESTS = (
    test_token_extraction,
    test_mailrules,
    test_ledger_union,
    test_rank_protection,
    test_identity_whitespace_normalization,
    test_status_vocabulary,
    test_full_chain,
    test_send_failure,
    test_verify_failure,
    test_timeout_resends_once,
    test_rotation_guard,
    test_resume_skips_keyed,
    test_apicheck_failure_is_partial,
    test_concurrency_no_crosstalk,
    test_worker_crash_is_recorded,
    test_apicheck_semantics,
    test_chain_uses_single_source_for_verify,
)


def main() -> int:
    print("=" * 74)
    print("Token Juice 注册链路 · 离线自测")
    print("=" * 74)
    for fn in TESTS:
        fn()
    ok, bad = support.counts()
    print("\n" + "=" * 74)
    print(f"通过 {ok} / 失败 {bad}")
    print("=" * 74)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
