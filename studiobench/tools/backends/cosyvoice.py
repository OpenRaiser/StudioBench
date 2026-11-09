# -*- coding: utf-8 -*-
"""tts：Fun-CosyVoice3-0.5B-2512，子进程跑在 CosyVoice 自己的环境里（workers/cosyvoice_tts.py）。

默认音色：bench 固定一条 prompt wav（文档 §3）。题目给了参考音色时，模型把它传成 voice_ref：
· 同时给了 voice_ref_text（参考音频的原文）→ zero-shot 克隆
· 没给原文 → cross-lingual 模式（只借音色，不需要原文）
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .common import WORKERS, Unavailable, check_paths, env, env_int, run_worker, under_models

NAME = "cosyvoice3"
_REPO_DEFAULT = under_models("WSH/StoryTelling/VGen_N/third_party/CosyVoice")


def config() -> Dict[str, str]:
    repo = env("STUDIO_COSYVOICE_REPO", _REPO_DEFAULT)
    return {
        "python": env("STUDIO_COSYVOICE_PYTHON", under_models("miniconda3/envs/cosyvoice/bin/python")),
        "repo": repo,
        "model": env("STUDIO_COSYVOICE_MODEL", under_models("WSH/Models/CosyVoice3-0.5B-2512")),
        # bench 默认旁白音色：CosyVoice 官方示例音频（3.48s 女声），原文见下
        "prompt_wav": env("STUDIO_TTS_PROMPT_WAV", repo + "/asset/zero_shot_prompt.wav"),
        "prompt_text": env("STUDIO_TTS_PROMPT_TEXT", "希望你以后能够做的比我还好呦。"),
    }


def available() -> Tuple[bool, str]:
    cfg = config()
    missing = check_paths(STUDIO_COSYVOICE_PYTHON=cfg["python"],
                          STUDIO_COSYVOICE_REPO=cfg["repo"] + "/cosyvoice/cli/cosyvoice.py",
                          STUDIO_COSYVOICE_MODEL=cfg["model"] + "/cosyvoice3.yaml",
                          STUDIO_TTS_PROMPT_WAV=cfg["prompt_wav"])
    return (False, missing) if missing else (True, cfg["model"])


def tts(text: str, out: Path, voice_ref: Optional[Path] = None,
        voice_ref_text: Optional[str] = None, speed: float = 1.0) -> Dict[str, Any]:
    ok, why = available()
    if not ok:
        raise Unavailable(why)
    cfg = config()
    tmp_ref = None
    if voice_ref is not None:
        prompt_wav, tmp_ref = _readable_audio(voice_ref)
        prompt_text = (voice_ref_text or "").strip()
    else:
        prompt_wav, prompt_text = cfg["prompt_wav"], cfg["prompt_text"]
    try:
        return _tts(cfg, text, out, prompt_wav, prompt_text, voice_ref is not None, speed)
    finally:
        if tmp_ref:
            Path(tmp_ref).unlink(missing_ok=True)


def _readable_audio(src: Path) -> Tuple[str, Optional[str]]:
    """CosyVoice 用 libsndfile 读参考音色。直接能读的 wav/flac 原样用；视频、mp3、“WAV 里装 AAC”之类
    先用 ffmpeg 转成 24kHz 单声道 PCM wav。返回（给 CosyVoice 的路径，用完要删的临时文件或 None）。"""
    try:
        import soundfile as sf
        if sf.info(str(src)).format in ("WAV", "FLAC"):
            return str(src), None
    except Exception:  # noqa: BLE001  读不了就转
        pass
    from .. import media
    fd, tmp = tempfile.mkstemp(prefix="studio_voice_ref_", suffix=".wav")
    os.close(fd)
    try:
        media.run(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", tmp])
    except Exception as exc:  # noqa: BLE001
        Path(tmp).unlink(missing_ok=True)
        raise ValueError("参考音色 %s 读不出音频：%s" % (Path(src).name, str(exc)[:200])) from None
    return tmp, tmp


def _tts(cfg: Dict[str, str], text: str, out: Path, prompt_wav: str, prompt_text: str,
         has_ref: bool, speed: float) -> Dict[str, Any]:
    payload = {"text": text, "out": str(out), "repo": cfg["repo"], "model": cfg["model"],
               "prompt_wav": prompt_wav, "prompt_text": prompt_text,
               "mode": "zero_shot" if prompt_text else "cross_lingual", "speed": float(speed)}
    timeout = env_int("STUDIO_TTS_TIMEOUT", 900)
    if env("STUDIO_TTS_PERSISTENT", "0") == "1":
        res = _persistent_call(cfg["python"], payload, timeout)
    else:
        res = run_worker(cfg["python"], "cosyvoice_tts.py", payload, timeout=timeout)
    return {"backend": NAME, "mode": payload["mode"],
            "voice": "voice_ref" if has_ref else "bench_default",
            "sample_rate": res.get("sample_rate")}


# --------------------------------------------------------------------------- 常驻 worker
# 单次模式每句都要重新起进程、加载模型（实测约 390 秒一句，真正合成只占一小部分）。
# STUDIO_TTS_PERSISTENT=1 时，同一个 MCP 进程（即同一道题）里只在第一次调用时起一个常驻 worker，之后复用；
# 常驻 worker 会一直占着约 9GB 显存，辅助模型和别的服务共卡、显存很紧时不要开。
_SERVER: Optional[subprocess.Popen] = None


def _die_with_parent() -> None:
    import ctypes
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)          # PR_SET_PDEATHSIG：MCP 进程退出时跟着退出


def _server(python: str) -> subprocess.Popen:
    global _SERVER
    if _SERVER is not None and _SERVER.poll() is None:
        return _SERVER
    run_env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):                     # 同 run_subprocess：模型有自己的环境
        run_env.pop(key, None)
    log = open(Path(tempfile.gettempdir()) / ("studio_cosyvoice_%d.log" % os.getpid()), "a")
    _SERVER = subprocess.Popen([python, str(WORKERS / "cosyvoice_tts.py"), "--serve"], stdin=subprocess.PIPE,
                               stdout=log, stderr=log, text=True, env=run_env, preexec_fn=_die_with_parent)
    return _SERVER


def _persistent_call(python: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    for attempt in (1, 2):                                       # 常驻进程中途挂了就重起一次
        proc = _server(python)
        with tempfile.TemporaryDirectory(prefix="studio_tts_") as tmp:
            req, resp = Path(tmp) / "request.json", Path(tmp) / "response.json"
            req.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            try:
                proc.stdin.write(json.dumps({"request": str(req), "response": str(resp)}) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                proc.kill()
                continue
            deadline = time.time() + timeout
            while not resp.exists():
                if proc.poll() is not None:
                    break
                if time.time() > deadline:
                    proc.kill()
                    raise RuntimeError("tts 常驻进程超时（%ds）" % timeout)
                time.sleep(0.2)
            if not resp.exists():
                continue
            out = json.loads(resp.read_text(encoding="utf-8"))
        if not out.get("ok"):
            raise RuntimeError("worker 报错：%s" % out.get("error"))
        return out
    raise RuntimeError("tts 常驻进程启动失败，日志在 %s" % tempfile.gettempdir())
