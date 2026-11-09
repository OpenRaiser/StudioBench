#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具池端到端自测（假 H3 + 替身 worker，没 GPU 也能跑）。

    python3 -m studiobench.harness.pooltest [--out 目录] [--gpus 16]

起一套假池（sb pool up --fake），两个 MCP server 模拟两道题同时交 batch，检查：
跑满所有实例、两道题公平轮转（谁也不被饿着）、同一实例不会同时接两个活、调用方崩溃后租约收回、
工具状态里显示池信息。结束时 sb pool down。全部通过返回 0。
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
from typing import Any, Dict, List

from .paralleltest import Client, check, payload, RESULTS

REPO = Path(__file__).resolve().parents[2]
H3_SEC, WORKER_SEC = 6.0, 3.0


def sb(*args: str, env: Dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "studiobench", *args], env=env, cwd=str(REPO),
                          capture_output=True, text=True, timeout=600)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--gpus", default="16")
    args = ap.parse_args()
    root = Path(args.out or tempfile.mkdtemp(prefix="sb_pool_")).resolve()
    state = root / "pool"
    base = dict(os.environ, PYTHONPATH=str(REPO), STUDIO_POOL_LEASE_TTL="12", STUDIO_POOL_WAITER_TTL="30")
    base.pop("STUDIO_BACKEND", None)
    print("目录：%s" % root)
    up = sb("pool", "up", "--gpus", args.gpus, "--fake", "--fake-sec", str(WORKER_SEC), "--fake-h3-sec", str(H3_SEC),
            "--state", str(state), "--timeout", "120", env=base)
    print(up.stdout[-1500:], up.stderr[-1500:])
    check("pool up --fake 起来", up.returncode == 0 and (state / "pool.env").exists())
    layout = json.loads((state / "layout.json").read_text(encoding="utf-8"))
    n_fl = sum(1 for x in layout["h3"] if x["pool"] == "h3_fl2va")
    n_t2i = sum(1 for x in layout["workers"] if x["pool"] == "t2i")
    pool_env = dict(l.split("=", 1) for l in (state / "pool.env").read_text().splitlines() if "=" in l and l[0] != "#")
    try:
        return _run(root, base, pool_env, n_fl, n_t2i)
    finally:
        sb("pool", "down", "--state", str(state), env=base)


def _run(root: Path, base: Dict[str, str], pool_env: Dict[str, str], n_fl: int, n_t2i: int) -> int:
    # ---- 两道题：各自的工作目录、运行目录（= 网关看到的 run id）
    clients: List[Client] = []
    for name in ("A", "B"):
        work = root / name / "work"
        work.mkdir(parents=True, exist_ok=True)
        env = dict(base, **pool_env)
        env.update({"STUDIO_WORKDIR": str(work), "STUDIO_RUN_DIR": str(root / name), "STUDIO_MAX_CALLS": "200",
                    "PYTHONUNBUFFERED": "1", "STUDIO_HEARTBEAT_SEC": "0", "H3_POLL": "1",
                    "STUDIO_MOCK_DELAY": ""})
        c = Client(env)
        c.wait(c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
        clients.append(c)
    tools = {t["name"]: t for t in clients[0].wait(clients[0].send("tools/list"))["result"]["tools"]}
    check("i2v 由工具池服务、标为可用", not tools["i2v"]["description"].startswith("[本机不可用"),
          tools["i2v"]["description"][:60])

    seed = [payload(c.wait(c.call("t2i", {"prompt": "seed", "out": "kf/seed.png"}))) for c in clients]
    check("经池生成首帧", all(s.get("file") == "kf/seed.png" for s in seed), str([s.get("error") for s in seed]))
    check("返回里带实例名", all(str(s.get("instance", "")).startswith("t2i-") for s in seed),
          str([s.get("instance") for s in seed]))

    # ---- 两道题同时各交 6 段 i2v：总共 12 段，n_fl 个实例，每实例串行 H3_SEC 秒
    per = 6
    done: Dict[str, float] = {}
    res: Dict[str, Dict[str, Any]] = {}
    t0 = time.time()

    def go(c: Client, tag: str) -> None:
        rep = c.wait(c.call("batch", {"calls": [{"tool": "i2v", "args": {"prompt": "%s-%d" % (tag, k),
                                                                         "first_frame": "kf/seed.png"}}
                                                for k in range(per)]}), timeout=600)
        done[tag] = time.time() - t0
        res[tag] = payload(rep)

    ths = [threading.Thread(target=go, args=(c, tag)) for c, tag in zip(clients, "AB")]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    ideal = -(-2 * per // n_fl) * H3_SEC
    serial = 2 * per * H3_SEC
    check("两道题 12 段 i2v 全部成功", all(res[t].get("ok") == per for t in "AB"),
          str({t: (res[t].get("ok"), res[t].get("failed")) for t in "AB"}))
    total = max(done.values())
    check("跑满 %d 个 H3 实例" % n_fl, total < ideal * 1.6 + 6,
          "%.0fs（理想 %.0fs，单实例串行 %.0fs）" % (total, ideal, serial))
    check("两道题公平：完成时间接近", abs(done["A"] - done["B"]) <= 2 * H3_SEC + 3,
          "A %.0fs，B %.0fs" % (done["A"], done["B"]))
    used = {r.get("instance") for t in "AB" for r in res[t].get("results") or []}
    check("用到了每个 H3 实例", len(used) == n_fl, "、".join(sorted(map(str, used))))

    # ---- 同一实例不会同时接两个活：看各题日志里同一实例的执行区间
    spans: Dict[str, List[Any]] = {}
    for tag in "AB":
        for line in (root / tag / "work" / ".calls.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("instance") and r.get("t_lease"):
                spans.setdefault(r["instance"], []).append((r["t_lease"], r["t_release"]))
    clash = 0
    for s in spans.values():
        s.sort()
        clash += sum(1 for a, b in zip(s, s[1:]) if b[0] < a[1] - 0.05)
    check("同一实例没有被同时租出去", clash == 0 and len(spans) >= n_fl,
          "%d 处冲突，%d 个实例有租用记录" % (clash, len(spans)))

    # ---- 调用方崩溃：子进程租走 t2i 实例后被 kill -9，租约到期后实例回到池里
    gw = pool_env["STUDIO_POOL_URL"]
    holder = subprocess.Popen([sys.executable, "-c", (
        "import os,time;from studiobench.pool import client\n"
        "with client.lease('t2i'):\n    print('got', flush=True); time.sleep(600)")],
        env=dict(base, STUDIO_POOL_URL=gw, STUDIO_POOL_RUN="crasher", STUDIO_POOL_RENEW="3"),
        stdout=subprocess.PIPE, text=True, cwd=str(REPO))
    assert holder.stdout
    holder.stdout.readline()
    holder.kill()
    t1 = time.time()
    rep = payload(clients[0].wait(clients[0].call("t2i", {"prompt": "after crash"}), timeout=120))
    waited = time.time() - t1
    if n_t2i == 1:
        check("调用方崩溃后租约收回", bool(rep.get("file")) and 8 < waited < 40, "等了 %.0fs（租约 12s）" % waited)
    else:
        check("调用方崩溃后仍能用池", bool(rep.get("file")), "等了 %.0fs" % waited)

    # ---- sb pool status
    st = sb("pool", "status", env=dict(base, STUDIO_POOL_URL=gw))
    print(st.stdout)
    check("pool status 能看到各池", "h3_fl2va" in st.stdout and "t2i" in st.stdout)
    for c in clients:
        c.close()
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d 通过%s" % (len(RESULTS) - len(failed), len(RESULTS), ("；失败：" + "、".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
