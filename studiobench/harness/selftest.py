#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每个工具真调一次：经 stdio JSON-RPC 拉起 mcp_server.py，验证协议、后端、ffmpeg 链路和看图。

    python3 selftest.py --mock                     # 没 GPU：全部 mock
    python3 selftest.py                            # GPU 机：真实后端（H3 每段几分钟）
    python3 selftest.py --only t2i,tts,sfx         # 只测部分工具
    python3 selftest.py --skip ref_video,lipsync

前一步失败时用 ffmpeg 造的替身素材继续，保证后面的工具都能测到。
报告写到 <out>/selftest.json；所有被测工具都通过才返回 0。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools import media

REPO = Path(__file__).resolve().parents[2]

RUNS_DIR = os.environ.get("STUDIO_RUNS_DIR", str(Path.cwd() / "runs"))


class Server:
    def __init__(self, python: str, env: Dict[str, str]):
        self.proc = subprocess.Popen([python, "-m", "studiobench.tools.server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=sys.stderr, text=True, env=env)
        self.next_id = 0

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server 退出了（看上面的 stderr）")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="")
    ap.add_argument("--out")
    ap.add_argument("--python", default=os.environ.get("STUDIO_PYTHON") or sys.executable)
    args = ap.parse_args()

    out = Path(args.out or Path(RUNS_DIR) / "_selftest" / (time.strftime("%Y%m%d-%H%M%S")
                                                           + ("__mock" if args.mock else ""))).resolve()
    work = out / "work"
    (work / "inputs").mkdir(parents=True, exist_ok=True)
    only = {t for t in args.only.split(",") if t}
    skip = {t for t in args.skip.split(",") if t}

    # 替身素材：真实工具失败时让后续步骤还能跑
    media.run(["-y", "-f", "lavfi", "-i", "testsrc2=s=1344x768:d=1", "-frames:v", "1",
               str(work / "inputs" / "stand_in.png")])
    media.run(["-y", "-f", "lavfi", "-i", "testsrc2=s=1344x768:r=24:d=5", "-f", "lavfi", "-i",
               "sine=frequency=440:sample_rate=48000:duration=5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(work / "inputs" / "stand_in.mp4")])
    media.run(["-y", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=3",
               "-c:a", "pcm_s16le", str(work / "inputs" / "stand_in.wav")])

    env = dict(os.environ)
    env.update({"STUDIO_WORKDIR": str(work), "STUDIO_RUN_DIR": str(out), "STUDIO_MAX_CALLS": "200",
                "STUDIO_FFMPEG": media.FFMPEG, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(REPO)})
    if os.environ.get("STUDIO_CUDA_COMPAT"):          # 同 run.py：宿主驱动比 torch 旧时，不加 torch 看不到 GPU
        env["LD_LIBRARY_PATH"] = os.environ["STUDIO_CUDA_COMPAT"] + ":" + os.environ.get("LD_LIBRARY_PATH", "")
    if args.mock:
        env["STUDIO_BACKEND"] = "mock"
    else:
        env.pop("STUDIO_BACKEND", None)

    server = Server(args.python, env)
    server.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    listed = server.request("tools/list")["result"]["tools"]
    unavailable = {t["name"]: t["description"] for t in listed if t["description"].startswith("[本机不可用")}

    report: List[Dict[str, Any]] = []
    have: Dict[str, str] = {"img": "inputs/stand_in.png", "clip": "inputs/stand_in.mp4",
                            "voice": "inputs/stand_in.wav"}

    def call(tool: str, arguments: Dict[str, Any], keep: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if (only and tool not in only) or tool in skip:
            return None
        if tool in unavailable:                      # tools/list 已标明本机不可用：跳过，不算失败
            row = {"tool": tool, "ok": True, "skipped": True, "sec": 0.0, "backend": None,
                   "images": 0, "error": None, "reason": unavailable[tool][:160], "note": {}}
            report.append(row)
            print("%-4s %-13s  %s" % ("SKIP", tool, row["reason"]), flush=True)
            return None
        t0 = time.time()
        reply = server.request("tools/call", {"name": tool, "arguments": arguments})
        res = reply.get("result") or {}
        content = res.get("content") or []
        text = next((c["text"] for c in content if c.get("type") == "text"), "{}")
        payload = json.loads(text)
        images = [c for c in content if c.get("type") == "image"]
        ok = not res.get("isError") and "error" not in payload
        row = {"tool": tool, "ok": ok, "sec": round(time.time() - t0, 1), "backend": payload.get("backend"),
               "images": len(images), "error": payload.get("error"),
               "note": {k: (payload[k][:120] if isinstance(payload[k], str) else payload[k])
                        for k in ("file", "duration_s", "width", "height", "has_audio", "count", "text")
                        if k in payload}}
        report.append(row)
        print("%-4s %-13s %6.1fs  %-26s %s" % ("PASS" if ok else "FAIL", tool, row["sec"], row["backend"] or "",
                                               (row["error"] or json.dumps(row["note"], ensure_ascii=False))[:160]),
              flush=True)
        if ok and keep and payload.get("file"):
            have[keep] = payload["file"]
        return payload if ok else None

    call("t2i", {"prompt": "一位穿红色雨衣的女孩站在雨夜霓虹街头，电影感，35mm", "out": "st_t2i.png"}, keep="img")
    call("image_edit", {"src": have["img"], "prompt": "把雨衣换成黄色，其他保持不变", "out": "st_edit.png"},
         keep="img2")
    call("i2v", {"prompt": "女孩慢慢抬头看向镜头，雨滴落下", "first_frame": have["img"], "duration": 5,
                 "out": "st_i2v.mp4"}, keep="clip")
    call("i2v", {"prompt": "镜头缓慢推近", "first_frame": have["img"], "last_frame": have.get("img2", have["img"]),
                 "duration": 5, "out": "st_i2v_fl.mp4"}, keep="clip_fl")
    for key in ("clip", "clip_fl"):          # 人工看一眼：i2v 有没有动起来、尾帧是否接近 st_edit.png
        if have.get(key, "").startswith("st_"):
            call("sample_frames", {"video": have[key], "fps": 1})
    call("t2v", {"prompt": "清晨的海边，海浪拍打礁石，航拍缓慢前推", "duration": 5, "aspect": "16:9",
                 "out": "st_t2v.mp4"}, keep="clip2")
    call("ref_video", {"prompt": "<Image 1> 中的女孩在雨中奔跑", "references": [have["img"]], "duration": 5,
                       "aspect": "16:9", "out": "st_ref.mp4"})
    call("tts", {"text": "这是 StudioBench 的自检旁白，确认语音合成工作正常。", "out": "st_tts.wav"}, keep="voice")
    call("music", {"prompt": "温暖的钢琴与弦乐，舒缓", "duration": 10, "out": "st_music.wav"}, keep="music")
    call("sfx", {"video": have["clip"], "prompt": "雨声，远处车流", "out": "st_sfx.wav"}, keep="sfx")
    call("asr", {"audio": have["voice"]})
    call("search", {"query": "岳阳楼 始建 年代", "max_results": 3})
    call("lipsync", {"video": have["clip"], "audio": have["voice"], "out": "st_lipsync.mp4"})
    call("mix_audio", {"video": have["clip"], "audio": have.get("sfx", have["voice"]), "replace": True,
                       "out": "st_mix1.mp4"}, keep="mixed")
    call("mix_audio", {"video": have.get("mixed", have["clip"]), "audio": have["voice"], "start": 0.5,
                       "gain": 1.0, "out": "st_mix2.mp4"}, keep="mixed")
    call("concat", {"videos": [have.get("mixed", have["clip"]), have.get("clip2", have["clip"])],
                    "out": "st_concat.mp4"}, keep="cat")
    call("trim", {"video": have.get("cat", have["clip"]), "start": 1.0, "end": 4.5, "out": "st_trim.mp4"})
    call("overlay", {"video": have.get("cat", have["clip"]), "out": "st_sub.mp4",
                     "subtitles": [{"start": 0.2, "end": 2.5, "text": "自检字幕：中文显示正常吗？"}]}, keep="sub")
    call("sample_frames", {"video": have.get("sub", have["clip"]), "fps": 1})
    call("sample_frames", {"video": have["img"]})

    # ---- v2 新增：剪辑 / 音频 / 分析
    call("composite", {"video": have.get("cat", have["clip"]), "layer": have["img"], "position": "top-right",
                       "width": 320, "start": 0.5, "end": 3.0, "out": "st_comp.mp4"})
    call("transition", {"videos": [have["clip"], have.get("clip2", have["clip"])], "kind": "fade",
                        "duration": 0.5, "out": "st_xfade.mp4"})
    call("reframe", {"video": have["clip"], "width": 720, "height": 1280, "mode": "blur", "out": "st_reframe.mp4"})
    call("ken_burns", {"image": have["img"], "duration": 3, "pan": "left-to-right", "zoom_to": 1.2,
                       "width": 1280, "height": 720, "out": "st_kb.mp4"})
    call("speed", {"video": have["clip"], "factor": 1.5, "out": "st_speed.mp4"})
    call("audio_mix", {"tracks": [{"file": have.get("music", have["voice"]), "gain_db": -6},
                                  {"file": have["voice"], "start": 0.5}], "duck": 1, "normalize_lufs": -14,
                       "out": "st_amix.wav"})
    call("audio_edit", {"audio": have["voice"], "fade_in": 0.2, "fade_out": 0.5, "gain_db": -1,
                        "out": "st_aedit.wav"})
    call("scene_detect", {"video": have.get("cat", have["clip"])})
    call("beat_detect", {"audio": have.get("music", have["voice"])})
    call("color", {"video": have["clip"], "preset": "teal_orange", "out": "st_color.mp4"})
    call("fetch_url", {"url": "https://www.python.org/static/img/python-logo.png", "out": "st_fetch.png"})
    call("remove_bg", {"image": have["img"], "out": "st_cut.png"})
    # ---- v2 新增：代码渲染（HTML / three.js / Blender）
    call("render_html", {"html": "<!doctype html><html><head><meta charset=utf-8><style>"
                         "body{margin:0;background:#101820;overflow:hidden}h1{position:absolute;top:40%;left:0;"
                         "color:#fff;font:700 96px 'Noto Sans CJK SC',sans-serif;animation:m 2s linear forwards}"
                         "@keyframes m{from{transform:translateX(-60%)}to{transform:translateX(40%)}}</style></head>"
                         "<body><h1>StudioBench 自检</h1></body></html>",
                         "duration": 2, "fps": 24, "width": 1280, "height": 720, "out": "st_html.mp4"})
    call("render_html", {"html": "<!doctype html><html><head><meta charset=utf-8><style>body{margin:0;background:transparent}"
                         "div{position:absolute;left:60px;bottom:60px;padding:16px 28px;background:rgba(200,30,40,.85);"
                         "color:#fff;font:700 56px 'Noto Sans CJK SC',sans-serif;border-radius:12px}</style></head>"
                         "<body><div>透明标题</div></body></html>",
                         "duration": 2, "fps": 24, "width": 1344, "height": 768, "transparent": True,
                         "out": "st_title.mov"}, keep="title")
    call("composite", {"video": have["clip"], "layer": have.get("title", have["img"]), "out": "st_comp_title.mp4"})
    call("render_three", {"code": "export default async function setup({THREE, scene}) {"
                          " const m = new THREE.Mesh(new THREE.TorusKnotGeometry(1, 0.3, 128, 16),"
                          " new THREE.MeshStandardMaterial({color: 0x44aaff, metalness: 0.3, roughness: 0.4}));"
                          " scene.add(m); scene.add(new THREE.AmbientLight(0xffffff, 0.4));"
                          " const l = new THREE.DirectionalLight(0xffffff, 2); l.position.set(3, 4, 5); scene.add(l);"
                          " return (t) => { m.rotation.x = t * 0.8; m.rotation.y = t * 1.2; }; }",
                          "duration": 2, "fps": 24, "width": 960, "height": 540, "out": "st_three.mp4"})
    scene_py = ("import bpy\n"
                "bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()\n"   # 去掉默认立方体、灯、相机
                "bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 0))\n"
                "ob = bpy.context.object\n"
                "ob.rotation_euler = (0, 0, 0); ob.keyframe_insert('rotation_euler', frame=1)\n"
                "ob.rotation_euler = (0, 0, 3.14); ob.keyframe_insert('rotation_euler', frame=24)\n"
                "bpy.ops.object.light_add(type='SUN', location=(4, -4, 6))\n"
                "bpy.ops.object.camera_add(location=(0, -5, 1.5), rotation=(1.35, 0, 0))\n"
                "bpy.context.scene.camera = bpy.context.object\n")
    call("render_blender", {"code": scene_py, "frames": [1, 12, 24], "width": 640, "height": 360,
                            "engine": "cycles", "samples": 8})
    call("render_blender", {"code": scene_py, "start": 1, "end": 24, "fps": 24, "width": 640, "height": 360,
                            "engine": "cycles", "samples": 8, "out": "st_blender.mp4"})

    call("inspect", {"path": have.get("sub", have["clip"])})
    call("export", {"src": have.get("sub", have["clip"])})
    server.close()

    calls = [json.loads(line) for line in (work / ".calls.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = {"mock": args.mock, "out": str(out),
               "passed": sum(r["ok"] and not r.get("skipped") for r in report),
               "skipped": [r["tool"] for r in report if r.get("skipped")],
               "failed": [r["tool"] for r in report if not r["ok"]],
               "unavailable_listed": unavailable, "logged_calls": len(calls), "results": report}
    (out / "selftest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n%d/%d 通过，跳过 %d（本机不可用），报告：%s"
          % (summary["passed"], len(report) - len(summary["skipped"]), len(summary["skipped"]),
             out / "selftest.json"))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
