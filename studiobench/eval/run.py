#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给一次运行打分（框架文档 §4）。

    python3 eval_run.py --run <run_dir>                          # 题目从 result.json 里找
    python3 eval_run.py --run <run_dir> --task t.json --no-vlm --no-vbench --no-asr

产出 <run_dir>/eval/eval.json；镜头切片在 eval/shots/，VLM 抽帧联系表在 eval/vlm/。
算不出来的项一律 value=None 并写明原因，不拿别的项顶。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from ..harness import taskspec
from ..tools import media
from . import hard, lineage, score, vbench, vlm, wer

NOT_IMPLEMENTED = {
    "lip_sync": "口型同步：MSAVBench lip sync 无本地实现",
    "sfx_sync": "音效同步：MSAVBench audio-visual sync 无本地实现",
    "beat_sync": "卡点：切镜是否落在节拍，无本地实现",
    "cross_shot_identity": "跨段角色：ArcFace 权重未落地",
}


def _scene_cuts(video: Path, threshold: float = 0.3) -> List[float]:
    log = media.run(["-i", str(video), "-vf", "select='gt(scene,%s)',showinfo" % threshold, "-an", "-f", "null", "-"])
    return [round(float(t), 3) for t in re.findall(r"pts_time:([\d.]+)", log)]


def _cut_shots(final: Path, shots: List[Dict[str, Any]], out_dir: Path) -> List[Path]:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    files = []
    for k, s in enumerate(shots, start=1):
        path = out_dir / ("shot_%02d.mp4" % k)
        media.run(["-y", "-ss", "%.3f" % s["start"], "-i", str(final), "-t", "%.3f" % (s["end"] - s["start"]),
                   "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(path)])
        files.append(path)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--task")
    ap.add_argument("--no-vlm", action="store_true")
    ap.add_argument("--no-vbench", action="store_true")
    ap.add_argument("--no-asr", action="store_true")
    args = ap.parse_args()

    run = Path(args.run).resolve()
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    task = taskspec.load(Path(args.task or result["task_file"]))
    work = run / "work"
    final = run / "final.mp4"
    out_dir = run / "eval"
    out_dir.mkdir(exist_ok=True)
    submitted = final.is_file() and final.stat().st_size > 0

    report: Dict[str, Any] = {"run": str(run), "task": task["id"], "model": result.get("model"),
                              "valid_for_bench": result.get("valid_for_bench"),
                              "invalid_reasons": result.get("invalid_reasons"),
                              "task_issues": [{"level": lv, "msg": m} for lv, m in taskspec.validate(task)],
                              "submitted": submitted, "not_implemented": NOT_IMPLEMENTED}
    if not submitted:
        report["scores"] = score.main_scores(False, {}, {}, {})
        report["reason"] = "没有 export：四项主分记 0"
        (out_dir / "eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["scores"], ensure_ascii=False))
        return 0

    report["spec"] = hard.spec(final, task)

    # ---- 切镜头：优先按调用血缘，追不出来就退回场景切换检测
    trace = lineage.trace_final(work, lineage.load_calls(work))
    if trace.get("ok"):
        shots, method = trace["shots"], "lineage"
    else:
        cuts = _scene_cuts(final)
        bounds = [0.0] + cuts + [report["spec"]["duration_s"] or 0.0]
        shots = [{"start": a, "end": b, "source": None, "tool": None} for a, b in zip(bounds, bounds[1:]) if b - a > 0.2]
        method = "scene_detect"
    shot_files = _cut_shots(final, shots, out_dir / "shots")
    report["lineage"] = {"method": method, "shots": shots, "mixes": trace.get("mixes", []),
                         "unresolved": trace.get("unresolved", [])}

    # ---- §4.1
    report["hard"] = {"temporal_flickering": hard.temporal_flickering(shot_files),
                      "vocal_unmasked": hard.vocal_unmasked(final, trace.get("mixes", []), work)}

    # ---- §4.2 / §4.3
    report["vbench"] = ({d: {"value": None, "reason": "--no-vbench"} for d in vbench.DIMS} if args.no_vbench
                        else vbench.run(out_dir / "shots", out_dir / "vbench"))
    report["vbench2"] = vbench.vbench2_placeholders(task)

    # ---- §4.4 人声可懂
    intel = {"value": None, "reason": "--no-asr"} if args.no_asr else \
        wer.intelligibility(final, task, trace.get("tts_texts", []))
    transcript = "\n".join("[%s–%s] %s" % (c.get("start"), c.get("end"), c.get("text"))
                           for c in intel.get("chunks") or [])
    report["audio"] = {"intelligibility": {k: v for k, v in intel.items() if k != "chunks"}}

    # ---- §4.5 VLM 是非题
    storyboard = (run / "script.md").read_text(encoding="utf-8") if (run / "script.md").exists() else ""
    refs = [Path(p) for p in (task.get("assets") or []) + (task.get("references") or [])
            if media.is_image(Path(p)) and Path(p).is_file()]
    if args.no_vlm:
        items = vlm.build_items(task, storyboard, bool(transcript))
        for it in items:
            it.update({"answer": None, "reason": "--no-vlm"})
        report["vlm"] = {"configured": False, "items": items}
    else:
        report["vlm"] = vlm.judge(task, final, out_dir / "vlm", transcript=transcript,
                                  storyboard=storyboard, assets=refs)

    # ---- 汇总
    report["film"] = score.film_score(report["vlm"]["items"], report["spec"].get("duration_in_range"))
    metrics = {"intelligibility": intel.get("value"),
               "vocal_unmasked": report["hard"]["vocal_unmasked"].get("value"),
               "lip_sync": None, "sfx_sync": None, "beat_sync": None}
    report["audio"].update(score.audio_scores(task, metrics))
    report["scores"] = score.main_scores(True, report["film"], report["vbench"], report["audio"])
    report["vlm_extra"] = {it["kind"]: it.get("answer") for it in report["vlm"]["items"] if not it.get("scored")}

    (out_dir / "eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scores": report["scores"], "valid_for_bench": report["valid_for_bench"],
                      "film": report["film"], "spec": report["spec"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
