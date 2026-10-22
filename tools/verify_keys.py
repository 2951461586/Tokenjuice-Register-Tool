#!/usr/bin/env python
"""验证台账里的 API Key 是否**真的能调通**，并导出可用凭据到 `result/`。

为什么必须做这一步：拿到 `tj_...` 只证明「创建接口返回了字符串」，
不证明它在推理网关上有效（可能被轮换、风控、停用）。

判据（`src/apicheck.py` 的唯一实现，本工具与生产阶段共用）：

    GET  /v1/models          200 = key 被网关接受  ← **交付判据**
    POST /v1/chat/completions 200 = 真的能出 token  ← 记录为 `ok`（更强证据）

🔴 **交付判据是 `authenticated`（models 200），不是 chat 200**：
   chat 的 429/5xx 是容量或上游问题，不代表 key 失效。用 chat 当唯一判据
   会让可用 key 从交付物里静默消失（参考项目实测过 `SSLError` 误判）。

候选集 = 台账**合并视图** ∪ **原始行**：合并视图按邮箱去重，同一账号重跑
（轮换前）的第二把 key 会被吃掉 —— 两把都曾真实存在过，验收要能看到它们
（旧的那把现在应该已 401，这本身就是轮换护栏的验收证据）。

产物（全部落在 `result/`，即交付物目录）：

    result/success.jsonl        成功**账号**（账号级：每邮箱一行；幂等补录）
    result/keys.txt             email----api_key----suffix（人可读）
    result/keys_verified.json   机器可读的验收结果
    result/apikeys.txt          纯 api_key，一行一个

用法：
    python tools/verify_keys.py                      # 验证全部有 key 的记录
    python tools/verify_keys.py --limit 3
    python tools/verify_keys.py --ledger other.jsonl
"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。


from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
from _bootstrap import ROOT  # noqa: F401  （副作用：把仓库根加进 sys.path）

from src import config
from src.apicheck import verify_key
from src.ledger import Ledger


def _write_lf(path: str, text: str) -> None:
    """只写 LF。

    Windows 默认把 `\\n` 翻成 `\\r\\n`，于是每行尾部都带 CR：
      · `keys.txt` 是凭据清单，Linux/WSL 下 `while read` 读出来的 api_key
        会变成 `tj_xxx\\r` ⇒ 直接 401，而肉眼完全看不出区别；
      · `success.jsonl` 是 JSONL，CR 会让严格解析器报错。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.KEYS_TXT_PATH),
                    help=f"人可读凭据清单（默认 {config.KEYS_TXT_PATH}）")
    ap.add_argument("--json", default=str(config.KEYS_JSON_PATH),
                    help=f"机器可读验收结果（默认 {config.KEYS_JSON_PATH}）")
    ap.add_argument("--apikeys", default=str(config.APIKEYS_TXT_PATH),
                    help=f"纯 api_key 清单（默认 {config.APIKEYS_TXT_PATH}）")
    ap.add_argument("--ledger", default=str(config.LEDGER_PATH),
                    help=f"源台账（默认 {config.LEDGER_PATH}）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    led = Ledger(Path(args.ledger))
    recs = [r for r in led.load() if r.get("api_key")]

    # 再补一遍**原始行**里的 key（合并视图会吃掉同账号的第二把）。
    have = {r["api_key"] for r in recs}
    extra = [r for r in led.raw_rows() if r.get("api_key") and r["api_key"] not in have]
    if extra:
        for r in extra:
            have.add(r["api_key"])
        recs += extra
        print(f"另有 {len(extra)} 把 key 只存在于原始行里"
              f"（合并视图按邮箱去重时被吃掉）—— 一并验收")

    # 补录：把记录同步进 result/success.jsonl（交付物目录）。幂等、可反复跑。
    succ = Ledger(config.SUCCESS_LEDGER_PATH)
    st = succ.upsert_many(recs)
    n_rows = len(succ.raw_rows())
    emails_in = {r["key"] for r in recs}
    processed = st["added"] + st["updated"] + st["kept"]
    if processed != len(recs):
        raise RuntimeError(f"upsert 只处理了 {processed} / {len(recs)} 条输入")
    missing = emails_in - {r["key"] for r in succ.load()}
    if missing:
        raise RuntimeError(
            f"补录后仍有 {len(missing)} 个邮箱不在 success.jsonl 里："
            f"{sorted(missing)[:3]}")
    print(f"成功台账 {config.SUCCESS_LEDGER_PATH}：新增 {st['added']} / "
          f"更新 {st['updated']} / 未变 {st['kept']}（输入 {len(recs)} 条）")
    print(f"  落盘 {n_rows} 行（**账号级**，每行一个邮箱）")

    # 同一邮箱可能有多条（重跑过），按 key 去重
    seen: dict[str, dict] = {}
    for r in recs:
        seen.setdefault(r["api_key"], r)
    items = list(seen.items())
    if args.limit:
        items = items[:args.limit]
    print(f"台账 {args.ledger}：{len(recs)} 条有 key 的记录，"
          f"去重后 {len(items)} 个唯一 key\n")

    results = []
    for i, (key, rec) in enumerate(items, 1):
        v = verify_key(key)
        v["email"] = str(rec.get("email", "") or "").strip()
        v["api_key"] = key
        v["api_key_suffix"] = rec.get("api_key_suffix", "")
        results.append(v)
        flag = ("✓" if v.get("authenticated")
                else ("?" if v.get("inconclusive") else "✗"))
        if v.get("authenticated"):
            detail = (f"models=200 chat={v.get('chat_status')} "
                      f"model={v.get('model')} "
                      f"tokens={v.get('usage', {}).get('total_tokens', '?')}")
            if v.get("degraded"):
                detail += f" ⚠降级：{str(v.get('error'))[:60]}"
        elif v.get("inconclusive"):
            detail = (f"HTTP {v.get('status')} {str(v.get('error'))[:60]} "
                      f"⚠ 网关 429/5xx，重试后仍不确定 —— "
                      f"**未写入交付清单**，稍后重跑即可")
        else:
            detail = f"HTTP {v.get('status')} {str(v.get('error'))[:70]}"
        print(f"  [{i}/{len(items)}] {flag} {rec.get('email', ''):<44} "
              f"{v['elapsed']:.1f}s  {detail}")

    ok = sum(1 for v in results if v.get("authenticated"))
    unsure = sum(1 for v in results if v.get("inconclusive"))
    bad = len(results) - ok - unsure
    print(f"\n可用 {ok} / 不确定 {unsure} / 不可用 {bad} / 合计 {len(results)}")

    _write_lf(args.json, json.dumps(results, ensure_ascii=False, indent=2))
    lines = ["# Token Juice API Keys（已实测可用）",
             f"# 验证时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"# 端点：POST {config.API_ORIGIN}/chat/completions",
             "# 认证：Authorization: Bearer <key>",
             "# 格式：<email>----<api_key>----<suffix>", ""]
    for v in results:
        if v.get("authenticated"):
            lines.append(f"{v['email']}----{v['api_key']}----{v['api_key_suffix']}")
    _write_lf(args.out, "\n".join(lines) + "\n")

    # 纯 key 版：与 keys.txt 同源、同一次运行写出 ⇒ 两者集合恒等，只差元数据形态。
    # ⚠️ 同样只写 authenticated 的行 —— 把不可用的 key 放进交付清单等于交付坏数据。
    apikeys = [v["api_key"] for v in results if v.get("authenticated")]
    if len(apikeys) != len(set(apikeys)):
        raise RuntimeError("apikeys.txt 出现重复 key")
    _write_lf(args.apikeys, "".join(f"{k}\n" for k in apikeys))

    print(f"已写入 {args.out}、{args.apikeys} 和 {args.json}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
