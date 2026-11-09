# -*- coding: utf-8 -*-
"""sb pool：按卡数规划、起服务、看状态、停服务。

    ./sb-gpu pool plan   --gpus 0-15                       # 只打印布局
    ./sb-gpu pool up     --gpus 0-15 --profile 8gpu-1node  # 起 H3 实例 + 辅助模型服务 + 网关，写 pool.env
    ./sb-gpu pool status
    ./sb-gpu pool down
    ./sb     pool up     --gpus 8 --fake                   # 没 GPU：假 H3 + 替身 worker，测调度和并发
    ./sb     pool profile                                  # 从历次运行统计需求权重

up 之后 runner 读 runtime/pool/<节点>/pool.env：STUDIO_POOL_URL（网关）、STUDIO_POOL_LANES（同时跑几道题），
以及兼容旧配置的 H3_ENDPOINT / H3_REF2VA_ENDPOINT（第一个实例）。
H3 用 deploy/lib/start_h3.sh 起（H3_INSTANCE/H3_GPUS/H3_TP/H3_USP/H3_PORT 覆盖 profile），各实例并行加载；
辅助模型用 STUDIO_PYTHON 起 workerd，按物理卡号设 STUDIO_*_DEVICE / STUDIO_GPU_AUDIO（不设 CUDA_VISIBLE_DEVICES）。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import planner
from .catalog import VRAM_GB

REPO = Path(__file__).resolve().parents[2]
SB = Path(os.environ.get("STUDIO_HOME", str(REPO)))
START_H3 = SB / "deploy" / "lib" / "start_h3.sh"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEVICE_ENV = {"t2i": "STUDIO_T2I_DEVICE", "image_edit": "STUDIO_EDIT_DEVICE", "asr": "STUDIO_ASR_DEVICE"}
AUDIO = ("tts", "sfx", "music", "lipsync")


def state_dir(arg: Optional[str]) -> Path:
    return Path(arg) if arg else REPO / "runtime" / "pool" / socket.gethostname().split(".")[0]


def _health(url: str, timeout: float = 3) -> Optional[Dict[str, Any]]:
    try:
        with _NOPROXY.open(urllib.request.Request(url + "/health"), timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}") if resp.status == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _port_free(port: int, bind: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind, port))
            return True
        except OSError:
            return False


def _assign_ports(layout: Dict[str, Any], bind: str, reuse_h3: bool) -> List[str]:
    """规划的端口被别的服务占了（训练机上常见）就往后找空端口。已经在跑的 H3（start_h3.sh 会直接复用）不挪。"""
    moved: List[str] = []
    taken: set = set()

    def pick(port: int, what: str) -> int:
        p = port
        while p in taken or not _port_free(p, bind):
            p += 1
        taken.add(p)
        if p != port:
            moved.append("%s :%d → :%d（原端口被占用）" % (what, port, p))
        return p

    for x in layout["h3"]:
        if reuse_h3 and _health("http://127.0.0.1:%d" % x["port"]) is not None:
            taken.add(x["port"])
            continue
        x["port"] = pick(x["port"], x["name"])
    for w in layout["workers"]:
        w["port"] = pick(w["port"], w["name"])
    layout["gateway_port"] = pick(layout["gateway_port"], "gateway")
    return moved


def _spawn(argv: List[str], env: Dict[str, str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = log.open("ab")
    proc = subprocess.Popen(argv, env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, cwd=str(REPO))
    return proc.pid


def _worker_env(base: Dict[str, str], w: Dict[str, Any]) -> Dict[str, str]:
    env = dict(base)
    env.pop("CUDA_VISIBLE_DEVICES", None)             # 物理卡号直接写进各自的设备变量
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONUNBUFFERED"] = "1"
    pool = w["pool"]
    if pool in DEVICE_ENV:
        env[DEVICE_ENV[pool]] = "cuda:%d" % w["gpu"]
    if pool in ("t2i", "image_edit"):
        env["STUDIO_IMAGE_OFFLOAD"] = "model" if w.get("offload") else "none"
    if pool in AUDIO:
        env["STUDIO_GPU_AUDIO"] = str(w["gpu"])
        env["STUDIO_TTS_PERSISTENT"] = "1"            # 常驻服务：TTS 模型只加载一次
    if env.get("STUDIO_CUDA_COMPAT"):
        env["LD_LIBRARY_PATH"] = env["STUDIO_CUDA_COMPAT"] + ":" + env.get("LD_LIBRARY_PATH", "")
    return env


def up(args: argparse.Namespace) -> int:
    out = state_dir(args.state)
    weights = json.loads(Path(args.weights).read_text(encoding="utf-8")) if args.weights else None
    try:
        layout = planner.plan(planner.parse_gpus(args.gpus), args.vram, weights, args.lanes, args.h3_size, args.ref2va)
    except planner.PlanError as exc:
        print("规划失败：%s" % exc)
        return 2
    print(planner.describe(layout), "\n", flush=True)
    if not args.fake and not args.profile:
        print("真实部署要给 --profile（deploy/profiles 下的名字，用来找 H3 权重、解释器、cuda-compat）")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    for note in _assign_ports(layout, "0.0.0.0" if args.bind == "0.0.0.0" else args.bind, not args.fake):
        print("端口：" + note)
    host = args.host
    for x in layout["h3"] + layout["workers"]:
        x["url"] = "http://%s:%d" % (host, x["port"])
    (out / "layout.json").write_text(json.dumps(layout, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    pids: Dict[str, int] = {}
    py = sys.executable
    base = dict(os.environ)

    # ---- H3 实例（真实部署时 start_h3.sh 自己等就绪，最长 30 分钟；各实例并行起）
    errors: List[str] = []

    def start_h3(x: Dict[str, Any]) -> None:
        env = dict(base, PROFILE=args.profile, H3_INSTANCE=x["name"], H3_GPUS=",".join(map(str, x["gpus"])),
                   H3_TP=str(x["tp"]), H3_USP=str(x["usp"]), H3_PORT=str(x["port"]))
        rc = subprocess.run(["bash", str(START_H3), x["variant"]], env=env).returncode
        if rc != 0:
            errors.append("%s 起不来（start_h3.sh 返回 %d）" % (x["name"], rc))

    threads = []
    for x in layout["h3"]:
        if args.fake:
            pids[x["name"]] = _spawn([py, "-m", "studiobench", "pool", "worker", "--fake-h3", "--port", str(x["port"]),
                                      "--name", x["name"], "--fake", str(args.fake_h3_sec), "--bind", args.bind,
                                      "--cache", str(out / "fake_cache")],
                                     dict(base, PYTHONPATH=str(REPO)), out / "logs" / (x["name"] + ".log"))
        else:
            t = threading.Thread(target=start_h3, args=(x,))
            t.start()
            threads.append(t)

    # ---- 辅助模型服务
    for w in layout["workers"]:
        if args.fake:
            argv = [py, "-m", "studiobench", "pool", "worker", "--tool", w["pool"], "--port", str(w["port"]),
                    "--name", w["name"], "--fake", str(args.fake_sec), "--bind", args.bind]
        else:
            argv = [os.environ.get("STUDIO_PYTHON") or py, "-m", "studiobench", "pool", "worker", "--tool", w["pool"],
                    "--port", str(w["port"]), "--name", w["name"], "--warm", "--bind", args.bind]
        pids[w["name"]] = _spawn(argv, _worker_env(base, w), out / "logs" / (w["name"] + ".log"))

    # ---- 网关
    pids["gateway"] = _spawn([py, "-m", "studiobench", "pool", "gateway", "--layout", str(out / "layout.json"),
                              "--port", str(layout["gateway_port"]), "--bind", args.bind],
                             dict(base, PYTHONPATH=str(REPO)), out / "logs" / "gateway.log")
    (out / "pids.json").write_text(json.dumps(pids, indent=1) + "\n", encoding="utf-8")

    # ---- 等就绪
    gw = "http://%s:%d" % (host, layout["gateway_port"])
    deadline = time.time() + args.timeout
    pending = {x["name"]: x["url"] for x in layout["workers"]}
    if args.fake:
        pending.update({x["name"]: x["url"] for x in layout["h3"]})
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            h = _health(url)
            if h is not None and h.get("warm") in (None, "ready", "skip"):
                pending.pop(name)
        if pending:
            time.sleep(3)
    for t in threads:
        t.join()
    if pending:
        errors.append("这些实例 %d 秒内没就绪：%s（看 %s/logs/）" % (args.timeout, "、".join(pending), out))
    while time.time() < deadline and _health(gw) is None:
        time.sleep(1)

    fl = [x for x in layout["h3"] if x["pool"] == "h3_fl2va"]
    ref = [x for x in layout["h3"] if x["pool"] == "h3_ref2va"]
    lines = ["# sb pool up 生成（%s）" % time.strftime("%F %T"),
             "STUDIO_POOL_URL=%s" % gw, "STUDIO_POOL_LANES=%d" % layout["lanes"],
             "STUDIO_SLOTS_EXPORT=1"]
    if fl:
        lines.append("H3_ENDPOINT=%s" % fl[0]["url"])
    if ref:
        lines.append("H3_REF2VA_ENDPOINT=%s" % ref[0]["url"])
    (out / "pool.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("状态目录：%s\n池网关：%s\n同时跑 %d 道题；runner 读 %s/pool.env" % (out, gw, layout["lanes"], out))
    for e in errors:
        print("问题：" + e)
    time.sleep(2 if args.fake else 0)
    show_status(gw)
    return 1 if errors else 0


def _register(gw: str, x: Dict[str, Any], kind: str) -> bool:
    body = json.dumps({"pool": x["pool"], "name": x["name"], "url": x["url"], "kind": kind}).encode()
    req = urllib.request.Request(gw + "/register", body, {"Content-Type": "application/json"}, method="POST")
    try:
        with _NOPROXY.open(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def up_cluster(args: argparse.Namespace) -> int:
    """多机：所有节点按同一份全局布局各起自己那几张卡上的实例，0 号节点起网关，各节点把实例登记到网关。

    每个节点各跑一次（--node-rank 0..N-1），--nodes 按 rank 顺序给各节点 IP；状态目录在共享盘、各节点共用。"""
    nodes = [x.strip() for x in args.nodes.split(",") if x.strip()]
    rank, gpn = args.node_rank, args.gpus_per_node
    if not 0 <= rank < len(nodes):
        print("--node-rank 要在 0..%d 之间" % (len(nodes) - 1))
        return 2
    weights = json.loads(Path(args.weights).read_text(encoding="utf-8")) if args.weights else None
    try:
        layout = planner.plan(list(range(len(nodes) * gpn)), args.vram, weights, args.lanes, args.h3_size, args.ref2va)
    except planner.PlanError as exc:
        print("规划失败：%s" % exc)
        return 2
    out = state_dir(args.state)
    mine_dir = out / ("node%d" % rank)
    mine_dir.mkdir(parents=True, exist_ok=True)
    lo, hi = rank * gpn, (rank + 1) * gpn
    for x in layout["h3"]:
        if (min(x["gpus"]) < lo) != (max(x["gpus"]) < lo) or (min(x["gpus"]) < hi) != (max(x["gpus"]) < hi):
            print("规划出错：%s 跨了节点（卡 %s）" % (x["name"], x["gpus"]))
            return 2
    h3 = [dict(x, node=rank, gpus=[g - lo for g in x["gpus"]]) for x in layout["h3"] if lo <= x["gpus"][0] < hi]
    workers = [dict(w, node=rank, gpu=w["gpu"] - lo) for w in layout["workers"] if lo <= w["gpu"] < hi]
    mine = {"h3": h3, "workers": workers, "gateway_port": layout["gateway_port"]}
    if rank == 0:
        print(planner.describe(layout), "\n", flush=True)
        (out / "layout.json").write_text(json.dumps(layout, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    moved = _assign_ports(mine, "0.0.0.0", True)
    for note in moved:
        print("端口：" + note)
    ip = nodes[rank]
    for x in h3 + workers:
        x["url"] = "http://%s:%d" % (ip, x["port"])
    (mine_dir / "mine.json").write_text(json.dumps(mine, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("节点 %d（%s）：H3 %s；辅助 %s" % (rank, ip, "、".join(x["name"] for x in h3) or "无",
                                         "、".join(w["name"] for w in workers) or "无"), flush=True)
    py = sys.executable
    base = dict(os.environ)
    pids: Dict[str, int] = {}
    env_file = out / "pool.env"
    if rank == 0:                                              # 网关：只在 0 号节点
        gw_port = mine["gateway_port"]
        gw = "http://%s:%d" % (nodes[0], gw_port)
        pids["gateway"] = _spawn([py, "-m", "studiobench", "pool", "gateway", "--port", str(gw_port), "--bind", "0.0.0.0",
                                  "--persist", str(out / "registry.json")],
                                 dict(base, PYTHONPATH=str(REPO)), mine_dir / "logs" / "gateway.log")
        lanes_per_node = -(-layout["lanes"] // len(nodes))
        fl = [x for x in layout["h3"] if x["pool"] == "h3_fl2va"]
        env_file.write_text("\n".join(["# sb pool up --nodes 生成（%s），%d 个节点 × %d 卡" % (time.strftime("%F %T"), len(nodes), gpn),
                                       "STUDIO_POOL_URL=%s" % gw, "STUDIO_POOL_LANES=%d" % lanes_per_node,
                                       "STUDIO_SLOTS_EXPORT=1",
                                       "H3_ENDPOINT=http://%s:%d" % (nodes[0], fl[0]["port"])]) + "\n", encoding="utf-8")
    else:                                                      # 其它节点等 0 号写出 pool.env（网关地址）
        t0 = time.time()
        while not env_file.exists() and time.time() - t0 < 900:
            time.sleep(5)
        if not env_file.exists():
            print("等不到 0 号节点的 %s" % env_file)
            return 1
    gw = next(l.split("=", 1)[1] for l in env_file.read_text(encoding="utf-8").splitlines()
              if l.startswith("STUDIO_POOL_URL="))
    threads, errors = [], []

    def start_h3(x: Dict[str, Any]) -> None:
        env = dict(base, PROFILE=args.profile, H3_INSTANCE=x["name"], H3_GPUS=",".join(map(str, x["gpus"])),
                   H3_TP=str(x["tp"]), H3_USP=str(x["usp"]), H3_PORT=str(x["port"]), H3_BIND="0.0.0.0")
        if subprocess.run(["bash", str(START_H3), x["variant"]], env=env).returncode != 0:
            errors.append("%s 起不来" % x["name"])

    for x in h3:
        t = threading.Thread(target=start_h3, args=(x,))
        t.start()
        threads.append(t)
    for w in workers:
        argv = [os.environ.get("STUDIO_PYTHON") or py, "-m", "studiobench", "pool", "worker", "--tool", w["pool"],
                "--port", str(w["port"]), "--name", w["name"], "--warm", "--bind", "0.0.0.0"]
        pids[w["name"]] = _spawn(argv, _worker_env(base, w), mine_dir / "logs" / (w["name"] + ".log"))
    (mine_dir / "pids.json").write_text(json.dumps(pids, indent=1) + "\n", encoding="utf-8")
    t0 = time.time()                                           # 网关起来后先全部登记：没就绪的实例网关探活不过，不会分配
    while time.time() - t0 < 300:
        if _health(gw) is not None and all(_register(gw, x, "h3") for x in h3) and \
                all(_register(gw, w, "worker") for w in workers):
            break
        time.sleep(5)
    else:
        errors.append("登记不到网关 %s" % gw)
    deadline = time.time() + args.timeout
    pending = {w["name"]: w["url"] for w in workers}
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            h = _health(url)
            if h is not None and h.get("warm") in (None, "ready", "skip"):
                pending.pop(name)
        if pending:
            time.sleep(5)
    for t in threads:
        t.join()
    if pending:
        errors.append("这些实例没就绪：%s" % "、".join(pending))
    print("节点 %d 完成：网关 %s；%s" % (rank, gw, "问题：" + "；".join(errors) if errors else "全部就绪"), flush=True)
    show_status(gw)
    return 1 if errors else 0


def show_status(gw: str) -> int:
    try:
        with _NOPROXY.open(urllib.request.Request(gw + "/status"), timeout=5) as resp:
            st = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print("网关连不上（%s）：%s" % (gw, exc))
        return 1
    print("%-11s %5s %5s %5s %5s  %s" % ("池", "实例", "在线", "忙", "排队", "各题占用"))
    for name, p in st["pools"].items():
        print("%-11s %5d %5d %5d %5d  %s" % (name, p["instances"], p["healthy"], p["busy"], p["waiting"],
                                              "，".join("%s×%d" % (Path(r).name[:28], n) for r, n in p["runs"].items())))
    return 0


def _gateway_url(args: argparse.Namespace) -> str:
    if os.environ.get("STUDIO_POOL_URL"):
        return os.environ["STUDIO_POOL_URL"]
    env = state_dir(args.state) / "pool.env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        if line.startswith("STUDIO_POOL_URL="):
            return line.split("=", 1)[1]
    return "http://127.0.0.1:30100"


def down(args: argparse.Namespace) -> int:
    out = state_dir(args.state)
    if getattr(args, "node_rank", None) is not None and (out / ("node%d" % args.node_rank)).is_dir():
        out = out / ("node%d" % args.node_rank)
    pf = out / "pids.json"
    pids = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
    layout_f = out / "mine.json" if (out / "mine.json").exists() else out / "layout.json"
    layout = json.loads(layout_f.read_text(encoding="utf-8")) if layout_f.exists() else {}
    log_dir = Path(os.environ.get("STUDIO_LOCAL_ROOT", "/root")) / "studio_deploy"
    for x in layout.get("h3") or []:                 # 真实 H3：start_h3.sh 写的 pid 文件
        p = log_dir / ("%s.pid" % x["name"])
        if p.exists() and x["name"] not in pids:
            try:
                pids[x["name"]] = int(p.read_text().strip())
            except ValueError:
                pass
    for name, pid in pids.items():
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            print("停 %s（pid %d）" % (name, pid))
        except (ProcessLookupError, PermissionError):
            pass
    if pf.exists():
        pf.unlink()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv else ""
    rest = argv[1:]
    if sub == "plan":
        return planner.main(rest)
    if sub == "profile":
        from . import profile
        sys.argv = ["sb pool profile"] + rest
        return profile.main()
    if sub == "gateway":
        from . import gateway
        return gateway.main(rest)
    if sub == "worker":
        from . import workerd
        return workerd.main(rest)
    ap = argparse.ArgumentParser(prog="sb pool " + (sub or ""), description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--state", help="状态目录（默认 runtime/pool/<主机名>）")
    if sub == "up":
        ap.add_argument("--gpus", required=True)
        ap.add_argument("--profile", help="deploy/profiles 下的配置名（真实部署必填）")
        ap.add_argument("--vram", type=float, default=VRAM_GB)
        ap.add_argument("--lanes", type=int)
        ap.add_argument("--h3-size", type=int, choices=(2, 4, 8))
        ap.add_argument("--ref2va", choices=("auto", "on", "off"), default="auto")
        ap.add_argument("--weights")
        ap.add_argument("--host", default="127.0.0.1", help="别的机器要访问时填本机内网 IP")
        ap.add_argument("--bind", default="127.0.0.1", help="监听地址；多机共享时用 0.0.0.0")
        ap.add_argument("--timeout", type=int, default=2400, help="等实例就绪的秒数")
        ap.add_argument("--fake", action="store_true", help="假 H3 + 替身 worker（没 GPU 测调度用）")
        ap.add_argument("--fake-sec", type=float, default=3.0)
        ap.add_argument("--fake-h3-sec", type=float, default=8.0)
        ap.add_argument("--nodes", help="多机：各节点 IP，按 rank 顺序逗号分隔；给了就按全局布局只起本节点那部分")
        ap.add_argument("--node-rank", type=int, default=0)
        ap.add_argument("--gpus-per-node", type=int, default=8)
        a = ap.parse_args(rest)
        return up_cluster(a) if a.nodes else up(a)
    if sub == "status":
        return show_status(_gateway_url(ap.parse_args(rest)))
    if sub == "down":
        ap.add_argument("--node-rank", type=int)
        return down(ap.parse_args(rest))
    print(__doc__)
    return 2
