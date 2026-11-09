# -*- coding: utf-8 -*-
"""各工具实例的资源规格，以及规划用的需求权重默认值。

显存、耗时来自 deploy/README.md 在 H20 96GB 上的实测：
  H3 常驻每卡 52GB、峰值 59–61GB，不能和别的模型同卡；TP 固定 2，实例 2 / 4 / 8 卡（USP = 卡数 / 2）。
  qwen-image t2i 不开 offload 约 58GB、image_edit 约 60GB（峰值到 97GB 的记录是和 ref2va 同卡时开 offload 的情况）；
  mmaudio sfx 峰值 12.5GB、ACE-Step music 18.6GB、LatentSync 21.7GB、Qwen3-TTS 常驻约 8GB、whisper large-v3 约 6GB。
需求权重（GPU·秒占比）由 profile.py 从 hlabel_v2 + demo1024 的调用日志算出，见 DEFAULT_DEMAND。
"""
from __future__ import annotations

from typing import Dict, List

VRAM_GB = 96.0                  # 每卡显存（H20）
VRAM_RESERVE_GB = 4.0           # 每卡留余量：CUDA 上下文、碎片

# 池名 → 规格。kind=h3：vllm-omni 服务；kind=worker：workerd 辅助模型服务
POOLS: Dict[str, Dict] = {
    "h3_fl2va":  {"kind": "h3", "variant": "fl2va", "tools": ["t2v", "i2v"], "sizes": [8, 4, 2]},
    "h3_ref2va": {"kind": "h3", "variant": "ref2va", "tools": ["ref_video"], "sizes": [8, 4, 2]},
    "image_edit": {"kind": "worker", "tools": ["image_edit"], "vram": 62.0},
    "t2i":       {"kind": "worker", "tools": ["t2i"], "vram": 58.0},
    "sfx":       {"kind": "worker", "tools": ["sfx"], "vram": 14.0},
    "tts":       {"kind": "worker", "tools": ["tts"], "vram": 10.0},
    "music":     {"kind": "worker", "tools": ["music"], "vram": 20.0},
    "lipsync":   {"kind": "worker", "tools": ["lipsync"], "vram": 23.0},
    "asr":       {"kind": "worker", "tools": ["asr"], "vram": 7.0},
}
TOOL_POOL: Dict[str, str] = {t: name for name, spec in POOLS.items() for t in spec["tools"]}
WORKER_POOLS: List[str] = [n for n, s in POOLS.items() if s["kind"] == "worker"]

# 需求权重：各池占总 GPU·秒的比例（H3 按实例卡数折算，辅助模型按显存占一张卡的比例折算）。
# 由 `sb pool profile` 从 runs/hlabel_v2 + runs/demo1024 算出（2026-10-29），可用 --weights 换成新统计。
DEFAULT_DEMAND: Dict[str, float] = {
    "h3_fl2va": 0.870, "image_edit": 0.0718, "t2i": 0.0345, "h3_ref2va": 0.013, "sfx": 0.005,
    "tts": 0.0025, "music": 0.0022, "lipsync": 0.0007, "asr": 0.0003,
}
# 显存放不下时，t2i / image_edit 可以退到 CPU offload（STUDIO_IMAGE_OFFLOAD=model）：常驻少、每张慢一些
OFFLOAD_VRAM: Dict[str, float] = {"t2i": 42.0, "image_edit": 44.0}

# 起服务用的端口段
PORT_GATEWAY = 30100
PORT_H3 = {"fl2va": 30020, "ref2va": 30040}     # 第 k 个实例用 base + k（第一个 fl2va 仍是 30020，兼容旧配置）
PORT_WORKER = 30200
