# -*- coding: utf-8 -*-
"""Scripted end-to-end demo with mock generation backends.

    python -m studiobench demo --task tasks/demo/DEMO-TINY.json --out runs/_demo

Runs a fixed, deterministic "agent" (no LLM) through the real tool implementations: two
generated shots, concat, voice-over, music, mix, subtitles, export, then the hard-gate
evaluation. The result is marked ``valid_for_bench=false`` because generation used mock
backends; everything else (editing, probing, scoring) is real.

Use it to verify a machine end-to-end, to produce a demo site, or as a contract test when
changing tools.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from ..tools import media, registry
from . import taskspec

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TASK = REPO / "tasks" / "demo" / "DEMO-TINY.json"


class Script:
    def __init__(self, work: Path):
        self.work = work
        self.calls: List[Dict[str, Any]] = []

    def call(self, tool: str, **args: Any) -> Dict[str, Any]:
        fn = registry.TOOLS[tool]["fn"]
        started = time.time()
        ok, error = True, None
        try:
            out = fn(**args)
            ok, error = True, None
        except Exception as exc:  # noqa: BLE001
            out, ok, error = {}, False, str(exc)
            raise
        finally:
            self.calls.append({"i": len(self.calls) + 1, "tool": tool, "args": args,
                               "ok": ok, "error": error, "sec": round(time.time() - started, 2),
                               "t": started})
        return out


def run_demo(task_path: Path, out_root: Path, model: str = "mock-agent") -> Path:
    task = taskspec.load(task_path)
    run_dir = out_root / task["id"] / model
    work = run_dir / "work"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    work.mkdir(parents=True)
    taskspec.copy_inputs(task, work)
    registry.set_workdir(work)
    os.environ["STUDIO_BACKEND"] = "mock"          # explicit: mock generation only
    script = Script(work)

    shot_a = script.call("t2i", prompt="清晨窗边，手冲咖啡特写，暖色阳光，浅景深", out="shot_a.png")
    clip_a = script.call("i2v", first_frame=shot_a["file"], prompt="镜头缓慢推近咖啡杯，热气升起",
                         duration=5, out="clip_a.mp4")
    shot_b = script.call("t2i", prompt="咖啡杯口热气特写，逆光，品牌感", out="shot_b.png")
    clip_b = script.call("i2v", first_frame=shot_b["file"], prompt="热气在阳光里飘动，缓慢拉远",
                         duration=5, out="clip_b.mp4")
    cut = script.call("concat", videos=[clip_a["file"], clip_b["file"]], out="cut.mp4")
    voice = script.call("tts", text="一杯咖啡，唤醒来时的路。", out="vo.wav")
    music = script.call("music", prompt="轻快的原声吉他，温暖，明亮", duration=12, out="music.wav")
    mixed = script.call("mix_audio", video=cut["file"], audio=voice["file"], out="mixed_vo.mp4")
    mixed2 = script.call("mix_audio", video=mixed["file"], audio=music["file"], gain=0.35,
                         out="mixed_all.mp4")
    final_work = script.call("overlay", video=mixed2["file"],
                             subtitles=[{"start": 0.5, "end": 3.5, "text": "一杯咖啡"}],
                             out="final_work.mp4")
    script.call("export", src=final_work["file"])

    final = run_dir / "final.mp4"
    submitted = (work / "final.mp4").is_file()
    if submitted:                                  # same hand-off as run.py: work/final.mp4 -> run/final.mp4
        shutil.copy2(work / "final.mp4", final)
    probe = media.probe(final) if final.exists() else {}
    result = {
        "task": task["id"], "task_file": str(Path(task_path).resolve()), "model": model,
        "mock": True, "submitted": submitted, "final": str(final) if submitted else None,
        "video": probe, "returncode": 0, "timed_out": False,
        "seconds": round(sum(c["sec"] for c in script.calls), 2),
        "tool_calls": len(script.calls), "tool_errors": 0,
        "valid_for_bench": False,
        "invalid_reasons": ["mock backends (demo run)"],
        "demo": True, "created_at": time.time(),
    }
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    with open(run_dir / "work" / ".calls.jsonl", "w", encoding="utf-8") as f:
        for call in script.calls:
            f.write(json.dumps(call, ensure_ascii=False) + "\n")
    (run_dir / "prompt.txt").write_text(task["wish"], encoding="utf-8")

    # Hard-gate evaluation (no VLM / VBench / ASR required).
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    subprocess.run([sys.executable, "-m", "studiobench", "eval", "--run", str(run_dir),
                    "--task", str(task_path), "--no-vlm", "--no-vbench", "--no-asr"],
                   cwd=str(REPO), env=env, check=False)
    return run_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Scripted mock end-to-end demo")
    ap.add_argument("--task", default=str(DEFAULT_TASK))
    ap.add_argument("--out", default=os.environ.get("STUDIO_RUNS_DIR", str(Path.cwd() / "runs")))
    ap.add_argument("--model", default="mock-agent")
    args = ap.parse_args()

    run_dir = run_demo(Path(args.task), Path(args.out), args.model)
    print("demo run: %s" % run_dir)
    for name in ("final.mp4", "result.json", "eval/eval.json"):
        path = run_dir / name
        print("  %-16s %s" % (name, "ok" if path.exists() else "MISSING"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
