# -*- coding: utf-8 -*-
"""记住 MCP server 起过的子进程组，退出时统一 killpg。

blender 和 chromium 都是 start_new_session 起的独立进程组（`extra/blender.py`、`extra/web.py`）。
工具自己超时会 killpg，但 server 被强杀时就没人管了——run.py 到点先 killpg claude 那组，
MCP server 跟着没，正在渲的 blender / chromium 会接着跑（最长 60 分钟），占着 GPU 进下一题。

所以：起子进程时把 pgid 记下来，退出时（正常 / SIGTERM / SIGINT / SIGHUP）统一清；
同时把清单写进运行目录，run.py 收尾时再兜一次，对付 server 被 SIGKILL 的情况。
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Set

_LOCK = threading.RLock()
_PGIDS: Set[int] = set()
_PATH: Optional[Path] = None
_SELF_PGID = os.getpgrp()
NAME = "procgroups.json"                 # 运行目录里的清单文件名


def configure(path: Optional[Path]) -> None:
    """清单写到哪（server 用 STUDIO_RUN_DIR，run.py 用运行目录）。"""
    global _PATH
    _PATH = Path(path) if path else None


def _write() -> None:
    if not _PATH:
        return
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_name(_PATH.name + ".tmp")
        tmp.write_text(json.dumps(sorted(_PGIDS)), encoding="utf-8")
        tmp.replace(_PATH)
    except OSError:
        pass


def register(proc: Any) -> None:
    """记下一个子进程所在的进程组。同组的不用记——杀 server 的组就带上了。"""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return
    if pgid == _SELF_PGID:
        return
    with _LOCK:
        if pgid not in _PGIDS:
            _PGIDS.add(pgid)
            _write()


def release(proc: Any) -> None:
    """子进程正常结束了，不用再管它。"""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = proc.pid                  # 进程已经没了：start_new_session 时 pgid 就等于 pid
    with _LOCK:
        if pgid in _PGIDS:
            _PGIDS.discard(pgid)
            _write()


def _kill(pgids: List[int]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pgid in pgids:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if sig == signal.SIGTERM:
            time.sleep(0.4)              # 给 blender / chromium 一点时间收尾，再强杀


def kill_all(reason: str = "") -> List[int]:
    """杀掉记下的每个进程组，返回杀过的 pgid。"""
    with _LOCK:
        pgids = sorted(_PGIDS)
        if not pgids:
            return []
        _PGIDS.clear()
        _write()
    _kill(pgids)
    return pgids


def kill_recorded(path: Path) -> List[int]:
    """run.py 收尾兜底：读 server 写下的清单，杀掉还在的，然后把清单清空。"""
    try:
        pgids = [int(p) for p in json.loads(Path(path).read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []
    pgids = [p for p in pgids if p > 1 and p != _SELF_PGID]
    if not pgids:
        return []
    with _LOCK:                      # run.py 清过的，server 这边也别再记着（否则死掉的 pgid 会越攒越多）
        for p in pgids:
            _PGIDS.discard(p)
        if _PATH is not None and Path(path).resolve() == _PATH.resolve():
            _write()
    _kill(pgids)
    try:
        Path(path).write_text("[]", encoding="utf-8")
    except OSError:
        pass
    return pgids


def install(reason: str = "server 退出") -> None:
    """server 起来时调一次：退出、SIGTERM / SIGINT / SIGHUP 都统一清。"""
    atexit.register(kill_all, reason)

    def _on_signal(signum, _frame):
        kill_all("收到信号 %d" % signum)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass                          # 不在主线程里装不了，atexit 还在
