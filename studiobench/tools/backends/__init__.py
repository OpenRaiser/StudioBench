# -*- coding: utf-8 -*-
"""后端注册表：每个工具在当前机器上实际用哪个后端、是否可用。

· 默认只用真实后端；某个后端不可用时，工具直接报错并说明原因，**绝不悄悄换成 mock**
· `STUDIO_BACKEND=mock` 时所有生成类工具一律走 mock（只用于没有 GPU 时验证 harness）
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Tuple

from . import cosyvoice, h3, mmaudio, qwen3tts, qwen_image, search, template, whisper
from .common import Unavailable, env

GENERATION_TOOLS = ("t2v", "i2v", "ref_video", "t2i", "image_edit", "tts", "music",
                    "sfx", "lipsync", "asr", "search")
FFMPEG_TOOLS = ("sample_frames", "trim", "concat", "overlay", "mix_audio", "inspect", "export")
TTS_BACKENDS = {"qwen3-tts": qwen3tts, "qwen3": qwen3tts, "cosyvoice3": cosyvoice, "cosyvoice": cosyvoice}

__all__ = ["GENERATION_TOOLS", "FFMPEG_TOOLS", "Unavailable", "mock_mode", "status", "backend_name",
           "tts_backend"]


def tts_backend():
    """tts 用哪个后端：STUDIO_TTS_BACKEND（默认 qwen3-tts；cosyvoice3 只作对照/回退）。"""
    name = env("STUDIO_TTS_BACKEND", "qwen3-tts").lower()
    return TTS_BACKENDS.get(name, qwen3tts)


def mock_mode() -> bool:
    return env("STUDIO_BACKEND").lower() == "mock"


def _h3(base: str, label: str, deep: bool) -> Tuple[bool, str]:
    if not base:
        return False, "未配置 %s" % label
    if not deep:
        return True, base
    ok, detail = h3.ping(base, timeout=3)
    return ok, ("%s %s" % (base, detail)) if ok else ("%s 连不上：%s" % (base, detail))


_CHECKS: Dict[str, Tuple[str, Callable[[bool], Tuple[bool, str]]]] = {
    "t2v": ("h3-fl2va", lambda deep: _h3(h3.fl2va_base(), "H3_ENDPOINT", deep)),
    "i2v": ("h3-fl2va", lambda deep: _h3(h3.fl2va_base(), "H3_ENDPOINT", deep)),
    "ref_video": ("h3-ref2va", lambda deep: _h3(h3.ref2va_base(), "H3_REF2VA_ENDPOINT", deep)),
    "t2i": (qwen_image.T2I_NAME, lambda deep: qwen_image.available_t2i()),
    "image_edit": (qwen_image.EDIT_NAME, lambda deep: qwen_image.available_edit()),
    "tts": (qwen3tts.NAME, lambda deep: tts_backend().available()),
    "music": ("music-cmd", lambda deep: template.music_available()),
    "sfx": (mmaudio.NAME, lambda deep: mmaudio.available()),
    "lipsync": ("lipsync-cmd", lambda deep: template.lipsync_available()),
    "asr": (whisper.NAME, lambda deep: whisper.available()),
    "search": (search.NAME, lambda deep: search.available()),
}


def _extra(tool: str):
    """v2 新增工具的 spec（extra/），不是新增工具就返回 None。延迟导入，避免和 registry 循环导入。"""
    from ..extra import TOOLS as extra_tools
    return extra_tools.get(tool)


def backend_name(tool: str) -> str:
    if tool in FFMPEG_TOOLS:
        return "ffmpeg"
    spec = _extra(tool)
    if spec is not None:                      # 新增工具都在本机跑（浏览器、Blender、ffmpeg），mock 模式下也真跑
        return str(spec.get("backend") or tool)
    if mock_mode():
        return "mock"
    name = _CHECKS[tool][0]
    if tool == "music":
        return env("STUDIO_MUSIC_NAME", name)
    if tool == "lipsync":
        return env("STUDIO_LIPSYNC_NAME", name)
    if tool == "search":
        return search.backend_name()          # web（免 key）| serper
    if tool == "tts":
        return tts_backend().NAME             # qwen3-tts（默认）| cosyvoice3
    return name


def status(deep: bool = False) -> Dict[str, Dict[str, object]]:
    """tool → {backend, ok, detail}。deep=True 时会探测 H3 服务是否在线。"""
    rows: Dict[str, Dict[str, object]] = {}
    from ...pool import client as pool_client
    pools = pool_client.pools() if not mock_mode() else {}
    for tool in GENERATION_TOOLS:
        if mock_mode():
            rows[tool] = {"backend": "mock", "ok": True, "detail": "STUDIO_BACKEND=mock"}
            continue
        pool = pool_client.pool_of(tool) if pools else None
        if pool:                              # 工具池服务：池里有健康实例就算可用
            p = pools[pool]
            # 网关重启后的头几秒所有实例都是 unhealthy，这里短暂重试，避免 preflight 误判
            for _ in range(3):
                if p.get("healthy", 0) > 0 or deep is False:
                    break
                time.sleep(1.5)
                p = (pool_client.pools(max_age=0) or {}).get(pool, p)
            rows[tool] = {"backend": backend_name(tool), "ok": p.get("healthy", 0) > 0,
                          "detail": "工具池 %s：%d/%d 个实例在线" % (pool, p.get("healthy", 0), p.get("instances", 0))}
            continue
        name, check = _CHECKS[tool]
        try:
            ok, detail = check(deep)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, "检查失败：%s" % exc
        rows[tool] = {"backend": backend_name(tool), "ok": bool(ok), "detail": detail}
    for tool in FFMPEG_TOOLS:
        rows[tool] = {"backend": "ffmpeg", "ok": True, "detail": ""}
    from ..extra import TOOLS as extra_tools
    for tool, spec in extra_tools.items():
        check = spec.get("check")
        try:
            ok, detail = check() if check else (True, "")
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, "检查失败：%s" % exc
        rows[tool] = {"backend": backend_name(tool), "ok": bool(ok), "detail": detail}
    return rows
