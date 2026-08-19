"""PyInstaller 入口：把 Harness 的 stdio JSONL 服务冻结为独立 exe。

仅用于打包；开发模式仍用 `python -m quant_agent_harness.server`。
"""

from quant_agent_harness.server import main

raise SystemExit(main())
