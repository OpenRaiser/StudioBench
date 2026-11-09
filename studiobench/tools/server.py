#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StudioBench MCP server（stdio，JSON-RPC 2.0，换行分帧）。

把 `backend.TOOLS` 的 18 个原子工具暴露给 Claude Code，并负责：
· **预算**：STUDIO_MAX_CALLS 次调用（文档 200）；用完后一律拒绝，以已经 export 的成片为准
· **时限**：STUDIO_DEADLINE（epoch 秒，文档 8 小时）之后一律拒绝
· **留痕**：每次调用写 <WORKDIR>/.calls.jsonl（工具、参数、后端、产物、耗时、成败）
· **看图**：工具返回的 `_images` 编码成 MCP image content，模型能直接看到画面
· **可用性**：tools/list 里给本机不可用的工具加「[本机不可用：原因]」，调用时同样报错，不会退回 mock
· **心跳**：工具运行期间每 STUDIO_HEARTBEAT_SEC 秒（默认 60）发一条 MCP 进度通知，免得 Claude Code 的空闲超时
  （stdio 工具默认 30 分钟没有回音就判失败）把还在排队、生成的 H3 调用掐掉；进度通知不进模型上下文
· **并发**：tools/call 进线程池（STUDIO_MAX_PARALLEL，默认 16），同一条消息里的多个调用真并行；另有 `batch` 工具一次交多条。
  预算、序号 i、日志都在锁里记；同一 GPU 资源的调用按 tools/concurrency.py 的槽位排队（开工具池后由网关按实例分配）。
  日志每条多记 t_run（拿到槽位开始执行）、t_end；batch 里的调用记同一个 batch 号；返回的 budget.call 是这次调用的 i。

由 harness/run.py 生成的 mcp.json 以 `python -m studiobench.tools.server` 拉起；密钥不写进 mcp.json，而是放在 STUDIO_SECRETS_FILE 指向的私有文件里。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))      # 直接按脚本运行时也能找到 studiobench


def _load_secrets() -> None:
    path = os.environ.get("STUDIO_SECRETS_FILE")
    if not path or not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


_load_secrets()

from studiobench.tools import backends  # noqa: E402
from studiobench.tools import concurrency  # noqa: E402
from studiobench.pool import client as pool_client  # noqa: E402
from studiobench.tools import procgroups  # noqa: E402
from studiobench.tools import registry as backend  # noqa: E402

WORKDIR = Path(os.environ.get("STUDIO_WORKDIR", ".")).resolve()
MAX_CALLS = int(os.environ.get("STUDIO_MAX_CALLS", "200"))
DEADLINE = float(os.environ.get("STUDIO_DEADLINE") or 0) or None
CALL_LOG = WORKDIR / ".calls.jsonl"
SERVER_LOG = Path(os.environ.get("STUDIO_RUN_DIR") or WORKDIR) / "mcp_server.log"
PROTOCOL = "2025-06-18"
HEARTBEAT_SEC = float(os.environ.get("STUDIO_HEARTBEAT_SEC", "60"))
MAX_PARALLEL = max(1, int(os.environ.get("STUDIO_MAX_PARALLEL", "16")))
BATCH_MAX = max(1, int(os.environ.get("STUDIO_BATCH_MAX", "16")))
BATCH_IMAGES = max(0, int(os.environ.get("STUDIO_BATCH_IMAGES", "16")))
PARALLEL_HINT = os.environ.get("STUDIO_MCP_PARALLEL_HINT", "1") != "0"
NO_BATCH = ("export", "batch")               # 交卷只能单独调；batch 不能套 batch
backend.set_workdir(WORKDIR)


_SEND_LOCK = threading.Lock()                # 工具运行时心跳线程也会写 stdout


def _send(obj: Dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _SEND_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _log_server(msg: str) -> None:
    try:
        with SERVER_LOG.open("a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


_STATE_LOCK = threading.Lock()               # 预算计数、序号、调用日志
_CALL_EXEC = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="call")
_BATCH_EXEC = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="batch")   # 分开：batch 占着一个 call 线程等子项
_BATCH_SEQ = [0]


def _append_call(entry: Dict[str, Any]) -> None:
    with _STATE_LOCK:
        with CALL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _executed_calls() -> int:
    if not CALL_LOG.exists():
        return 0
    n = 0
    for line in CALL_LOG.read_text(encoding="utf-8").splitlines():
        try:
            n += 0 if json.loads(line).get("rejected") else 1
        except ValueError:
            continue
    return n


