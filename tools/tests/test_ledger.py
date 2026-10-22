"""台账测试：并集合并 / 等级保护 / 已赚字段 / 状态词汇 / LF 输出。"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from tests.support import check

from src import stages as st
from src.ledger import RANK, Ledger, merge


def test_ledger_union() -> None:
    print("\n[ledger] 并集合并与幂等")
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(Path(td) / "ledger.jsonl")
        led.append({"key": "a@b.test", "email": "a@b.test", "status": "registered",
                    "stages": {"send": "ok"}, "timings": {"login": 1.0}})
        led.append({"key": "a@b.test", "email": "a@b.test", "status": "keyed",
                    "api_key": "tj_x", "stages": {"api_key": "ok"}})
        recs = led.load()
        check("同键合并成一条", len(recs) == 1, str(recs))
        r = recs[0]
        check("升级时新值胜出", r["status"] == "keyed")
        check("dict 字段做并集（stages 不丢）",
              r["stages"] == {"send": "ok", "api_key": "ok"}, str(r["stages"]))
        check("dict 字段做并集（timings 不丢）", r["timings"] == {"login": 1.0})

        st = led.upsert_many([{"key": "a@b.test", "email": "a@b.test",
                               "status": "keyed", "api_key": "tj_x"}])
        check("幂等：同内容 upsert 记 kept",
              st == {"added": 0, "updated": 0, "kept": 1}, str(st))

        raw = (Path(td) / "ledger.jsonl").read_bytes()
        check("JSONL 只写 LF（无 CR）", b"\r" not in raw)


def test_rank_protection() -> None:
    print("\n[ledger] 等级保护（重跑失败不能清掉凭据）")
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(Path(td) / "ledger.jsonl")
        led.append({"key": "a@b.test", "email": "a@b.test", "status": "keyed",
                    "api_key": "tj_live", "api_key_suffix": "live"})
        led.append({"key": "a@b.test", "email": "a@b.test", "status": "failed",
                    "error": "重跑时链接过期"})
        r = led.load()[0]
        check("低等级记录不覆盖成功记录", r["status"] == "keyed", str(r))
        check("已赚字段 api_key 未被空值覆盖", r["api_key"] == "tj_live")
        check("失败原因记进 last_error", r.get("last_error") == "重跑时链接过期",
              str(r.get("last_error")))

    # 同级空壳也不能清掉凭据
    old = {"key": "a", "email": "a", "status": "keyed", "api_key": "tj_live"}
    new = {"key": "a", "email": "a", "status": "keyed", "api_key": ""}
    check("同级合并保住已赚字段", merge(old, new)["api_key"] == "tj_live")


def test_identity_whitespace_normalization() -> None:
    print("\n[ledger] 身份字段削空白")
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(Path(td) / "ledger.jsonl")
        led.append({"key": "a@b.test\r", "email": " a@b.test ", "status": "started"})
        r = led.load()[0]
        check("key 去掉 \\r", r["key"] == "a@b.test", repr(r["key"]))
        check("email 去掉首尾空白", r["email"] == "a@b.test", repr(r["email"]))


def test_status_vocabulary() -> None:
    """AST 扫 `src/stages.py`：写入的每个 status 字面量都必须在 `ledger.RANK` 里。

    这是参考项目踩过的坑：等级表词汇与实际写入词汇错配 ⇒ 全部并列 0 分 ⇒
    「一次重跑失败就把 api_key 覆盖成空串」，账号从交付物里静默消失。
    """
    print("\n[ledger] 状态词汇必须与写入方一致")
    src = Path(st.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    written: set[str] = set()
    for node in ast.walk(tree):
        # rec.status = "..." / self._fail(..., status="...")
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute) and t.attr == "status"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    written.add(node.value.value)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "status" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    written.add(kw.value.value)
    missing = written - set(RANK)
    check(f"stages.py 写入的 status 全部登记在 RANK（{sorted(written)}）",
          not missing, f"未登记：{sorted(missing)}")
