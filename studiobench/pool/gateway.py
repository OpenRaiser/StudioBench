# -*- coding: utf-8 -*-
"""节点网关：登记工具实例，按租约把空闲实例分给调用方（各道题的 MCP server）。

    python -m studiobench pool gateway --layout pool/layout.json [--port 30100]

协议（全部 JSON，POST 体 / GET 查询）：
  POST /acquire {pool, run, key, wait}   长轮询最多 wait 秒（默认 25）：拿到 → {lease, url, name}；没拿到 → {wait: true, position}
                                         调用方用同一个 key 继续轮询，排队位置不丢
  POST /renew   {lease}                   续租；租约 LEASE_TTL 秒（默认 90）没续就收回（调用方崩了也不会把实例占死）
  POST /release {lease}
  POST /register {pool, name, url, kind}  加实例（launch 起完服务后登记；多机时别的机器上的实例也可以登记进来）
  POST /unregister {name}                 删掉已停掉的实例登记；手上有租约的拒绝（{ok: false, busy: true}），先 park 等它做完
  GET  /status                            各池实例数、忙闲、排队、每道题占用
  GET  /health

分配：空出一个实例时，在等这个池的请求里挑「这道题当前在这个池占的实例最少」的，同样多就先来先得，
多道题同时跑时不会有一道题把实例全占住。实例每 15 秒探一次 /health，不健康的不分配。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .catalog import PORT_GATEWAY

LEASE_TTL = float(os.environ.get("STUDIO_POOL_LEASE_TTL", "90"))      # 调用方每 20 秒续一次
WAITER_TTL = float(os.environ.get("STUDIO_POOL_WAITER_TTL", "90"))
HEALTH_EVERY = 15.0
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Pool:
    def __init__(self, persist: Optional[Path] = None) -> None:
        self.persist = persist                                # 登记过的实例存盘：网关重启后不用各节点重新登记
        self.cv = threading.Condition()
        self.instances: Dict[str, Dict[str, Any]] = {}      # name → {pool, url, kind, healthy, lease, served, busy_s}
        self.leases: Dict[str, Dict[str, Any]] = {}          # lease → {pool, name, run, t0, renewed}
        self.waiters: Dict[str, Dict[str, Any]] = {}         # key → {pool, run, t0, seen, got}
        self.seq = itertools.count(1)
        self.started = time.time()

    # ------------------------------------------------------------------ 登记
    def register(self, pool: str, name: str, url: str, kind: str = "worker", healthy: bool = False) -> None:
        with self.cv:
            url = url.rstrip("/")
            base = name.split("@")[0]
            for other in [n for n, x in self.instances.items()
                          if n != name and x["url"] == url and x["lease"] is None and n.split("@")[0] != base]:
                del self.instances[other]                 # 同一个 URL 就是同一个进程：换名重起的实例占了旧端口，旧名字要去掉，否则一个进程会被当成两个实例分出去
                                                          # 例外：<实例>@<角色> 是故意同址的两个登记（图像双用进程同时挂 t2i 和 image_edit），不能互删
            old = self.instances.get(name)
            self.instances[name] = {"pool": pool, "url": url, "kind": kind,
                                    "healthy": healthy if old is None else old["healthy"],
                                    "lease": old["lease"] if old else None,
                                    "served": old["served"] if old else 0, "busy_s": old["busy_s"] if old else 0.0}
            self._save()
            self.cv.notify_all()

    def unregister(self, name: str) -> Dict[str, Any]:
        with self.cv:
            x = self.instances.get(name)
            if x is None:
                return {"ok": True, "missing": True}
            if x["lease"] is not None:
                return {"ok": False, "busy": True}
            del self.instances[name]
            self._save()
            return {"ok": True}

    def _save(self) -> None:
        """调用时已持有 cv。登记表落盘，网关重启后按它恢复。"""
        if self.persist is None:
            return
        rows = [{"pool": x["pool"], "name": n, "url": x["url"], "kind": x["kind"]} for n, x in self.instances.items()]
        tmp = self.persist.with_suffix(".part")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.persist)

    # ------------------------------------------------------------------ 分配
    def _held(self, pool: str, run: str) -> int:
        return sum(1 for l in self.leases.values() if l["pool"] == pool and l["run"] == run)

    def _assign(self, pool: str) -> None:
        """调用时已持有 cv。把这个池的空闲健康实例分给排队的请求。"""
        while True:
            free = [n for n, x in self.instances.items() if x["pool"] == pool and x["healthy"] and x["lease"] is None]
            waiting = [(k, w) for k, w in self.waiters.items() if w["pool"] == pool and w["got"] is None]
            if not free or not waiting:
                return
            key, w = min(waiting, key=lambda kw: (self._held(pool, kw[1]["run"]), kw[1]["t0"]))
            name = min(free, key=lambda n: self.instances[n]["served"])     # 轮着用，别总压一个实例
            lease = "L%d" % next(self.seq)
            now = time.time()
            self.instances[name]["lease"] = lease
            self.instances[name]["served"] += 1
            self.leases[lease] = {"pool": pool, "name": name, "run": w["run"], "t0": now, "renewed": now}
            w["got"] = lease
            self.cv.notify_all()

    def acquire(self, pool: str, run: str, key: str, wait: float) -> Dict[str, Any]:
        end = time.time() + max(0.0, min(wait, 60.0))
        with self.cv:
            if not any(x["pool"] == pool for x in self.instances.values()):
                return {"error": "没有 %s 池" % pool, "pools": sorted({x["pool"] for x in self.instances.values()})}
            w = self.waiters.get(key)
            if w is None:
                w = self.waiters[key] = {"pool": pool, "run": run, "t0": time.time(), "seen": time.time(), "got": None}
            w["seen"] = time.time()
            self._assign(pool)
            while w["got"] is None:
                left = end - time.time()
                if left <= 0:
                    queued = sorted((x for x in self.waiters.values() if x["pool"] == pool and x["got"] is None),
                                    key=lambda x: x["t0"])
                    return {"wait": True, "position": next((k for k, x in enumerate(queued) if x is w), 0) + 1}
                self.cv.wait(left)
                w["seen"] = time.time()
            lease = w["got"]
            del self.waiters[key]
            inst = self.leases[lease]["name"]
            return {"lease": lease, "url": self.instances[inst]["url"], "name": inst, "ttl": LEASE_TTL}

    def renew(self, lease: str) -> Dict[str, Any]:
        with self.cv:
            l = self.leases.get(lease)
            if l is None:
                return {"expired": True}
            l["renewed"] = time.time()
            return {"ok": True}

    def release(self, lease: str) -> Dict[str, Any]:
        with self.cv:
            l = self.leases.pop(lease, None)
            if l is None:
                return {"expired": True}
            inst = self.instances.get(l["name"])
            if inst is not None and inst["lease"] == lease:
                inst["lease"] = None
                inst["busy_s"] += time.time() - l["t0"]
            self._assign(l["pool"])
            return {"ok": True}

    # ------------------------------------------------------------------ 维护
    def reap(self) -> None:
        now = time.time()
        with self.cv:
            for lease, l in list(self.leases.items()):
                if now - l["renewed"] > LEASE_TTL:
                    self.leases.pop(lease)
                    inst = self.instances.get(l["name"])
                    if inst is not None and inst["lease"] == lease:
                        inst["lease"] = None
                    self._assign(l["pool"])
            for key, w in list(self.waiters.items()):
                if w["got"] is None and now - w["seen"] > WAITER_TTL:
                    del self.waiters[key]
                elif w["got"] is not None and now - w["seen"] > WAITER_TTL:   # 分到了但调用方再没来取：退回
                    self.waiters.pop(key)
                    self.release(w["got"])

    def probe(self, only_down: bool = False) -> None:
        with self.cv:
            targets = [(n, x["url"], x["kind"]) for n, x in self.instances.items() if not (only_down and x["healthy"])]
        for name, url, kind in targets:
            ok = _healthy(url, kind)
            with self.cv:
                inst = self.instances.get(name)
                if inst is not None and inst["healthy"] != ok:
                    inst["healthy"] = ok
                    if ok:
                        self._assign(inst["pool"])

    def status(self) -> Dict[str, Any]:
        with self.cv:
            pools: Dict[str, Dict[str, Any]] = {}
            for name, x in sorted(self.instances.items()):
                p = pools.setdefault(x["pool"], {"instances": 0, "healthy": 0, "busy": 0, "waiting": 0, "runs": {},
                                                 "members": []})
                p["instances"] += 1
                p["healthy"] += int(x["healthy"])
                p["busy"] += int(x["lease"] is not None)
                p["members"].append({"name": name, "url": x["url"], "healthy": x["healthy"],
                                     "busy": x["lease"] is not None, "served": x["served"],
                                     "busy_s": round(x["busy_s"], 1)})
            for w in self.waiters.values():
                if w["got"] is None and w["pool"] in pools:
                    pools[w["pool"]]["waiting"] += 1
            for l in self.leases.values():
                runs = pools[l["pool"]]["runs"]
                runs[l["run"]] = runs.get(l["run"], 0) + 1
            return {"uptime_s": round(time.time() - self.started), "pools": pools}


def _healthy(url: str, kind: str) -> bool:
    try:
        with _NOPROXY.open(urllib.request.Request(url + "/health"), timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def make_handler(pool: Pool):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002     # 不往 stderr 刷每个请求
            return

        def _reply(self, obj: Dict[str, Any], code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._reply({"ok": True})
            elif path == "/status":
                self._reply(pool.status())
            else:
                self._reply({"error": "未知路径"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._reply({"error": "不是 JSON"}, 400)
                return
            path = urlparse(self.path).path
            try:
                if path == "/acquire":
                    self._reply(pool.acquire(str(req["pool"]), str(req.get("run") or "?"),
                                             str(req.get("key") or "k%d" % next(pool.seq)), float(req.get("wait", 25))))
                elif path == "/renew":
                    self._reply(pool.renew(str(req["lease"])))
                elif path == "/release":
                    self._reply(pool.release(str(req["lease"])))
                elif path == "/register":
                    pool.register(str(req["pool"]), str(req["name"]), str(req["url"]), str(req.get("kind", "worker")))
                    self._reply({"ok": True})
                elif path == "/unregister":
                    self._reply(pool.unregister(str(req["name"])))
                else:
                    self._reply({"error": "未知路径"}, 404)
            except KeyError as exc:
                self._reply({"error": "缺参数 %s" % exc}, 400)

    return Handler


def load_layout(pool: Pool, layout: Dict[str, Any], host: str = "127.0.0.1") -> None:
    for x in layout.get("h3") or []:
        pool.register(x["pool"], x["name"], x.get("url") or "http://%s:%d" % (host, x["port"]), "h3")
    for x in layout.get("workers") or []:
        pool.register(x["pool"], x["name"], x.get("url") or "http://%s:%d" % (host, x["port"]), "worker")


def serve(port: int, layout: Optional[Dict[str, Any]] = None, bind: str = "0.0.0.0",
          persist: Optional[Path] = None) -> None:
    pool = Pool(persist)
    if persist is not None and persist.exists():           # 上次登记过的实例（多机：各节点 POST /register 进来的）
        for x in json.loads(persist.read_text(encoding="utf-8")):
            pool.register(x["pool"], x["name"], x["url"], x.get("kind", "worker"))
    if layout:
        load_layout(pool, layout)

    def loop(fn, every: float) -> None:
        while True:
            try:
                fn()
            except Exception:  # noqa: BLE001  维护线程不能死
                pass
            time.sleep(every)

    threading.Thread(target=loop, args=(pool.probe, HEALTH_EVERY), daemon=True).start()
    threading.Thread(target=loop, args=(lambda: pool.probe(only_down=True), 2.0), daemon=True).start()   # 刚起、刚恢复的实例尽快可用
    threading.Thread(target=loop, args=(pool.reap, 5.0), daemon=True).start()
    server = ThreadingHTTPServer((bind, port), make_handler(pool))
    server.daemon_threads = True
    print("gateway 监听 %s:%d，%d 个实例" % (bind, port, len(pool.instances)), flush=True)
    server.serve_forever()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="工具池节点网关")
    ap.add_argument("--layout", help="planner 写出的 layout.json（里面的实例全部登记）")
    ap.add_argument("--port", type=int, default=PORT_GATEWAY)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--persist", help="登记表存盘文件（多机工具池用）")
    args = ap.parse_args(argv)
    layout = json.loads(Path(args.layout).read_text(encoding="utf-8")) if args.layout else None
    serve(args.port, layout, args.bind, Path(args.persist) if args.persist else None)
    return 0
