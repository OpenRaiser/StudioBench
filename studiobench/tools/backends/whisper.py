# -*- coding: utf-8 -*-
"""asr：Belle-whisper-large-v3-zh，transformers pipeline 进程内单例（MCP server 的解释器里要有 transformers）。

输入可以是音频或视频（视频先抽 16k 单声道）。返回全文和带时间戳的分段，方便模型对齐旁白。
"""
from __future__ import annotations

import importlib.util
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, Tuple

from .. import media

from .common import Unavailable, check_paths, env, under_models

NAME = "belle-whisper-large-v3-zh"
_PIPE: Any = None


def model_path() -> str:
    return env("STUDIO_ASR_MODEL", under_models("Models/Belle-whisper-large-v3-zh"))


def available() -> Tuple[bool, str]:
    missing = check_paths(STUDIO_ASR_MODEL=model_path())
    if missing:
        return False, missing
    for mod in ("torch", "transformers"):
        if importlib.util.find_spec(mod) is None:
            return False, "当前解释器没有 %s" % mod
    return True, model_path()


def _pipe():
    global _PIPE
    if _PIPE is None:
        ok, why = available()
        if not ok:
            raise Unavailable(why)
        import torch
        from transformers import pipeline
        # STUDIO_ASR_DEVICE：MCP 进程内的逻辑卡号（cuda:N），不设就是第一张卡；部署包按配置文件换算好
        device = int((env("STUDIO_ASR_DEVICE", "cuda:0").split(":") + ["0"])[1] or 0) \
            if torch.cuda.is_available() else -1
        dtype = torch.float16 if device >= 0 else torch.float32
        try:                                           # transformers 5 用 dtype，4.x 用 torch_dtype
            _PIPE = pipeline("automatic-speech-recognition", model=model_path(), device=device, dtype=dtype)
        except TypeError:
            _PIPE = pipeline("automatic-speech-recognition", model=model_path(), device=device, torch_dtype=dtype)
    return _PIPE


def _samples(wav: Path) -> Dict[str, Any]:
    """自己读 16k 单声道 pcm_s16le 交给 pipeline。传文件名的话 pipeline 要在 PATH 里找 ffmpeg，工具池的 worker 环境里没有。"""
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        rate, raw = w.getframerate(), w.readframes(w.getnframes())
    return {"raw": np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, "sampling_rate": rate}


def _release() -> None:
    """把这次转写的激活缓存还给显卡。长音频 + batch_size 4 的峰值很大，而 worker 是常驻的，
    torch 的缓存分配器会把这些块一直攒着——实测一个 asr 实例攒到 34.7G，把整张卡占满，
    同卡的 lipsync 每次调用都 OOM（429 次失败），asr 自己也开始报 cuDNN workspace 失败。"""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def transcribe(path: Path, language: str = "zh") -> Dict[str, Any]:
    pipe = _pipe()
    try:
        with tempfile.TemporaryDirectory(prefix="studio_asr_") as tmp:
            wav = media.extract_audio(path, Path(tmp) / "a.wav", rate=16000, mono=True)
            out = pipe(_samples(wav), chunk_length_s=30, batch_size=4, return_timestamps=True,
                       generate_kwargs={"language": language, "task": "transcribe"})
    finally:
        _release()
    chunks = []
    for c in out.get("chunks") or []:
        start, end = (c.get("timestamp") or (None, None))[:2]
        chunks.append({"start": start, "end": end, "text": (c.get("text") or "").strip()})
    return {"backend": NAME, "text": (out.get("text") or "").strip(), "chunks": chunks}
