"""JSONL 台账：追加写、按唯一键并集合并、幂等、可补录。

四条硬规矩（沿用参考项目踩过的坑）：

1. **并集**合并，不「比大小/比字段数」—— 启发式总有相等或边界的死角。
2. **能读自己的输出** —— 导出文件很容易变成唯一副本，重跑不能缩水。
3. 补录按事件真实发生时间入账，不是补录那一刻。
4. 🔴 **状态等级表的词汇必须与写入方实际写的词汇一致。**
   否则等级判断静默失效（详见 `RANK` 上方说明）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

#: **可重入**锁：`upsert_many` 的「读-改-写」必须整体原子，而 `_rewrite` 自己也取锁。
_LOCK = threading.RLock()

#: 记录状态优先级：只用于「升级 / 降级」判断，不用来决定「是否写入」。
#:
#: 🔴 **本表的键必须覆盖 `src/stages.py` 实际写入的每一个 status 字面量。**
#: 历史教训（参考项目）：等级表照搬兄弟项目口径（success/skipped），而本项目
#: 写的是 keyed/partial/… ⇒ 全部并列 0 分 ⇒ `keyed`（已拿到 key）与 `failed`
#: 同级 ⇒ 走「同级并集、新值胜出」⇒ **一次重跑失败就把 api_key 覆盖成空串**，
#: 而验收脚本按「api_key 有没有值」筛清单 ⇒ 这些账号**从交付物里静默消失**。
#:
#: 现在由 `tools/tests/test_ledger.py::test_status_vocabulary` 用 AST 扫源码钉住：
#: 新增任何 status 字面量而没登记进本表，自测立刻失败。
RANK: dict[str, int] = {
    "keyed": 5,         # 终态成功：拿到并**验活**了 api_key
    "partial": 4,       # 会话已建立但没拿到可用 key（已有 key 明文不可取回等）
    "registered": 3,    # 会话已建立（过渡态，正常情况下会被 keyed/partial 覆盖）
    "started": 0,       # 刚建记录（与 failed 同级，避免「空壳」覆盖成功记录）
    "failed": 0,
    "": 0,
    # 兼容兄弟项目/外部文件里的旧词汇，避免「未知词汇静默落到 0 分」这个坑复发
    "success": 5,
    "skipped": 0,
}

#: 一旦赚到就**不允许被空值覆盖**的字段。
#:
#: `AccountRecord.to_dict()` 无条件输出这些键，失败时是空串；并集合并的
#: 「新值胜出」会把已经拿到的凭据清掉 —— 只修 `RANK` 挡不住「同级覆盖」。
EARNED_FIELDS: tuple[str, ...] = (
    "api_key", "api_key_suffix", "api_key_created_at", "email",
)

#: 累积型字段：合并时做**并集**，不用后来者整体替换。
DICT_FIELDS: tuple[str, ...] = ("stages", "timings", "login", "user")


def rank(rec: dict[str, Any]) -> int:
    return RANK.get(rec.get("status") or "", 0)


def merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """两条同键记录的合并规则。

    - 新记录等级更高 → 新记录胜出（但仍要保住旧记录里已赚到的字段）
    - 同级 → 并集，新值胜出，旧字段一个不丢
    - 新记录等级更低 → **整体保留旧记录**，只把这次的失败记进 `last_error`

    最后一条是关键：不这么做的话，「重跑一次失败」会把已经成功的记录打回原形，
    连凭据一起清掉。
    """
    r_old, r_new = rank(old), rank(new)

    if r_new < r_old:
        out = dict(old)
        err = str(new.get("error") or "").strip()
        if err and err != str(old.get("error") or "").strip():
            out["last_error"] = err
        return out

    out = dict(new) if r_new > r_old else {**old, **new}
    for k in DICT_FIELDS:
        a, b = old.get(k), new.get(k)
        # 🔴 只要**任意一边**是 dict 就并集：`AccountRecord.to_dict()` 总是输出
        # 全部 dict 字段（空 `{}`），但补录/外部文件可能不带这些键 ——
        # 若写成「两边都是 dict 才并集」，缺键的那条记录会把旧数据整体丢掉。
        if isinstance(a, dict) or isinstance(b, dict):
            out[k] = {**(a or {}), **(b or {})}
    for k in EARNED_FIELDS:
        if not out.get(k) and old.get(k):
            out[k] = old[k]
    return out


class Ledger:
    """JSONL 台账。append 是线程安全的（多 producer 并发实测零丢失）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 写 ────────────────────────────────────────────────────────────
    def append(self, rec: dict[str, Any]) -> None:
        # 写入边界再削一次首尾空白：`x@y.com\r` 会与 `x@y.com` 并列为两个键，
        # 台账「按邮箱去重」静默失效、交付数字虚高（参考项目实测踩过）。
        rec = dict(rec)
        for f in ("key", "email"):
            if isinstance(rec.get(f), str):
                rec[f] = rec[f].strip()
        line = json.dumps(rec, ensure_ascii=False)
        # `newline=""` —— JSONL 只写 LF。Windows 默认把 \n 翻成 \r\n，
        # 每行尾部带 CR 会让严格 JSONL 解析器报错。
        with _LOCK, self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")

    def upsert_many(self, records: Iterable[dict[str, Any]]) -> dict[str, int]:
        """把一批记录并入台账（按 `key` 字段去重合并）。

        返回 {"added", "updated", "kept"} —— 增量写回必须能自证「真的写进去了」。
        整个「读-改-写」在锁内完成（`_rewrite` 是整文件替换）。
        """
        with _LOCK:
            incoming = [r for r in records if r.get("key")]
            current = self.load()
            by_key: dict[str, dict[str, Any]] = {r["key"]: r for r in current}

            added = updated = kept = 0
            for rec in incoming:
                k = rec["key"]
                if k not in by_key:
                    by_key[k] = rec
                    added += 1
                else:
                    merged = merge(by_key[k], rec)
                    if merged != by_key[k]:
                        by_key[k] = merged
                        updated += 1
                    else:
                        kept += 1

            order = [r["key"] for r in current]
            for r in incoming:
                if r["key"] not in order:
                    order.append(r["key"])

            self._rewrite([by_key[k] for k in order if k in by_key])
            return {"added": added, "updated": updated, "kept": kept}

    def _rewrite(self, records: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with _LOCK, tmp.open("w", encoding="utf-8", newline="") as fh:
            for r in records:
                r = dict(r)
                for f in ("key", "email"):
                    if isinstance(r.get(f), str):
                        r[f] = r[f].strip()
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        with _LOCK:
            tmp.replace(self.path)

    # ── 读 ───────────────────────────────────────────────────────────
    def load(self) -> list[dict[str, Any]]:
        """读台账：同键后写覆盖前写，并按等级升级 / 降级保护。持锁读，避免半行。"""
        with _LOCK:
            by_key: dict[str, dict[str, Any]] = {}
            for rec in self._iter_raw():
                k = rec.get("key")
                if not k:
                    continue
                by_key[k] = merge(by_key[k], rec) if k in by_key else rec
            return list(by_key.values())

    def raw_rows(self) -> list[dict[str, Any]]:
        """**不做合并**，按文件顺序返回每一行。

        为什么需要：`load()` 按 key（邮箱）去重，同一账号的**第二把** key 会被
        吃掉。⚠️ Token Juice 侧更严重：建新 key 会让旧 key **失效**（轮换），
        所以「第二把」不是「两把都有效」而是「第一把已死」——
        见 `tools/verify_keys.py` 与 `stages.stage_create_key` 的轮换护栏。
        """
        with _LOCK:
            return list(self._iter_raw())

    def _iter_raw(self) -> Iterator[dict[str, Any]]:
        """逐行读，解析失败的行**静默跳过**（不炸整次读）。"""
        if not self.path.is_file():
            return
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue
