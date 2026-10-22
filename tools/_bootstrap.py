"""把仓库根加进 `sys.path`，让 `tools/` 任意深度的脚本都能 `import src.*`。

按**标记文件**定位仓库根，跟脚本自身深度无关（脚本移进子目录也不用改）。
用法（放在其他 import 之前）：

    from _bootstrap import ROOT      # noqa: F401  （有副作用：改 sys.path）
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

#: 仓库根的标记文件（同时存在才认定是根，避免误判到子目录）
_MARKERS = ("src", ".gitignore")


def find_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__).resolve().parent).resolve()
    for cand in (here, *here.parents):
        if all((cand / m).exists() for m in _MARKERS):
            return cand
    raise RuntimeError(f"向上找不到仓库根（需要同时含 {_MARKERS}），起点 {here}")


ROOT = find_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 控制台编码 ────────────────────────────────────────────────────────
# Windows 默认用 GBK（cp936）编码 stdout，日志里的 `✓` / `⚠` 会直接抛
# UnicodeEncodeError（在打印阶段崩掉，看起来像测试失败）。统一改成 UTF-8：
# `errors="replace"` 保证即使终端不支持也不会把整条日志打断。
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if callable(_reconf):
        with contextlib.suppress(ValueError, OSError):
            _reconf(encoding="utf-8", errors="replace")
