# -*- coding: utf-8 -*-
"""工具池：按给定的 GPU 数自动规划工具实例配比，起服务，由节点网关把调用分给空闲实例。

  catalog   每种工具实例占多少卡 / 显存，需求权重的默认值
  profile   从历次运行的 .calls.jsonl 统计各工具的 GPU·秒需求
  planner   N 张卡 → 布局（H3 实例、辅助模型实例、同时跑几道题）
  gateway   节点网关：实例登记、租约分配（多道题之间轮转）、健康检查
  workerd   辅助模型服务（t2i / image_edit / asr / tts / sfx / music / lipsync），一进程一工具一张卡
  client    MCP server 一侧：租实例、远程调用
  launch    sb pool plan|up|status|down
"""