USED = _executed_calls()                 # 续跑时从日志恢复
STATUS = backends.status(deep=True)      # 启动时探测一次 H3 是否在线


def _budget(call: Any = None) -> Dict[str, Any]:
    with _STATE_LOCK:
        used = USED
    info: Dict[str, Any] = {"calls_used": used, "calls_max": MAX_CALLS}
    if call is not None:
        info["call"] = call                  # 这次调用的序号（= .calls.jsonl 里的 i）
    if DEADLINE:
        info["minutes_left"] = max(0, int((DEADLINE - time.time()) // 60))
    running = concurrency.in_flight()
    if running > 1:
        info["running"] = running            # 同时在跑的调用数
    return info


def _admit(name: str, args: Dict[str, Any]):
    """原子地查时限和预算、占一个序号。返回 (i, None, None) 或 (None, 原因, 给模型的话)。"""
    global USED
    with _STATE_LOCK:
        if DEADLINE and time.time() >= DEADLINE:
            return None, "deadline", "时间已到（墙钟上限）。本题以已经 export 的成片为准，请结束。"
        if USED >= MAX_CALLS:
            return None, "budget", "调用预算已用完（%d/%d）。本题以已经 export 的成片为准，请结束。" % (USED, MAX_CALLS)
        USED += 1
        return USED, None, None


def _tool_list() -> List[Dict[str, Any]]:
    tools = []
    for name, spec in backend.TOOLS.items():
        desc = spec["desc"]
        st = STATUS.get(name) or {}
        if not st.get("ok", True):
            desc = "[本机不可用：%s] %s" % (st.get("detail"), desc)
        item: Dict[str, Any] = {"name": name, "description": desc, "inputSchema": spec["schema"]}
        if PARALLEL_HINT and name not in NO_BATCH:
            # Claude Code 只并发执行标了只读的工具；这些工具都在各自的输出文件上工作，可以同时跑
            item["annotations"] = {"readOnlyHint": True}
        tools.append(item)
    tools.append({"name": "batch", "description": BATCH_DESC, "inputSchema": BATCH_SCHEMA})
    return tools


BATCH_DESC = ("一次提交多个互不依赖的工具调用，服务端并行执行，全部完成后按提交顺序一起返回结果（results[i] 对应 calls[i]）。"
              "适合一口气生成多张关键帧、多段镜头、多句配音或多条音效：后一条要用前一条的产物时不要放进同一个 batch。"
              "每条单独计入调用预算，超出预算的那几条会被拒绝；某条失败不影响其它条。最多 %d 条，不能包含 export 和 batch。"
              "同一时刻真正在跑的数量受本机 GPU 实例数限制，多出来的会排队。" % BATCH_MAX)
BATCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["calls"], "properties": {
    "calls": {"type": "array", "minItems": 1, "maxItems": BATCH_MAX, "description": "要并行执行的调用",
              "items": {"type": "object", "additionalProperties": False, "required": ["tool", "args"], "properties": {
                  "tool": {"type": "string", "description": "工具名，如 i2v、t2i、tts"},
                  "args": {"type": "object", "description": "该工具的参数，和单独调用时一样"}}}}}}


def _text(obj: Any) -> Dict[str, str]:
    return {"type": "text", "text": json.dumps(obj, ensure_ascii=False)}


def _reject(rid: Any, name: str, args: Dict[str, Any], reason: str, message: str) -> None:
    _append_call({"t": time.time(), "tool": name, "args": args, "ok": False, "rejected": reason})
    _send({"jsonrpc": "2.0", "id": rid,
           "result": {"content": [_text({"error": message, "budget": _budget()})], "isError": True}})


def _outputs(result: Dict[str, Any]) -> List[str]:
    files = [result[k] for k in ("file", "sheet") if isinstance(result.get(k), str)]
    return files


def _heartbeat(token: Any, name: str, stop: threading.Event) -> None:
    started = time.time()
    while not stop.wait(HEARTBEAT_SEC):
        sec = round(time.time() - started, 1)
        _send({"jsonrpc": "2.0", "method": "notifications/progress",
               "params": {"progressToken": token, "progress": sec, "message": "%s 已运行 %d 秒" % (name, sec)}})


def _out_key(args: Dict[str, Any]) -> str:
    """显式给了 out 的调用：同一个输出文件同一时刻只让一个调用写。"""
    out = args.get("out")
    if isinstance(out, str) and out.strip():
        try:
            return "out:" + str((WORKDIR / out).resolve())
        except (OSError, ValueError):
            return ""
    return ""


def _execute(i: int, name: str, args: Dict[str, Any], batch: Any = None):
    """真正执行一次已占好序号的调用，写日志，返回 (payload, images, is_error)。"""
    spec = backend.TOOLS[name]
    started = time.time()
    images: List[Path] = []
    entry: Dict[str, Any] = {"i": i, "t": started, "tool": name, "args": args,
                             "backend": backends.backend_name(name)}
    if batch is not None:
        entry["batch"] = batch
    pool_client.reset()
    with concurrency.call_context():
        try:
            key = _out_key(args)
            with (concurrency.keyed(key) if key else _NULL), concurrency.SLOTS.hold(name):
                entry["t_run"] = round(time.time(), 3)          # 拿到 GPU 槽位、开始执行的时刻
                if backends.mock_mode():
                    concurrency.simulate(name)
                result = spec["fn"](**args)
            images = [Path(p) for p in result.pop("_images", [])]
            entry.update({"ok": True, "backend": result.get("backend", entry["backend"]),
                          "outputs": _outputs(result)})
            payload: Dict[str, Any] = dict(result)
            is_error = False
        except TypeError as exc:
            payload = {"error": "参数错误: %s（参数见工具说明）" % exc}
            entry.update({"ok": False, "error": payload["error"]})
            is_error = True
        except backends.Unavailable as exc:
            payload = {"error": "该工具在本机不可用：%s" % exc}
            entry.update({"ok": False, "error": payload["error"], "unavailable": True})
            is_error = True
        except Exception as exc:  # noqa: BLE001
            payload = {"error": "工具执行失败: %s" % exc}
            entry.update({"ok": False, "error": str(exc)[:2000]})
            is_error = True
            _log_server("%s 失败：\n%s" % (name, traceback.format_exc()))
    leased = pool_client.last_lease()
    if leased:                                   # 工具池：哪个实例、什么时候租到（之前是在网关排队）、什么时候还
        entry.update(leased)
    end = time.time()
    entry["t_end"] = round(end, 3)
    entry["sec"] = round(end - started, 2)
    _append_call(entry)
    backend.cleanup_parts()
    return payload, images, is_error


class _Null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return False


_NULL = _Null()


def _start_heartbeat(params: Dict[str, Any], name: str):
    token = (params.get("_meta") or {}).get("progressToken")
    stop, beat = threading.Event(), None
    if token is not None and HEARTBEAT_SEC > 0:
        beat = threading.Thread(target=_heartbeat, args=(token, name, stop), daemon=True)
        beat.start()
    return stop, beat


def _image_blocks(images: List[Path]) -> List[Dict[str, Any]]:
    out = []
    for img in images:
        try:
            out.append({"type": "image", "mimeType": "image/jpeg",
                        "data": base64.b64encode(img.read_bytes()).decode("ascii")})
        except OSError:
            continue
    return out


def _tool_call(rid: Any, params: Dict[str, Any]) -> None:
    name = str(params.get("name") or "")
    args = params.get("arguments") or {}
    if name == "batch":
        _batch_call(rid, params, args)
        return
    if name not in backend.TOOLS:
        _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "未知工具: %s" % name}})
        return
    i, reason, message = _admit(name, args)
    if i is None:
        _reject(rid, name, args, reason, message)
        return
    stop, beat = _start_heartbeat(params, name)
    try:
        payload, images, is_error = _execute(i, name, args)
    finally:
        stop.set()
        if beat is not None:
            beat.join()                          # 心跳停了再回结果：结果之后才到的进度通知客户端不认
    payload["budget"] = _budget(i)
    _send({"jsonrpc": "2.0", "id": rid, "result": {"content": [_text(payload)] + _image_blocks(images),
                                                   "isError": is_error}})


