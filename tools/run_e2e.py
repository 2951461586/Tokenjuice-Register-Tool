#!/usr/bin/env python
"""端到端跑批 CLI。

用法：

    # 全链路：建临时邮箱 → 发链接 → 换会话 → 建 key → 验活 → 入库
    python tools/run_e2e.py --count 1

    # 对**已知邮箱**重跑（发信丢包 / 链接过期时用；默认跳过已拿到 key 的账号）
    python tools/run_e2e.py --mode resume --email a@b.com

    # 诊断：列出邮箱窗口内全部邮件并按规则分桶（看有没有漏网主题）
    python tools/run_e2e.py --mode scan

    # 环境体检（Worker /health + 建邮箱 + 站点与网关可达性）
    python tools/run_e2e.py --doctor

⚠️ 与参考项目的一个关键差异：`--mode resume` **默认跳过已有 api_key 的账号**，
   因为 Token Juice 建新 key 会让旧 key 立刻失效（轮换），而明文取不回来。
   确需轮换：`--rotate-key --no-skip-keyed`（会作废旧凭据，想清楚再跑）。
"""

# pyright: reportMissingImports=false
# ^ `src` 是在**运行时**由 `_bootstrap` 加进 sys.path 的；静态分析器
#   看不到这一点，但这不是缺失依赖（`pyright -p pyrightconfig.json` 为 0 错）。


from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tools/
import requests
from _bootstrap import ROOT  # noqa: F401  （副作用：把仓库根加进 sys.path）

from src import config
from src.ledger import Ledger
from src.runner import (
    DEFAULT_MAIL_BACKEND,
    MAIL_BACKENDS,
    AccountRecord,
    Pipeline,
    make_mail_client,
)
from src.stages import MAIL_TIMEOUT
from src.tempemail import TempMailClient


def _probe_site() -> list[str]:
    """只读可达性检查：**不建邮箱、不发信、不建 key**。

    期望值本身就是判据：
      · `GET /api/v1/auth/me` 无 Cookie -> **401**（站点活着且鉴权正常）
      · `GET /v1/models` 无 key        -> **401**（网关活着且鉴权正常）
    返回问题列表（空 = 正常）。
    """
    problems: list[str] = []
    try:
        r = requests.get(f"{config.SITE_ORIGIN}{config.ME_PATH}", timeout=20,
                         headers={"User-Agent": config.UA})
        if r.status_code == 401:
            print("✓ 控制面 ok：/auth/me 未登录返回 401（预期）")
        else:
            problems.append(f"/auth/me 期望 401，实际 {r.status_code}: {r.text[:120]}")
    except requests.RequestException as exc:
        problems.append(f"控制面不可达: {type(exc).__name__}: {exc}")

    try:
        r = requests.get(f"{config.API_ORIGIN}/models", timeout=20,
                         headers={"User-Agent": config.UA})
        if r.status_code == 401:
            print("✓ 推理网关 ok：/v1/models 无 key 返回 401（预期）")
        else:
            problems.append(f"/v1/models 期望 401，实际 {r.status_code}: {r.text[:120]}")
    except requests.RequestException as exc:
        problems.append(f"推理网关不可达: {type(exc).__name__}: {exc}")
    return problems


def cmd_doctor() -> int:
    """环境体检。CF Worker 建邮箱免费，所以直接建一个端到端验一遍。"""
    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值", file=sys.stderr)
        return 1

    c = make_mail_client()
    try:
        h = c.health()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 邮箱服务不可用：{exc}", file=sys.stderr)
        return 1
    print(f"✓ 邮箱服务 ok，storage={h.get('storage')} db={h.get('database')}")
    print(f"  可用域名：{', '.join(h.get('domains') or [])}")
    if config.TEMPMAIL_DOMAIN not in (h.get("domains") or []):
        print(f"  ⚠ TEMPMAIL_DOMAIN={config.TEMPMAIL_DOMAIN!r} 不在 domains 里 —— "
              f"建邮箱会失败或落到默认域")

    try:
        mb = c.create_mailbox()
        print(f"✓ 建邮箱 ok：{mb}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 建邮箱失败：{exc}", file=sys.stderr)
        return 1

    problems = _probe_site()
    for p in problems:
        print(f"✗ {p}", file=sys.stderr)
    if problems:
        return 1

    led = Ledger(config.LEDGER_PATH)
    recs = led.load()
    keyed = sum(1 for r in recs if r.get("api_key"))
    print(f"✓ 台账 {config.LEDGER_PATH} 现有 {len(recs)} 条（其中 {keyed} 条有 key）")
    print(f"  交付目录 {config.RESULT_DIR}")
    return 0


