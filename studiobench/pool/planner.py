# -*- coding: utf-8 -*-
"""N 张卡 → 工具实例布局。

    python -m studiobench pool plan --gpus 0-15 [--lanes N] [--h3-size 4] [--ref2va auto|on|off] [--weights w.json]

规则（按需求权重分卡，权重默认见 catalog.DEFAULT_DEMAND）：
1. ref2va 用得极少但不能缺（缺了 ref_video 对模型显示为不可用，题目条件就变了）：8 卡及以上固定一个 2 卡实例。
2. 辅助模型（生图、改图、音频、听写）先按「每种至少一个实例」装箱，算出最少要几张卡；显存不够时 t2i / image_edit 退到 offload。
3. 剩下的卡按 fl2va 的需求份额给 H3，取偶数；实例默认 4 卡（TP2×USP2），凑不满 4 的余数开一个 2 卡实例。
   H3 每段要的卡·分钟基本固定（8 卡约 3.5 分钟，1 卡约 30 分钟），实例拆大拆小总吞吐差不多：4 卡是单段延迟和
   同时能接几段之间的折中，也让一个实例挂了不至于全停。
4. 再多出来的卡给辅助模型加副本：按「需求 / 现有实例数」挑最缺的池，能装下就加。
5. 同时跑几道题（lane）默认 = fl2va 实例数 + 1：每道题的生成调用成串出现，中间有模型思考的空档，多一道题把空档填上。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .catalog import (DEFAULT_DEMAND, OFFLOAD_VRAM, POOLS, PORT_GATEWAY, PORT_H3, PORT_WORKER, VRAM_GB,
                      VRAM_RESERVE_GB, WORKER_POOLS)


class PlanError(ValueError):
    pass


def parse_gpus(spec: str) -> List[int]:
    """'0-7' / '0,1,2,3' / '8'（= 0..7）→ 卡号列表。"""
    spec = str(spec).strip()
    if spec.isdigit() and "," not in spec and "-" not in spec:
        return list(range(int(spec)))
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    if len(set(out)) != len(out):
        raise PlanError("卡号有重复：%s" % spec)
    return out


def _pack(items: List[Tuple[str, float]], ncards: int, cap: float) -> Optional[List[List[Tuple[str, float]]]]:
    """first-fit decreasing：把 (池, 显存) 装进 ncards 张卡，装不下返回 None。"""
    cards: List[List[Tuple[str, float]]] = [[] for _ in range(ncards)]
    for name, v in sorted(items, key=lambda x: -x[1]):
        best = None
        for k, c in enumerate(cards):
            free = cap - sum(x[1] for x in c)
            if free >= v and (best is None or free < cap - sum(x[1] for x in cards[best])):
                best = k                    # best fit：放进剩余最少但装得下的卡
        if best is None:
            return None
        cards[best].append((name, v))
    return cards


def _aux_items(counts: Dict[str, int], offload: Dict[str, bool]) -> List[Tuple[str, float]]:
    items = []
    for pool, n in counts.items():
        v = OFFLOAD_VRAM[pool] if offload.get(pool) else POOLS[pool]["vram"]
        items += [(pool, v)] * n
    return items


def _fit_aux(ncards: int, counts: Dict[str, int], cap: float):
    """先不开 offload 装；装不下依次让 t2i、image_edit 退到 offload。返回 (cards, offload) 或 None。"""
    for offload in ({}, {"t2i": True}, {"t2i": True, "image_edit": True}):
        cards = _pack(_aux_items(counts, offload), ncards, cap)
        if cards is not None:
            return cards, dict(offload)
    return None


def plan(gpus: List[int], vram: float = VRAM_GB, weights: Optional[Dict[str, float]] = None,
         lanes: Optional[int] = None, h3_size: Optional[int] = None, ref2va: str = "auto") -> Dict[str, Any]:
    w = dict(DEFAULT_DEMAND)
    w.update(weights or {})
    n = len(gpus)
    cap = vram - VRAM_RESERVE_GB
    notes: List[str] = []
    if n < 4:
        raise PlanError("至少要 4 张卡（H3 一个 2 卡实例 + 辅助模型 2 张卡），现在 %d 张" % n)

    ref_gpus = 2 if (ref2va == "on" or (ref2va == "auto" and n >= 8)) else 0
    if ref_gpus == 0:
        notes.append("卡数少于 8，不起 ref2va：ref_video 对模型显示为本机不可用")

    base_counts = {p: 1 for p in WORKER_POOLS}
    aux_min = None
    for k in range(1, n):
        if _fit_aux(k, base_counts, cap) is not None:
            aux_min = k
            break
    if aux_min is None:
        raise PlanError("辅助模型每种一个实例都装不下")

    room = n - ref_gpus - aux_min                      # 能给 fl2va 的最多卡数
    if room < 2:
        raise PlanError("卡不够：ref2va %d + 辅助模型至少 %d 张后，fl2va 不到 2 张" % (ref_gpus, aux_min))
    h3_share = w["h3_fl2va"] / max(1e-9, sum(v for k, v in w.items() if k != "h3_ref2va"))
    target = (n - ref_gpus) * h3_share
    fl = max(2, min(room, int(target // 2) * 2))
    size = h3_size or (4 if fl >= 4 else 2)
    if size not in POOLS["h3_fl2va"]["sizes"]:
        raise PlanError("H3 实例只能是 %s 卡" % POOLS["h3_fl2va"]["sizes"])
    sizes = [size] * (fl // size)
    if fl % size:
        rest = fl % size
        sizes += [2] * (rest // 2)
    aux_cards = n - ref_gpus - fl

    # 多出来的辅助卡加副本：按需求 / 实例数挑最缺的
    counts = dict(base_counts)
    fit = _fit_aux(aux_cards, counts, cap)
    assert fit is not None
    while True:
        best = None
        for pool in sorted(WORKER_POOLS, key=lambda p: -w.get(p, 0) / counts[p]):
            trial = dict(counts, **{pool: counts[pool] + 1})
            f = _fit_aux(aux_cards, trial, cap)
            if f is not None and (not f[1] or len(f[1]) <= len(fit[1])):   # 加副本不能逼着别的模型退到 offload
                best = (pool, trial, f)
                break
        if best is None or w.get(best[0], 0) / best[1][best[0]] < 0.004:      # 需求太小的不值得占显存
            break
        counts, fit = best[1], best[2]
    cards, offload = fit
    for p in offload:
        notes.append("%s 显存装不下，开 CPU offload（每张慢一些）" % p)

    # ---- 编号：卡号、端口
    free = list(gpus)
    h3: List[Dict[str, Any]] = []
    for k, s in enumerate(sizes):
        g, free = free[:s], free[s:]
        h3.append({"pool": "h3_fl2va", "name": "fl2va-%d" % k, "variant": "fl2va", "gpus": g, "tp": 2,
                   "usp": s // 2, "port": PORT_H3["fl2va"] + k})
    if ref_gpus:
        g, free = free[:ref_gpus], free[ref_gpus:]
        h3.append({"pool": "h3_ref2va", "name": "ref2va-0", "variant": "ref2va", "gpus": g, "tp": 2,
                   "usp": ref_gpus // 2, "port": PORT_H3["ref2va"]})
    workers: List[Dict[str, Any]] = []
    seq: Dict[str, int] = {}
    port = PORT_WORKER
    for gpu, card in zip(free, cards):
        for pool, v in card:
            k = seq.get(pool, 0)
            seq[pool] = k + 1
            workers.append({"pool": pool, "name": "%s-%d" % (pool, k), "gpu": gpu, "port": port, "vram": v,
                            "offload": bool(offload.get(pool))})
            port += 1
    nfl = sum(1 for x in h3 if x["pool"] == "h3_fl2va")
    lanes = lanes or nfl + 1
    return {"gpus": gpus, "vram": vram, "lanes": lanes, "h3": h3, "workers": workers,
            "gateway_port": PORT_GATEWAY, "weights": w, "notes": notes}


def describe(layout: Dict[str, Any]) -> str:
    lines = ["%d 张卡（每卡 %gGB），同时跑 %d 道题" % (len(layout["gpus"]), layout["vram"], layout["lanes"]), ""]
    lines.append("%-6s %s" % ("卡", "实例"))
    by_gpu: Dict[int, List[str]] = {g: [] for g in layout["gpus"]}
    for x in layout["h3"]:
        for g in x["gpus"]:
            by_gpu[g].append("%s（TP%d×USP%d，:%d）" % (x["name"], x["tp"], x["usp"], x["port"]))
    for x in layout["workers"]:
        by_gpu[x["gpu"]].append("%s %gGB%s :%d" % (x["name"], x["vram"], "（offload）" if x["offload"] else "",
                                                  x["port"]))
    for g in layout["gpus"]:
        used = sum(x["vram"] for x in layout["workers"] if x["gpu"] == g)
        load = "  [%g/%gGB]" % (used, layout["vram"]) if used else ""
        lines.append("%-6s %s%s" % (g, "、".join(by_gpu[g]) or "（空）", load))
    lines.append("")
    pools: Dict[str, int] = {}
    for x in layout["h3"] + layout["workers"]:
        pools[x["pool"]] = pools.get(x["pool"], 0) + 1
    lines.append("每个池的实例数：" + "，".join("%s ×%d" % kv for kv in pools.items()))
    for note in layout["notes"]:
        lines.append("注意：" + note)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="按卡数规划工具实例布局（不起服务）")
    ap.add_argument("--gpus", required=True, help="卡号：0-15、0,1,2,3，或卡数 8")
    ap.add_argument("--vram", type=float, default=VRAM_GB)
    ap.add_argument("--lanes", type=int)
    ap.add_argument("--h3-size", type=int, choices=(2, 4, 8))
    ap.add_argument("--ref2va", choices=("auto", "on", "off"), default="auto")
    ap.add_argument("--weights", help="profile --json 写出的权重文件")
    ap.add_argument("--json", help="把布局写成 JSON")
    args = ap.parse_args(argv)
    weights = json.loads(Path(args.weights).read_text(encoding="utf-8")) if args.weights else None
    try:
        layout = plan(parse_gpus(args.gpus), args.vram, weights, args.lanes, args.h3_size, args.ref2va)
    except PlanError as exc:
        print("规划失败：%s" % exc)
        return 2
    print(describe(layout))
    if args.json:
        Path(args.json).write_text(json.dumps(layout, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0
