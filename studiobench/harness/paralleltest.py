#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP server 并发自测（mock 后端 + 模拟耗时，没 GPU 也能跑）。

    python3 -m studiobench.harness.paralleltest [--out 目录]

检查：batch 和同一时刻多个 tools/call 真的并行；同一 GPU 组按槽位排队、不同组互不挡；预算在并发下不超发、
序号 i 唯一连续；自动命名不撞名；显式同名 out 串行写；长调用期间 ping 照常回；重启后计数从日志恢复。
全部通过返回 0。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]


class Client:
    """异步 JSON-RPC 客户端：可以连发多个请求不等回复，回复按 id 对上。"""

    def __init__(self, env: Dict[str, str]):
        self.proc = subprocess.Popen([sys.executable, "-m", "studiobench.tools.server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=sys.stderr, text=True, env=env)
        self.next_id = 0
        self.replies: Dict[int, Dict[str, Any]] = {}
        self.cv = threading.Condition()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and msg.get("id") is not None:
                with self.cv:
                    self.replies[msg["id"]] = msg
                    self.cv.notify_all()

    def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        with self.cv:
            self.next_id += 1
            rid = self.next_id
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                                         ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return rid

    def wait(self, rid: int, timeout: float = 120) -> Dict[str, Any]:
        end = time.time() + timeout
        with self.cv:
            while rid not in self.replies:
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError("等不到 id=%d 的回复" % rid)
                self.cv.wait(left)
            return self.replies.pop(rid)

    def call(self, tool: str, args: Dict[str, Any]) -> int:
        return self.send("tools/call", {"name": tool, "arguments": args})

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=60)


def payload(reply: Dict[str, Any]) -> Dict[str, Any]:
    content = (reply.get("result") or {}).get("content") or []
    text = next((c["text"] for c in content if c.get("type") == "text"), "{}")
    return json.loads(text)


def images(reply: Dict[str, Any]) -> int:
    return sum(1 for c in (reply.get("result") or {}).get("content") or [] if c.get("type") == "image")


RESULTS: List[Any] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print("%s  %-44s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def start(work: Path, extra: Dict[str, str]) -> Client:
    env = dict(os.environ)
    env.update({"STUDIO_WORKDIR": str(work), "STUDIO_RUN_DIR": str(work.parent), "STUDIO_BACKEND": "mock",
                "PYTHONPATH": str(REPO), "PYTHONUNBUFFERED": "1", "STUDIO_HEARTBEAT_SEC": "0"})
    env.update(extra)
    c = Client(env)
    c.wait(c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="测试目录（默认临时目录）")
    args = ap.parse_args()
    root = Path(args.out or tempfile.mkdtemp(prefix="sb_par_")).resolve()
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    print("目录：%s" % root)
    D = 1.5
    base = {"STUDIO_MAX_CALLS": "200", "STUDIO_MOCK_DELAY": "i2v=%s,t2i=%s,tts=%s,sfx=%s,*=0" % (D, D, D, D)}

    # ---- 1. 本地槽位：同组排队，不同组并行；tools/list 里有 batch 和并发提示
    c = start(work, base)
    tools = c.wait(c.send("tools/list"))["result"]["tools"]
    names = {t["name"]: t for t in tools}
    check("tools/list 有 batch", "batch" in names)
    check("可并行工具带 readOnlyHint", (names["i2v"].get("annotations") or {}).get("readOnlyHint") is True
          and "annotations" not in names["export"])
    seed = payload(c.wait(c.call("t2i", {"prompt": "seed frame", "out": "kf/seed.png"})))
    check("准备首帧", seed.get("file") == "kf/seed.png", str(seed.get("error") or ""))
    # mock 后端会真用 ffmpeg 生成占位片，单次耗时 = 模拟延迟 + ffmpeg；先各量一次单独调用作基线
    one: Dict[str, float] = {}
    for tool, a in (("i2v", {"prompt": "base", "first_frame": "kf/seed.png"}), ("tts", {"text": "基线"}),
                    ("t2i", {"prompt": "base"})):
        t0 = time.time()
        c.wait(c.call(tool, a))
        one[tool] = time.time() - t0
    print("      单次基线：%s" % "，".join("%s %.1fs" % kv for kv in one.items()))
    t0 = time.time()
    r1, r2 = c.call("i2v", {"prompt": "a", "first_frame": "kf/seed.png"}), c.call("tts", {"text": "你好世界"})
    pair = [payload(c.wait(r1)).get("budget", {}).get("call"), payload(c.wait(r2)).get("budget", {}).get("call")]
    dt = time.time() - t0
    rows = {r["i"]: r for r in map(json.loads, (work / ".calls.jsonl").read_text(encoding="utf-8").splitlines())
            if r.get("i") in pair}
    a_, b_ = rows.get(pair[0]), rows.get(pair[1])
    both = bool(a_ and b_) and a_["t_run"] < b_["t_end"] and b_["t_run"] < a_["t_end"]
    check("不同组（H3 + 音频）同时跑：执行区间重叠", both, "%.1fs（串行要 %.1fs）" % (dt, one["i2v"] + one["tts"]))
    t0 = time.time()
    rs = [c.call("i2v", {"prompt": "q%d" % k, "first_frame": "kf/seed.png"}) for k in range(3)]
    ids = [payload(c.wait(r)).get("budget", {}).get("call") for r in rs]
    dt = time.time() - t0
    rows = {r["i"]: r for r in map(json.loads, (work / ".calls.jsonl").read_text(encoding="utf-8").splitlines())
            if r.get("i") in ids}
    spans = sorted((rows[i]["t_run"], rows[i]["t_end"]) for i in ids if i in rows)
    serial = len(spans) == 3 and all(b0 >= a1 - 0.01 for (_, a1), (b0, _) in zip(spans, spans[1:]))
    check("同组 H3 默认 1 槽：执行区间不重叠", serial and dt >= 3 * D, "%.1fs" % dt)
    t0 = time.time()
    rid = c.call("i2v", {"prompt": "long", "first_frame": "kf/seed.png"})
    time.sleep(0.2)
    pt = time.time()
    c.wait(c.send("ping"), timeout=5)
    check("长调用期间 ping 即时回", time.time() - pt < 0.5, "%.2fs" % (time.time() - pt))
    c.wait(rid)
    c.close()

    # ---- 2. 放开槽位（相当于工具池里有 8 个实例）：batch 和原生并行都应接近 1 倍耗时
    wide = dict(base, STUDIO_SLOTS_H3_FL2VA="8", STUDIO_SLOTS_T2I="8", STUDIO_SLOTS_AUDIO="8")
    c = start(work, wide)
    t0 = time.time()
    rep = c.wait(c.call("batch", {"calls": [{"tool": "i2v", "args": {"prompt": "shot %d" % k,
                                                                      "first_frame": "kf/seed.png"}}
                                            for k in range(8)]}))
    dt = time.time() - t0
    pb = payload(rep)
    check("batch 8 条 i2v 并行", pb.get("ok") == 8 and dt < 8 * one["i2v"] / 3,
          "%.1fs（串行约 %.1fs，快 %.1f 倍），ok=%s" % (dt, 8 * one["i2v"], 8 * one["i2v"] / dt, pb.get("ok")))
    calls_i = [r.get("call") for r in pb.get("results") or []]
    check("batch 结果按提交顺序、各有序号", calls_i == sorted(calls_i) and len(set(calls_i)) == 8, str(calls_i))
    files = [r.get("file") for r in pb.get("results") or []]
    check("自动命名不撞名", len(set(files)) == 8 and all(files), ", ".join(map(str, files)))
    t0 = time.time()
    rs = [c.call("t2i", {"prompt": "frame %d" % k}) for k in range(6)]
    reps = [c.wait(r) for r in rs]
    dt = time.time() - t0
    t2i_files = [payload(r).get("file") for r in reps]
    check("同一时刻 6 个 tools/call 并行", dt < 6 * one["t2i"] / 2.5,
          "%.1fs（串行约 %.1fs，快 %.1f 倍）" % (dt, 6 * one["t2i"], 6 * one["t2i"] / dt))
    check("并发 t2i 自动命名不撞名", len(set(t2i_files)) == 6, ", ".join(map(str, t2i_files)))
    rep = c.wait(c.call("batch", {"calls": [{"tool": "sample_frames", "args": {"video": "kf/seed.png"}}]
                                  + [{"tool": "t2i", "args": {"prompt": "x", "out": "same.png"}} for _ in range(3)]}))
    pb = payload(rep)
    check("batch 带图：图片随结果返回", images(rep) >= 1 and (pb["results"][0].get("images") == [1]),
          "images=%d" % images(rep))
    check("显式同名 out 并发：串行写、都成功", pb.get("ok") == 4, "ok=%s" % pb.get("ok"))
    bad = payload(c.wait(c.call("batch", {"calls": [{"tool": "export", "args": {"src": "x.mp4"}}]})))
    check("batch 里放 export 被拒（不计预算）", "error" in bad and "export" in bad["error"])
    c.close()

    # ---- 3. 预算在并发下不超发
    log = work / ".calls.jsonl"
    used_before = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if '"rejected"' not in line)
    c = start(work, dict(wide, STUDIO_MAX_CALLS=str(used_before + 5)))
    rep = c.wait(c.call("batch", {"calls": [{"tool": "tts", "args": {"text": "第%d句" % k}} for k in range(8)]}))
    pb = payload(rep)
    rej = [r for r in pb.get("results") or [] if r.get("rejected")]
    check("预算剩 5：batch 8 条只执行 5 条", pb.get("ok") == 5 and len(rej) == 3,
          "ok=%s rejected=%d" % (pb.get("ok"), len(rej)))
    rs = [c.call("tts", {"text": "再来"}) for _ in range(3)]
    over = [payload(c.wait(r)) for r in rs]
    check("预算用完后并发调用全被拒", all("预算" in (o.get("error") or "") for o in over))
    c.close()

    # ---- 4. 日志：序号唯一连续、t_run/t_end 齐全；重启后计数恢复
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    executed = [r for r in rows if not r.get("rejected")]
    ii = sorted(r["i"] for r in executed)
    check("序号 i 唯一且连续", ii == list(range(1, len(ii) + 1)), "%d 条" % len(ii))
    check("每条都有 t_run / t_end", all("t_run" in r and "t_end" in r for r in executed if r.get("ok")))
    overlap = 0
    ex = sorted(executed, key=lambda r: r["t"])
    for a, b in zip(ex, ex[1:]):
        overlap += b["t"] < a["t_end"]
    check("日志里能看到并行重叠", overlap > 5, "%d 处重叠" % overlap)
    c = start(work, dict(wide, STUDIO_MAX_CALLS=str(len(ii) + 1)))
    first = payload(c.wait(c.call("tts", {"text": "续跑"})))
    second = payload(c.wait(c.call("tts", {"text": "超了"})))
    check("重启后计数从日志恢复", first.get("budget", {}).get("call") == len(ii) + 1 and "预算" in (second.get("error") or ""),
          "call=%s" % first.get("budget", {}).get("call"))
    c.close()
    leftovers = list(work.glob("*.part.*"))
    check("没有残留 .part 临时文件", not leftovers, ", ".join(p.name for p in leftovers))

    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d 通过%s" % (len(RESULTS) - len(failed), len(RESULTS), ("；失败：" + "、".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