def _batch_call(rid: Any, params: Dict[str, Any], args: Dict[str, Any]) -> None:
    calls = args.get("calls") if isinstance(args, dict) else None
    problem = ""
    if not isinstance(calls, list) or not calls:
        problem = "calls 必须是非空数组：[{\"tool\": \"i2v\", \"args\": {...}}, ...]"
    elif len(calls) > BATCH_MAX:
        problem = "一个 batch 最多 %d 条（收到 %d 条），分成几个 batch 提交" % (BATCH_MAX, len(calls))
    else:
        for k, c in enumerate(calls):
            tool = c.get("tool") if isinstance(c, dict) else None
            if not isinstance(c, dict) or not isinstance(tool, str) or not isinstance(c.get("args", {}), dict):
                problem = "calls[%d] 格式不对，应为 {\"tool\": 工具名, \"args\": {...}}" % k
            elif tool in NO_BATCH:
                problem = "calls[%d]：%s 不能放进 batch，请单独调用" % (k, tool)
            elif tool not in backend.TOOLS:
                problem = "calls[%d]：未知工具 %s" % (k, tool)
            if problem:
                break
    if problem:                                  # 整个 batch 格式错误：不计预算、不执行
        _send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [_text({"error": problem, "budget": _budget()})], "isError": True}})
        return
    with _STATE_LOCK:
        _BATCH_SEQ[0] += 1
        bid = "%d-%d" % (int(time.time()), _BATCH_SEQ[0])
    slots: List[Any] = []
    for c in calls:
        tool, cargs = c["tool"], c.get("args") or {}
        i, reason, message = _admit(tool, cargs)
        if i is None:
            _append_call({"t": time.time(), "tool": tool, "args": cargs, "ok": False, "rejected": reason, "batch": bid})
            slots.append({"tool": tool, "ok": False, "rejected": reason, "error": message})
        else:
            slots.append(_BATCH_EXEC.submit(_execute, i, tool, cargs, bid))
            slots[-1].call_i, slots[-1].tool = i, tool          # type: ignore[attr-defined]
    stop, beat = _start_heartbeat(params, "batch")
    results: List[Dict[str, Any]] = []
    images: List[Path] = []
    try:
        for slot in slots:
            if isinstance(slot, dict):
                results.append(slot)
                continue
            try:
                payload, imgs, is_error = slot.result()
            except Exception as exc:  # noqa: BLE001  _execute 自己兜住了异常，这里只防万一
                payload, imgs, is_error = {"error": "工具执行失败: %s" % exc}, [], True
            item: Dict[str, Any] = {"tool": slot.tool, "call": slot.call_i, "ok": not is_error}
            item.update(payload)
            room = BATCH_IMAGES - len(images)
            if imgs and room > 0:
                item["images"] = list(range(len(images) + 1, len(images) + 1 + min(room, len(imgs))))
                images += imgs[:room]
            elif imgs:
                item["images_omitted"] = len(imgs)
            results.append(item)
    finally:
        stop.set()
        if beat is not None:
            beat.join()
    ok = sum(1 for r in results if r.get("ok"))
    payload = {"batch": bid, "ok": ok, "failed": len(results) - ok, "results": results, "budget": _budget()}
    if images:
        payload["images_note"] = "下面的图片按顺序编号 1..%d，results[k].images 是该条对应的图片编号" % len(images)
    _send({"jsonrpc": "2.0", "id": rid, "result": {"content": [_text(payload)] + _image_blocks(images),
                                                   "isError": ok == 0}})


