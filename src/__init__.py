"""Token Juice 端到端注册链路（发链接 → 验证激活 → 建 API Key → 验活 → 入库）。

模块分层（依赖方向单向：tools → runner → stages → 叶子 → config，无循环）：

    叶子（互不依赖，只依赖 config）
      config.py      常量集中地 + .env 加载 + 启动校验
      parsing.py     纯解析：从邮件正文取登录 token（零第三方依赖）
      mailrules.py   收件过滤规则表（唯一零内部依赖的模块）
      apicheck.py    推理网关验活（GET /v1/models + POST /v1/chat/completions）
      ledger.py      JSONL 台账（并集合并 / 幂等 / 升级语义）
      tempemail.py   CF Temp Email Worker 客户端
      tokenjuice.py  控制面客户端（send-magic-link / verify / api-key）

    聚合（两层，方向仍是单向）
      stages.py      单账号阶段实现（`StageMixin`）+ `AccountRecord`
      runner.py      `Pipeline`：批量 / 并发 / 台账写入

`runner` 继承 `stages.StageMixin`，`stages` **不**反向引用 `runner`。

入口在 `tools/`（`run_e2e.py` 主入口、`verify_keys.py` 验收、
`selftest.py` 离线自测）。
"""

# ⚠️ 这里**不写 `__all__`**：列出的子模块并未在本文件 import，
# 静态检查会报 `reportUnsupportedDunderAll`（"specified in __all__ but is not
# present in module"）。而为了让 `__all__` 成立去 `from . import runner, stages…`
# 会让 `import src` 顺带拉起 `requests` —— 一个只想要 `src.config` 的脚本
# 不该被迫加载整条 HTTP 栈。调用方一律显式 `from src import <子模块>`。
