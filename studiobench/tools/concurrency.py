#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具并发执行的公共部件（MCP server 用线程池并发跑 tools/call 和 batch）。

· 槽位（slot）：同一资源组同时能跑几个调用。没开工具池时，进程内的 GPU 单例（qwen-image、whisper、TTS 常驻 worker）
  和同一张音频卡上的模型都只有一份，所以默认 1；H3 服务端自己也是串行，客户端先排队，免得排队时间算进 H3_MAX_WAIT。
  开了工具池（STUDIO_POOL_URL）后，池里的组由网关按实例空闲情况分配，这里不再限。
  环境变量 STUDIO_SLOTS_<组名大写>=N 可以改单组上限。
· 自动命名：不给 out 时的 t2i_001.png 这类名字，在内存里登记占用，两个并发调用不会拿到同一个名字。
· 临时文件：每次调用自己登记的 .part 文件，调用结束只清自己的；没有调用在跑时才整目录扫一遍。
· 键锁：同一个输出路径 / 抽帧目录同一时刻只让一个调用写。
· mock 模拟耗时：STUDIO_MOCK_DELAY="i2v=3,t2i=1,*=0.2"（秒），只在 STUDIO_BACKEND=mock 时生效，没 GPU 时测并发用。
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

# 工具 → 资源组。不在表里的（ffmpeg 剪辑、检索、看帧……）是 CPU 轻活，只受线程池上限约束
GROUP: Dict[str, str] = {
    "t2v": "h3_fl2va", "i2v": "h3_fl2va", "ref_video": "h3_ref2va",
    "t2i": "t2i", "image_edit": "image_edit", "asr": "asr",
    "tts": "audio", "music": "audio", "sfx": "audio", "lipsync": "audio",   # 同一张音频卡（STUDIO_GPU_AUDIO）
    "render_blender": "blender", "render_html": "web", "render_three": "web", "remove_bg": "remove_bg",
    "export": "export",
}
LOCAL_SLOTS: Dict[str, int] = {"h3_fl2va": 1, "h3_ref2va": 1, "t2i": 1, "image_edit": 1, "asr": 1, "audio": 1,
                               "blender": 2, "web": 2, "remove_bg": 2, "export": 1}
POOLED: Set[str] = set()          # 由工具池管的组（pool.client 启用时填）


def pool_enabled() -> bool:
    return bool(os.environ.get("STUDIO_POOL_URL", "").strip())


class _Slots:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sem: Dict[str, threading.BoundedSemaphore] = {}
        self.busy: Dict[str, int] = {}
        self.waiting: Dict[str, int] = {}

    def limit(self, group: str) -> Optional[int]:
        env = os.environ.get("STUDIO_SLOTS_" + group.upper(), "").strip()
        if env:
            try:
                return max(1, int(env))
            except ValueError:
                pass
        if group in POOLED:
            return None
        return LOCAL_SLOTS.get(group)

    def _get(self, group: str, n: int) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._sem.get(group)
            if sem is None:
                sem = self._sem[group] = threading.BoundedSemaphore(n)
            return sem

    @contextlib.contextmanager
    def hold(self, tool: str) -> Iterator[None]:
        group = GROUP.get(tool) or ""
        n = self.limit(group) if group else None
        if n and _pooled(tool):               # 这个工具由工具池服务：网关按实例空闲分配，这里不再排队
            n = None
        if not n:
            yield
            return
        sem = self._get(group, n)
        with self._lock:
            self.waiting[group] = self.waiting.get(group, 0) + 1
        sem.acquire()
        with self._lock:
            self.waiting[group] -= 1
            self.busy[group] = self.busy.get(group, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self.busy[group] -= 1
            sem.release()

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            out = {}
            for g in set(self.busy) | set(self.waiting):
                lim = self.limit(g)
                out[g] = {"busy": self.busy.get(g, 0), "waiting": self.waiting.get(g, 0), "slots": lim or 0}
            return out


def _pooled(tool: str) -> bool:
    if not pool_enabled():
        return False
    from ..pool import client                # 延迟导入：pool.client 只依赖 catalog
    return client.pool_of(tool) is not None


SLOTS = _Slots()

# --------------------------------------------------------------------------- 调用上下文（线程本地）
_CTX = threading.local()
_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT = 0


@contextlib.contextmanager
def call_context() -> Iterator[None]:
    """包住一次工具调用：登记在跑数、收集本次的临时文件和占用的名字，结束时释放。"""
    global _IN_FLIGHT
    _CTX.parts, _CTX.names = [], []
    with _FLIGHT_LOCK:
        _IN_FLIGHT += 1
    try:
        yield
    finally:
        for p in _CTX.parts:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        with _NAME_LOCK:
            for n in _CTX.names:
                _RESERVED.discard(n)
        _CTX.parts, _CTX.names = [], []
        with _FLIGHT_LOCK:
            _IN_FLIGHT -= 1


def in_flight() -> int:
    with _FLIGHT_LOCK:
        return _IN_FLIGHT


def track_part(p: Path) -> None:
    parts = getattr(_CTX, "parts", None)
    if parts is not None:
        parts.append(p)


# --------------------------------------------------------------------------- 自动命名
_NAME_LOCK = threading.Lock()
_RESERVED: Set[str] = set()


def reserve_auto(workdir: Path, tool: str, ext: str) -> Path:
    """tool_001.ext 起找第一个磁盘上不存在、也没被其它在跑调用占用的名字。"""
    with _NAME_LOCK:
        n = 1
        while True:
            name = "%s_%03d%s" % (tool, n, ext)
            p = workdir / name
            if str(p) not in _RESERVED and not p.exists():
                _RESERVED.add(str(p))
                names = getattr(_CTX, "names", None)
                if names is not None:
                    names.append(str(p))
                return p
            n += 1


def reserve_name(parent: Path, stem: str, ext: str = "") -> Path:
    """parent/stem_001ext 起找第一个没被用、也没被在跑调用占用的名字（目录或文件都行），登记到本次调用。"""
    with _NAME_LOCK:
        n = 1
        while True:
            p = parent / ("%s_%03d%s" % (stem, n, ext))
            if str(p) not in _RESERVED and not p.exists():
                _RESERVED.add(str(p))
                names = getattr(_CTX, "names", None)
                if names is not None:
                    names.append(str(p))
                return p
            n += 1


# --------------------------------------------------------------------------- 键锁
_KEY_LOCK = threading.Lock()
_KEYS: Dict[str, List] = {}       # key → [lock, 引用数]


@contextlib.contextmanager
def keyed(key: str) -> Iterator[None]:
    with _KEY_LOCK:
        ent = _KEYS.setdefault(key, [threading.Lock(), 0])
        ent[1] += 1
    ent[0].acquire()
    try:
        yield
    finally:
        ent[0].release()
        with _KEY_LOCK:
            ent[1] -= 1
            if ent[1] == 0:
                _KEYS.pop(key, None)


# --------------------------------------------------------------------------- mock 模拟耗时
def mock_delay(tool: str) -> float:
    spec = os.environ.get("STUDIO_MOCK_DELAY", "").strip()
    if not spec:
        return 0.0
    table: Dict[str, float] = {}
    for item in spec.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            try:
                table[k.strip()] = float(v)
            except ValueError:
                continue
    return table.get(tool, table.get("*", 0.0))


def simulate(tool: str) -> None:
    d = mock_delay(tool)
    if d > 0:
        time.sleep(d)