def _safe_call(rid: Any, params: Dict[str, Any]) -> None:
    try:
        _tool_call(rid, params)
    except Exception as exc:  # noqa: BLE001  线程里的意外：一定要给客户端回一个结果，否则它会一直等
        _log_server("tools/call 内部错误：\n%s" % traceback.format_exc())
        _send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [_text({"error": "服务端内部错误：%s" % exc, "budget": _budget()})], "isError": True}})


def main() -> None:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    # 起过的 blender / chromium 进程组记到运行目录里：退出时统一杀，run.py 收尾再兜一次
    procgroups.configure(Path(os.environ.get("STUDIO_RUN_DIR") or WORKDIR) / procgroups.NAME)
    procgroups.install("MCP server 退出")
    _log_server("start workdir=%s max_calls=%d used=%d mock=%s parallel=%d" % (WORKDIR, MAX_CALLS, USED,
                                                                             backends.mock_mode(), MAX_PARALLEL))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "studio", "version": "1.0.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_list()}})
        elif method == "tools/call":                 # 进线程池：同一条消息里的多个调用可以并行
            _CALL_EXEC.submit(_safe_call, rid, msg.get("params") or {})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None and not (method or "").startswith("notifications/"):
            _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "未实现: %s" % method}})
    _CALL_EXEC.shutdown(wait=True)               # stdin 关了：等在跑的调用写完日志再退
    _BATCH_EXEC.shutdown(wait=True)


if __name__ == "__main__":
    main()
