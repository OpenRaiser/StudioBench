# -*- coding: utf-8 -*-
"""§4.2 VBench 六项（custom_input），§4.3 VBench-2.0 可复用项。

VBench：VBENCH_ROOT 指向含 evaluate.py 的目录（文档里是 DSL/Benchs/VBench），VBENCH_PYTHON 是它的环境。
先切成单段再评（evaluate.py 对目录里的视频取平均，就是对段平均）。每个维度单独跑，
某项权重没准备好就空着，不用别的项填。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..tools.backends.common import env, env_int, run_subprocess

DIMS = ("subject_consistency", "background_consistency", "motion_smoothness",
        "dynamic_degree", "aesthetic_quality", "imaging_quality")
VBENCH2 = ("Human_Anatomy", "Human_Identity", "Human_Clothes", "Multi-View_Consistency")


def configured() -> bool:
    root = env("VBENCH_ROOT")
    return bool(root and (Path(root) / "evaluate.py").is_file() and env("VBENCH_PYTHON"))


def run(shots_dir: Path, out_dir: Path) -> Dict[str, Dict[str, Any]]:
    if not configured():
        reason = "未配置 VBENCH_ROOT（含 evaluate.py）/ VBENCH_PYTHON"
        return {d: {"value": None, "reason": reason} for d in DIMS}
    root = Path(env("VBENCH_ROOT"))
    rows: Dict[str, Dict[str, Any]] = {}
    for dim in DIMS:
        dim_out = out_dir / dim
        dim_out.mkdir(parents=True, exist_ok=True)
        try:
            run_subprocess([env("VBENCH_PYTHON"), str(root / "evaluate.py"), "--dimension", dim,
                            "--videos_path", str(shots_dir), "--mode=custom_input", "--output_path", str(dim_out)],
                           timeout=env_int("VBENCH_TIMEOUT", 3600), cwd=root)
            found = sorted(dim_out.glob("*_eval_results.json"), key=lambda p: p.stat().st_mtime)
            data = json.loads(found[-1].read_text(encoding="utf-8"))
            rows[dim] = {"value": float(data[dim][0])}
        except Exception as exc:  # noqa: BLE001
            rows[dim] = {"value": None, "reason": str(exc)[-300:]}
    return rows


def vbench2_placeholders(task: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """文档 §4.3：人出镜时测 Human_Anatomy / Human_Clothes；Human_Identity 等 ArcFace 权重；
    Multi-View_Consistency 只在题目标了 physics 时加测。权重都还没落地，先空着并写明原因。"""
    rows = {d: {"value": None, "reason": "VBench-2.0 权重未落地，未接入"} for d in VBENCH2}
    if "physics" not in (task.get("tags") or []):
        rows["Multi-View_Consistency"]["reason"] = "题目没标 physics，不测"
    return rows
