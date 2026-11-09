# -*- coding: utf-8 -*-
"""tts：Qwen3-TTS-12Hz-1.7B（CustomVoice / Base / VoiceDesign），子进程跑在 qwen_tts 自己的环境里
（workers/qwen3_tts.py，需要 transformers 4.57）。

三种用法，对应 worker 的三个 op：
· 不给 voice_ref → **custom**：bench 固定音色（STUDIO_TTS_SPEAKER，默认 Serena，中文女声），可带语气指令 instruct
· 给了 voice_ref + 原文 → **clone**：zero-shot 克隆（3 秒参考就够）
· 给了 voice_ref 没给原文 → clone 的 x_vector_only 模式（只借说话人向量，质量略降）

和 CosyVoice3 的区别：Qwen3-TTS 没有 speed 参数，合成后用 ffmpeg atempo 变速（不变调）；
参考音频一律先转 24kHz 单声道 PCM wav，避免 libsndfile 读不了视频、mp3、"WAV 里装 AAC"。
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

from .common import WORKERS, Unavailable, check_paths, env, env_int, run_worker, under_share

NAME = "qwen3-tts"
_ROOT_DEFAULT = under_share("models/Qwen3-TTS")
_PYTHON_DEFAULT = under_share("deploy/wrappers/qwentts-python")
_DIRS = {"custom": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
         "clone": "Qwen3-TTS-12Hz-1.7B-Base",
         "design": "Qwen3-TTS-12Hz-1.7B-VoiceDesign"}


def config() -> Dict[str, str]:
    return {
        "python": env("STUDIO_QWENTTS_PYTHON", _PYTHON_DEFAULT),
        "root": env("STUDIO_QWENTTS_ROOT", _ROOT_DEFAULT),
        # bench 固定音色：9 个预置音色里最接近旁白的女声（Vivian / Serena / Uncle_Fu / Dylan / Eric /
        # Ryan / Aiden / Ono_Anna / Sohee）
        "speaker": env("STUDIO_TTS_SPEAKER", "Serena"),
        "instruct": env("STUDIO_TTS_INSTRUCT", ""),
        "language": env("STUDIO_TTS_LANGUAGE", "Auto"),
    }


def available() -> Tuple[bool, str]:
    cfg = config()
    paths = {"STUDIO_QWENTTS_PYTHON": cfg["python"]}
    for kind, sub in _DIRS.items():
        paths["%s 权重" % kind] = cfg["root"] + "/" + sub + "/config.json"
    missing = check_paths(**paths)
    return (False, missing) if missing else (True, cfg["root"])


def tts(text: str, out: Path, voice_ref: Optional[Path] = None,
        voice_ref_text: Optional[str] = None, speed: float = 1.0,
        speaker: Optional[str] = None, instruct: Optional[str] = None,
        language: Optional[str] = None) -> Dict[str, Any]:
    ok, why = available()
    if not ok:
        raise Unavailable(why)
    cfg = config()
    op = "clone" if voice_ref is not None else "custom"
    payload: Dict[str, Any] = {"op": op, "text": text, "out": str(out), "root": cfg["root"],
                               "language": (language or cfg["language"] or "Auto")}
    tmp_ref = None
    if op == "clone":
        payload["ref_audio"], tmp_ref = _readable_audio(voice_ref)
        payload["ref_text"] = (voice_ref_text or "").strip()
    else:
        payload["speaker"] = speaker or cfg["speaker"]
        payload["instruct"] = instruct if instruct is not None else cfg["instruct"]
    try:
        res = _call(cfg["python"], payload)
        _atempo(out, float(speed))
    finally:
        if tmp_ref:
            Path(tmp_ref).unlink(missing_ok=True)
    meta = {"backend": NAME, "mode": op, "sample_rate": res.get("sample_rate"),
            "voice": "voice_ref" if op == "clone" else payload["speaker"],
            "synth_s": res.get("synth_s")}
    if op == "custom":
        meta["speaker"] = payload["speaker"]
        if payload["instruct"]:
            meta["instruct"] = payload["instruct"]
    else:
        meta["ref_text_given"] = bool(payload["ref_text"])
    if abs(float(speed) - 1.0) > 0.01:
        meta["speed"] = float(speed)                 # 事后 atempo，不是模型参数
    return meta


def _readable_audio(src: Path) -> Tuple[str, Optional[str]]:
    """参考音频统一成 24kHz 单声道 PCM wav：视频、mp3、"WAV 里装 AAC" 都能读。
    已经是 wav/flac 就原样用，不复制。返回（给 worker 的路径，用完要删的临时文件或 None）。"""
    if src.suffix.lower() in (".wav", ".flac"):
        return str(src), None
    from .. import media
    fd, tmp = tempfile.mkstemp(prefix="studio_voice_ref_", suffix=".wav")
    os.close(fd)
    try:
        media.run(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", tmp])
    except Exception as exc:  # noqa: BLE001
        Path(tmp).unlink(missing_ok=True)
        raise ValueError("参考音色 %s 读不出音频：%s" % (Path(src).name, str(exc)[:200])) from None
    return tmp, tmp


def _atempo(path: Path, speed: float) -> None:
    """speed ≠ 1 时用 ffmpeg 变速（atempo，保持音高）。"""
    if abs(speed - 1.0) <= 0.01:
        return
    from .. import media
    ratio, chain = float(speed), []
    while ratio < 0.5:                               # atempo 单次只吃 0.5–2.0
        chain.append("atempo=0.5")
        ratio /= 0.5
    while ratio > 2.0:
        chain.append("atempo=2.0")
        ratio /= 2.0
    chain.append("atempo=%.4f" % ratio)
    tmp = path.with_name(path.stem + ".speed" + path.suffix)
    media.run(["-y", "-i", str(path), "-filter:a", ",".join(chain), str(tmp)])
    tmp.replace(path)


def _call(python: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    timeout = env_int("STUDIO_TTS_TIMEOUT", 900)
    if env("STUDIO_TTS_PERSISTENT", "0") == "1":
        return _persistent_call(python, payload, timeout)
    return run_worker(python, "qwen3_tts.py", payload, timeout=timeout)


# --------------------------------------------------------------------------- 常驻 worker
# 单次模式每句都要重新起进程、加载 1.7B 权重；STUDIO_TTS_PERSISTENT=1 时同一道题只起一次。
# 三个模型按需加载、各自常驻约 4.5GB 显存，第一次用到某个 op 时才加载。
_SERVER: Optional[subprocess.Popen] = None


def _die_with_parent() -> None:
    import ctypes
    ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)          # PR_SET_PDEATHSIG：MCP 进程退出时跟着退出


def _server(python: str) -> subprocess.Popen:
    global _SERVER
    if _SERVER is not None and _SERVER.poll() is None:
        return _SERVER
    run_env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):                     # 模型有自己的环境
        run_env.pop(key, None)
    log = open(Path(tempfile.gettempdir()) / ("studio_qwentts_%d.log" % os.getpid()), "a")
    _SERVER = subprocess.Popen([python, str(WORKERS / "qwen3_tts.py"), "--serve"], stdin=subprocess.PIPE,
                               stdout=log, stderr=log, text=True, env=run_env, preexec_fn=_die_with_parent)
    return _SERVER


def _persistent_call(python: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    for _ in (1, 2):                                            # 常驻进程中途挂了就重起一次
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
