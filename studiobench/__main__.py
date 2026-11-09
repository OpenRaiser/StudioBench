# -*- coding: utf-8 -*-
"""python -m studiobench <命令> [参数]（仓库根目录下也可以用 ./sb <命令>）

推理阶段：
  preflight     检查本机每个工具的后端是否就绪
  selftest      经 MCP 把 18 个工具各真调一次
  build-tasks   从数据集生成题目输入（8 片种 × 5 任务）
  run           跑一道题（隔离的 Claude Code 会话 + 18 个原子工具）
  batch         按 8×5 格子批量跑、续跑、汇总
  demo          用 mock 生成后端跑一条脚本化的端到端样例
  mcp-server    MCP stdio server（由 run 生成的 mcp.json 拉起，一般不用手动跑）
  pool          工具池：plan（按卡数规划）/ up / status / down / profile（见 studiobench/pool/launch.py）
评测（可选，后续阶段）：
  eval          给一次运行打分（框架文档 §4）
  aggregate     汇总多次运行的分数
  leaderboard   把 eval 快照汇总成静态排行榜
  monitor       运行树 Web 监控面板
  explorer      生成静态案例浏览器
"""
from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "preflight": "studiobench.harness.preflight",
    "selftest": "studiobench.harness.selftest",
    "build-tasks": "studiobench.harness.build_tasks",
    "run": "studiobench.harness.run",
    "batch": "studiobench.harness.batch",
    "mcp-server": "studiobench.tools.server",
    "pool": "studiobench.pool.launch",
    "eval": "studiobench.eval.run",
    "aggregate": "studiobench.eval.aggregate",
    "leaderboard": "studiobench.leaderboard.build",
    "monitor": "studiobench.monitor.server",
    "explorer": "studiobench.explorer.build",
    "demo": "studiobench.harness.demo",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 0 if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help") else 2
    cmd = sys.argv[1]
    module = importlib.import_module(COMMANDS[cmd])
    sys.argv = ["studiobench %s" % cmd] + sys.argv[2:]
    return int(module.main() or 0)


if __name__ == "__main__":
    sys.exit(main())
