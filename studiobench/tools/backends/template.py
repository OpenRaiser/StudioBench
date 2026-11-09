# -*- coding: utf-8 -*-
"""music / lipsync：模型待定（文档写 ACE-Step / suno、LatentSync），先按命令模板接入。

STUDIO_MUSIC_CMD    JSON argv 模板，占位符 {prompt} {duration} {output} {lyrics} {lyrics_file}
STUDIO_LIPSYNC_CMD  JSON argv 模板，占位符 {video} {audio} {output}
模板写法和示例见 RUNBOOK_GPU.md。命令必须把结果写到 {output}，返回码 0 视为成功。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .common import env, env_int, run_argv_template


def music_available() -> Tuple[bool, str]:
    return (True, env("STUDIO_MUSIC_NAME", "music-cmd")) if env("STUDIO_MUSIC_CMD") \
        else (False, "未配置 STUDIO_MUSIC_CMD（ACE-Step/suno 待接入）")


def lipsync_available() -> Tuple[bool, str]:
    return (True, env("STUDIO_LIPSYNC_NAME", "lipsync-cmd")) if env("STUDIO_LIPSYNC_CMD") \
        else (False, "未配置 STUDIO_LIPSYNC_CMD（LatentSync 待接入）")


def music(prompt: str, seconds: float, out: Path, lyrics: Optional[str] = None) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="studio_music_") as tmp:
        lyrics_file = Path(tmp) / "lyrics.txt"
        lyrics_file.write_text(lyrics or "", encoding="utf-8")
        run_argv_template("STUDIO_MUSIC_CMD",
                          {"prompt": prompt, "duration": "%.2f" % seconds, "output": str(out),
                           "lyrics": lyrics or "", "lyrics_file": str(lyrics_file)},
                          timeout=env_int("STUDIO_MUSIC_TIMEOUT", 1800))
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("music 命令没有写出 %s" % out.name)
    return {"backend": env("STUDIO_MUSIC_NAME", "music-cmd")}


def lipsync(video: Path, audio: Path, out: Path) -> Dict[str, Any]:
    run_argv_template("STUDIO_LIPSYNC_CMD", {"video": str(video), "audio": str(audio), "output": str(out)},
                      timeout=env_int("STUDIO_LIPSYNC_TIMEOUT", 1800))
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("lipsync 命令没有写出 %s" % out.name)
    return {"backend": env("STUDIO_LIPSYNC_NAME", "lipsync-cmd")}
