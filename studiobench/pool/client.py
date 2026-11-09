# -*- coding: utf-8 -*-
"""MCP server 一侧的工具池客户端：设了 STUDIO_POOL_URL 就从网关租实例，没设就什么都不做（走原来的本机后端）。

  with lease("h3_fl2va") as base:   h3.generate(base, ...)                    # H3：拿到实例地址照旧调用
  routed("t2i", qwen_image.t2i, prompt, aspect, out, ...)                     # 辅助模型：在池里的实例上执行同一个函数

租约期间每 20 秒续一次；调用方进程崩了，网关 90 秒后自动收回实例。
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from .catalog import TOOL_POOL

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_STATUS: Dict[str, Any] = {"t": 0.0, "pools": {}}
_STATUS_LOCK = threading.Lock()
_TL = threading.local()                       # 当前线程最近一次租到的实例名（写进调用结果，便于追查）
RENEW_EVERY = float(os.environ.get("STUDIO_POOL_RENEW", "20"))


def gateway() -> str:
    return os.environ.get("STUDIO_POOL_URL", "").strip().rstrip("/")


def run_id() -> str:
    return os.environ.get("STUDIO_POOL_RUN") or os.environ.get("STUDIO_RUN_DIR") or "run-%d" % os.getpid()


def _post(url: str, obj: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                 {"Content-Type": "application/json"}, method="POST")
    with _NOPROXY.open(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _get(url: str, timeout: float) -> Dict[str, Any]:
    with _NOPROXY.open(urllib.request.Request(url), timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def pools(max_age: float = 30.0) -> Dict[str, Any]:
    """网关上的池（缓存 max_age 秒）；没开池或网关连不上返回 {}。"""
    gw = gateway()
    if not gw:
        return {}
    with _STATUS_LOCK:
        if time.time() - _STATUS["t"] < max_age:
            return _STATUS["pools"]
    try:
        data = _get(gw + "/status", timeout=5).get("pools") or {}
    except Exception:  # noqa: BLE001
        data = {}
    with _STATUS_LOCK:
        _STATUS.update({"t": time.time(), "pools": data})
    return data


def pool_of(tool: str) -> Optional[str]:
    """这个工具由哪个池服务；没开池、或网关上没有这个池时返回 None（走本机）。"""
    name = TOOL_POOL.get(tool)
    if not name or not gateway():
        return None
    return name if name in pools() else None


@contextlib.contextmanager
def lease(pool: str) -> Iterator[str]:
    """租一个实例，yield 它的地址；排队时一直等（MCP 工具超时远大于排队时间）。"""
    gw, key = gateway(), uuid.uuid4().hex
    got: Dict[str, Any] = {}
    errors = 0
    while not got:
        try:
            rep = _post(gw + "/acquire", {"pool": pool, "run": run_id(), "key": key, "wait": 25}, timeout=60)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            errors += 1
            if errors >= 12:                 # 网关一分钟连不上：报错，别无限等
                raise RuntimeError("工具池网关连不上（%s）：%s" % (gw, exc)) from None
            time.sleep(5)
            continue
        errors = 0
        if rep.get("error"):
            raise RuntimeError("工具池：%s" % rep["error"])
        if rep.get("lease"):
            got = rep
    stop = threading.Event()
    every = min(RENEW_EVERY, float(got.get("ttl") or 90) / 4)      # 续租间隔按网关的租约时长定，不会续晚了被收回

    def renew() -> None:
        while not stop.wait(every):
            try:
                _post(gw + "/renew", {"lease": got["lease"]}, timeout=10)
            except Exception:  # noqa: BLE001  网关短暂不通：下次再续
                pass

    t = threading.Thread(target=renew, daemon=True)
    t.start()
    _TL.instance = got.get("name")
    _TL.window = [round(time.time(), 3), None]
    try:
        yield got["url"]
    finally:
        _TL.window[1] = round(time.time(), 3)
        stop.set()
        try:
            _post(gw + "/release", {"lease": got["lease"]}, timeout=10)
        except Exception:  # noqa: BLE001  网关会按租约超时收回
            pass


# --------------------------------------------------------------------------- 远程执行辅助模型
def _enc(v: Any) -> Any:
    if isinstance(v, Path):
        return {"__path__": str(v)}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    return v


def dec(v: Any) -> Any:
    if isinstance(v, dict):
        if set(v) == {"__path__"}:
            return Path(v["__path__"])
        return {k: dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [dec(x) for x in v]
    return v


TIMEOUTS = {"t2i": 1200, "image_edit": 1200, "asr": 900, "tts": 900, "sfx": 1800, "music": 1800, "lipsync": 3600}


def routed(tool: str, fn: Callable[..., Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """池里有这个工具就在实例上执行 fn（实例上调同一个后端函数），否则本机直接调。"""
    pool = pool_of(tool)
    if pool is None:
        return fn(*args, **kwargs)
    with lease(pool) as url:
        try:
            rep = _post(url + "/run", {"tool": tool, "args": _enc(list(args)), "kwargs": _enc(kwargs)},
                        timeout=TIMEOUTS.get(tool, 1800))
        except urllib.error.HTTPError as exc:
            body = exc.read()[:2000].decode("utf-8", "replace")
            try:
                rep = json.loads(body)
            except ValueError:
                raise RuntimeError("%s 实例 HTTP %s：%s" % (tool, exc.code, body[:300])) from None
    if rep.get("unavailable"):
        from ..tools.backends import Unavailable
        raise Unavailable(rep["error"])
    if rep.get("error"):
        raise RuntimeError(rep["error"])
    res = dec(rep.get("result") or {})
    if rep.get("instance"):
        res["instance"] = rep["instance"]
    return res


def current_instance() -> Optional[str]:
    return getattr(_TL, "instance", None)


def reset() -> None:
    _TL.instance, _TL.window = None, None


def last_lease() -> Optional[Dict[str, Any]]:
    """本线程这次调用租过的实例和租用时间窗（写进 .calls.jsonl：排队 = t_lease - t_run，执行 = t_release - t_lease）。"""
    if not getattr(_TL, "instance", None):
        return None
    w = getattr(_TL, "window", None) or [None, None]
    return {"instance": _TL.instance, "t_lease": w[0], "t_release": w[1]}


def h3_base(tool: str, default: Callable[[], str]):
    """H3（t2v / i2v / ref_video）：池里有就租一个实例（上下文管理器，yield 地址），没有就用原来的单一地址。"""
    pool = pool_of(tool)
    return lease(pool) if pool else contextlib.nullcontext(default())