def cmd_scan() -> int:
    """按 `src/mailrules.py` 的规则表给窗口内全部邮件分桶。

    🔴 关键：**把「漏网主题」显式列出来**。站点改文案时，只报「没收到邮件」
    是查不出原因的；列出漏网主题能一眼看出是文案变了还是真没发。
    """
    from src.mailrules import RULES, diagnose

    c = TempMailClient()
    msgs = c.scan_all()
    if not msgs:
        print("窗口内没有邮件")
        return 0
    lo = min(m.received_at for m in msgs)
    hi = max(m.received_at for m in msgs)
    print(f"窗口 {len(msgs)} 封，时间跨度 {(hi - lo) / 60000:.1f} 分钟"
          f"（服务端保留最近 100 行，超出即删）")
    print("发件人过滤：sender 含 amazonses.com（规则表见 src/mailrules.py）\n")

    d = diagnose(msgs)
    for rule in RULES:
        hits = d["buckets"][rule.name]
        print(f"  ★ {rule.name:<16} {len(hits):>3} 封   [{rule.stage}]")
        for m in hits[:3]:
            print(f"        {m.recipient:<44} {m.received_at}")
        if len(hits) > 3:
            print(f"        …（共 {len(hits)} 封）")
    print(f"\n  邻居项目/无关邮件（发件人不含 amazonses.com）：{d['foreign']} 封")

    un = d["unclaimed_subjects"]
    if un:
        print(f"\n  ⚠ 是我们的但**没有规则认领**的主题 {len(un)} 种 —— 站点可能改了文案：")
        for s in un:
            print(f"      {s[:76]}")
    else:
        print("\n  ✓ 没有漏网主题：窗口内所有 SES 邮件都被规则覆盖")
    return 0


def report(recs: list[AccountRecord], *, json_path: str = "") -> int:
    """结果汇总。分类口径与台账状态一一对应，不要在这里另造一套词。"""
    print("\n" + "=" * 74)
    print("结果汇总")
    print("=" * 74)
    keyed = sum(1 for r in recs if r.status == "keyed")
    partial = sum(1 for r in recs if r.status == "partial")
    other = len(recs) - keyed - partial
    print(f"  拿到并验活 key {keyed} / 已建会话但未交付 {partial} / "
          f"其它失败 {other} / 合计 {len(recs)}")
    for r in recs:
        print(f"  - {r.email:<44} {r.status:<10} {r.stages}")
        if r.error:
            print(f"      ↳ {r.error}")

    timed = [r for r in recs if r.timings]
    if timed:
        slow = max(timed, key=lambda r: sum(r.timings.values()))
        print(f"\n  最慢账号 {slow.email} 阶段分解：")
        for k, v in slow.timings.items():
            print(f"    {k:<12} {v:6.2f}s")

    led = Ledger(config.LEDGER_PATH)
    print(f"\n  台账 {config.LEDGER_PATH} 现有 {len(led.load())} 条")

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(
            json.dumps([r.to_dict() for r in recs], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  结果已写入 {json_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Token Juice 端到端注册链路")
    ap.add_argument("--mode", choices=["full", "resume", "scan"], default="full",
                    help="full=建新邮箱跑全链路（默认）；"
                         "resume=对 --email 给的已知邮箱重跑（可反复重跑）；"
                         "scan=列出邮箱窗口内全部邮件并按规则分桶（只读诊断）")
    ap.add_argument("--count", type=int, default=1, help="批次数量（mode=full）")
    ap.add_argument("--email", action="append", default=[], help="指定邮箱（mode=resume）")
    ap.add_argument("--mail-backend", choices=list(MAIL_BACKENDS),
                    default=DEFAULT_MAIL_BACKEND,
                    help="邮箱后端（当前只有 cf = Cloudflare 临时邮箱 Worker，建邮箱免费）")
    ap.add_argument("--domain", default="",
                    help="地址后缀，默认取 config.TEMPMAIL_DOMAIN")
    ap.add_argument("--mail-timeout", type=float, default=None,
                    help=f"等登录邮件的秒数（默认 {MAIL_TIMEOUT:.0f}；"
                         f"调小会把迟到但成功的邮件误判成失败）")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发账号数。默认 1=串行；>1 时每个 worker 用独立的会话与"
                         "邮箱客户端，只共享台账。站点对'短时间大量发信'的容忍度未知，"
                         "建议先小批串行试跑")
    ap.add_argument("--rotate-key", action="store_true",
                    help="允许轮换已有 key（**会立刻作废旧 key**）。默认不轮换 —— "
                         "见 src/stages.py::stage_create_key 的护栏")
    ap.add_argument("--no-skip-keyed", action="store_true",
                    help="resume 时不跳过台账里已有 api_key 的邮箱（通常要与 "
                         "--rotate-key 一起用）")
    ap.add_argument("--json", default="", help="把结果写到这个文件")
    ap.add_argument("--doctor", action="store_true", help="只做环境体检")
    args = ap.parse_args()

    if args.doctor:
        return cmd_doctor()

    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值", file=sys.stderr)
        return 1

    if args.mode == "scan":
        return cmd_scan()

    pipe = Pipeline(backend=args.mail_backend, domain=args.domain or None,
                    rotate_key=args.rotate_key)
    kw = {}
    if args.mail_timeout is not None:
        kw["mail_timeout"] = args.mail_timeout

    if args.mode == "resume":
        if not args.email:
            print("✗ mode=resume 需要至少一个 --email", file=sys.stderr)
            return 1
        recs = pipe.resume(args.email, concurrency=args.concurrency,
                           skip_keyed=not args.no_skip_keyed,
                           rotate=args.rotate_key, **kw)
    else:
        recs = pipe.run_batch(count=args.count, concurrency=args.concurrency,
                              rotate=args.rotate_key, **kw)

    return report(recs, json_path=args.json)


if __name__ == "__main__":
    sys.exit(main())
