# -*- coding: utf-8 -*-
"""sfx：MMAudio，按镜头视频 + 文本出音效。直接调官方 `demo.py --video --prompt --duration`（文档 §3）。

demo.py 用相对路径找权重（./weights、./ext_weights），这里在临时目录里软链到共享盘权重再运行，
不往共享盘模型目录写任何东西。时长默认等于输入视频时长（旧实现固定 5 秒，会把 8 秒镜头截短）。

运行时还会从 HF hub 拉两份权重：apple/DFN5B-CLIP-ViT-H-14-384、nvidia/bigvgan_v2_44khz_128band_512x。
GPU 机没外网时先下载到 STUDIO_HF_CACHE（见 RUNBOOK_GPU.md）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import media

from .common import Unavailable, check_paths, env, env_int, run_subprocess, under_models, under_share

NAME = "mmaudio"
HF_MODELS = ("apple/DFN5B-CLIP-ViT-H-14-384", "nvidia/bigvgan_v2_44khz_128band_512x")


def config() -> Dict[str, str]:
    return {
        "python": env("STUDIO_MMAUDIO_PYTHON", under_models("miniconda3/envs/hunyuan15/bin/python")),
        "repo": env("STUDIO_MMAUDIO_REPO", under_models("WSH/MMAudio")),
        "weights": env("STUDIO_MMAUDIO_WEIGHTS", under_models("WSH/Models/MMAudio")),
        "variant": env("STUDIO_MMAUDIO_VARIANT", "large_44k_v2"),
        "hf_cache": env("STUDIO_HF_CACHE", under_share("models/hf_cache")),
    }


def missing_hf_models(cache: str) -> List[str]:
    return [m for m in HF_MODELS
            if not (Path(cache) / ("models--" + m.replace("/", "--"))).is_dir()]


def available() -> Tuple[bool, str]:
    cfg = config()
    missing = check_paths(STUDIO_MMAUDIO_PYTHON=cfg["python"],
                          STUDIO_MMAUDIO_REPO=cfg["repo"] + "/demo.py",
                          STUDIO_MMAUDIO_WEIGHTS=cfg["weights"] + "/weights/mmaudio_%s.pth" % cfg["variant"])
    if missing:
        return False, missing
    lacking = missing_hf_models(cfg["hf_cache"])
    if lacking and env("HF_HUB_OFFLINE") == "1":
        return False, "HF 缓存缺 %s（HF_HUB_OFFLINE=1 时无法在线拉取）" % ", ".join(lacking)
    note = "；HF 缓存缺 %s，首次运行会尝试联网下载" % ", ".join(lacking) if lacking else ""
    return True, "%s %s%s" % (cfg["variant"], cfg["weights"], note)


def sfx(video: Path, prompt: str, out: Path, seconds: Optional[float] = None,
        negative_prompt: str = "", seed: int = 42) -> Dict[str, Any]:
    ok, why = available()
    if not ok:
        raise Unavailable(why)
    cfg = config()
    dur = float(seconds or media.duration(video))
    with tempfile.TemporaryDirectory(prefix="mmaudio_") as tmp:
        tmp_dir = Path(tmp)
        for sub in ("weights", "ext_weights"):
            os.symlink(Path(cfg["weights"]) / sub, tmp_dir / sub)
        argv = [cfg["python"], cfg["repo"] + "/demo.py", "--variant", cfg["variant"],
                "--video", str(video), "--prompt", prompt, "--negative_prompt", negative_prompt,
                "--duration", "%.3f" % dur, "--num_steps", env("STUDIO_MMAUDIO_STEPS", "25"),
                "--cfg_strength", "4.5", "--seed", str(int(seed)),
                "--skip_video_composite", "--output", str(tmp_dir / "out")]
        run_subprocess(argv, timeout=env_int("STUDIO_SFX_TIMEOUT", 900), cwd=tmp_dir,
                       extra_env={"HF_HUB_CACHE": cfg["hf_cache"],
                                  "PYTHONPATH": cfg["repo"]})
        flac = tmp_dir / "out" / ("%s.flac" % Path(video).stem)
        if not flac.exists():
            produced = sorted((tmp_dir / "out").glob("*.flac"))
            if not produced:
                raise RuntimeError("MMAudio 没有产出 flac")
            flac = produced[0]
        media.run(["-y", "-i", str(flac), "-c:a", "pcm_s16le", str(out)])
    return {"backend": NAME, "variant": cfg["variant"]}
