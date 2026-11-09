# -*- coding: utf-8 -*-
"""从历次运行的调用日志统计各工具池的 GPU·秒需求占比，作为规划器的权重。

    python -m studiobench pool profile [--runs 目录 ...] [--h3-gpus 4]

H3 调用按「耗时 × 当时实例卡数」折算（默认 4 卡，即 8gpu-1node 的 fl2va）；辅助模型按「耗时 × 显存 / 每卡显存」折算。
失败的调用也算：GPU 时间照样花了。工具池版本的日志有租用时间窗（t_lease / t_release），只算实例真正干活的时间；
老日志只有 sec（含排队），H3 争用严重时会高估 H3 的份额。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List

from .catalog import POOLS, TOOL_POOL, VRAM_GB

DEFAULT_RUNS = [p for p in os.environ.get("STUDIO_PROFILE_RUNS", "").split(os.pathsep) if p]


def call_logs(roots: Iterable[str]) -> List[Path]:
    out: List[Path] = []
    for root in roots:
        out += sorted(Path(root).glob("*/*/work/.calls.jsonl"))
    return out


def demand(roots: Iterable[str], h3_gpus: float = 4.0) -> Dict[str, Dict[str, float]]:
    """池名 → {calls, seconds, gpu_seconds, share}。"""
    stat: Dict[str, Dict[str, float]] = defaultdict(lambda: {"calls": 0, "seconds": 0.0, "gpu_seconds": 0.0})
    for log in call_logs(roots):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            pool = TOOL_POOL.get(str(c.get("tool")))
            if not pool or c.get("rejected") or c.get("sec") is None:
                continue
            spec = POOLS[pool]
            sec = float(c["t_release"] - c["t_lease"]) if c.get("t_lease") and c.get("t_release") else float(c["sec"])
            per = h3_gpus if spec["kind"] == "h3" else spec["vram"] / VRAM_GB
            s = stat[pool]
            s["calls"] += 1
            s["seconds"] += sec
            s["gpu_seconds"] += sec * per
    total = sum(s["gpu_seconds"] for s in stat.values()) or 1.0
    for s in stat.values():
        s["share"] = s["gpu_seconds"] / total
    return dict(stat)


def main() -> int:
    ap = argparse.ArgumentParser(description="统计各工具池的 GPU·秒需求占比")
    ap.add_argument("--runs", nargs="*", default=DEFAULT_RUNS, help="批次目录（runs/<批次>）")
    ap.add_argument("--h3-gpus", type=float, default=4.0, help="统计这批数据时 H3 实例的卡数")
    ap.add_argument("--json", help="把权重写成 JSON 文件，给 plan / up 的 --weights 用")
    args = ap.parse_args()
    stat = demand(args.runs, args.h3_gpus)
    print("%-11s %7s %10s %12s %7s" % ("池", "调用", "耗时(时)", "GPU·时", "占比"))
    for name, s in sorted(stat.items(), key=lambda kv: -kv[1]["share"]):
        print("%-11s %7d %10.1f %12.1f %6.1f%%" % (name, s["calls"], s["seconds"] / 3600, s["gpu_seconds"] / 3600,
                                                   s["share"] * 100))
    weights = {k: round(v["share"], 4) for k, v in stat.items()}
    print("\n权重：%s" % json.dumps(weights, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(weights, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0
