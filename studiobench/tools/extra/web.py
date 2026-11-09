# -*- coding: utf-8 -*-
"""v2：HTML / three.js 渲染。headless Chromium 逐帧截图，经管道交给 ffmpeg（帧不落盘）。

抓帧逻辑在 workers/web_render.mjs（时间接管在 workers/web/shim.js）。运行环境在共享盘工具箱
（STUDIO_TOOLBOX，默认 StudioBench/toolbox）：node/bin/node、web/node_modules.tar（playwright-core、three r170 等）、
ms-playwright/chromium_headless_shell-*.tar（含 syslibs/ 备份库）、fonts/（Noto Sans CJK SC 等 + fonts.conf）。
共享盘（FUSE）建小文件极慢（约 3 个/秒），所以库和浏览器在共享盘上只放 tar，每台机器第一次调用时解到本机
STUDIO_TOOLBOX_CACHE（默认 /tmp/studiobench-toolbox），之后复用（tar 的大小或修改时间变了会重新解）。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import concurrency, media
from .. import procgroups
from ._base import TOOLBOX, R

NAME = "chromium-headless"
WORKER = Path(__file__).resolve().parents[1] / "workers" / "web_render.mjs"
NODE = TOOLBOX / "node" / "bin" / "node"
LIB_TAR = TOOLBOX / "web" / "node_modules.tar"
BROWSERS_DIR = TOOLBOX / "ms-playwright"
FONTS_CONF = TOOLBOX / "fonts" / "fonts.conf"
CACHE = Path(os.environ.get("STUDIO_TOOLBOX_CACHE") or "/tmp/studiobench-toolbox")
MAX_SIDE = 3840
MAX_DURATION = 600                     # 单次调用最多录 10 分钟；更长的分段再 concat
LIBS = ("three（含 three/addons/）、gsap、d3、katex、p5、animejs、chart.js、pixi.js、matter-js、lottie-web、"
        "simplex-noise、@tweenjs/tween.js")


def _browser_tar() -> Optional[Path]:
    hits = sorted(BROWSERS_DIR.glob("chromium_headless_shell-*.tar"))
    return hits[-1] if hits else None


def check():
    """便宜的就绪检查：node、库 tar、浏览器 tar、worker 都在（不解包、不启动浏览器）。"""
    missing = []
    if not NODE.is_file():
        missing.append("node/bin/node")
    if not LIB_TAR.is_file():
        missing.append("web/node_modules.tar")
    tar = _browser_tar()
    if tar is None:
        missing.append("ms-playwright/chromium_headless_shell-*.tar")
    if not WORKER.is_file():
        missing.append("workers/web_render.mjs")
    if missing:
        return False, "工具箱缺 %s（%s）" % ("、".join(missing), TOOLBOX)
    gl = "GPU（STUDIO_WEB_GPU=1）" if os.environ.get("STUDIO_WEB_GPU") == "1" else "SwiftShader"
    return True, "%s · WebGL=%s · %s" % (tar.stem, gl, TOOLBOX)


def _unpack(tar: Path, name: str) -> Path:
    """把共享盘上的 tar 解到本机缓存（按大小和修改时间区分版本）；并发调用时只有一个会落地，其余直接复用。"""
    st = tar.stat()
    dest = CACHE / ("%s-%d-%d" % (name, st.st_size, int(st.st_mtime)))
    if (dest / ".ok").is_file():
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / (".%s.%d.tmp" % (dest.name, os.getpid()))
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        subprocess.run(["tar", "xf", str(tar), "-C", str(tmp)], check=True, timeout=900,
                       stdin=subprocess.DEVNULL, capture_output=True)
        (tmp / ".ok").write_text("ok", encoding="utf-8")
        try:
            tmp.rename(dest)
        except OSError:                         # 另一个进程先解好了
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not (dest / ".ok").is_file():
        raise RuntimeError("解包失败：%s → %s" % (tar, dest))
    return dest


def _localize(src: Path, name: str) -> Path:
    """共享盘上的可执行文件拷到本机缓存再跑：从 FUSE 直接 exec 每次要 1–3.5 秒（node 125MB、ffmpeg 80MB 实测）。"""
    st = src.stat()
    dest = CACHE / ("%s-%d-%d" % (name, st.st_size, int(st.st_mtime))) / src.name
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(".%s.%d.tmp" % (src.name, os.getpid()))
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o755)
    tmp.replace(dest)
    return dest


_SHARED_FS = tuple(p for p in os.environ.get("STUDIO_SLOW_FS_PREFIXES", "").split(os.pathsep) if p)


def _ffmpeg() -> str:
    path = Path(media.FFMPEG)
    if str(path).startswith(_SHARED_FS) and path.is_file():
        return str(_localize(path, "ffmpeg"))
    return str(path)


_ENV_CACHE: Optional[Dict[str, str]] = None


def _env() -> Dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is None:
        ok, detail = check()
        if not ok:
            raise RuntimeError(detail)
        libs = _unpack(LIB_TAR, "web")
        browsers = _unpack(_browser_tar(), "ms-playwright")
        extra = {"STUDIO_WEB_LIBDIR": str(libs / "node_modules"), "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
                 "_NODE": str(_localize(NODE, "node")), "STUDIO_FFMPEG": _ffmpeg()}
        shell = next(iter(sorted(browsers.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/"
                                                "chrome-headless-shell"))), None)
        if shell is not None and (browsers / "syslibs").is_dir():
            try:                                # 本机缺 chromium 依赖的系统库时，用 tar 里备份的那一份
                out = subprocess.run(["ldd", str(shell)], capture_output=True, text=True, timeout=30).stdout
                if "not found" in out:
                    extra["_SYSLIBS"] = str(browsers / "syslibs")
            except (OSError, subprocess.SubprocessError):
                pass
        _ENV_CACHE = extra
    env = dict(os.environ)
    env.update({k: v for k, v in _ENV_CACHE.items() if not k.startswith("_")})
    env["STUDIO_TOOLBOX"] = str(TOOLBOX)
    if FONTS_CONF.is_file():
        env.setdefault("FONTCONFIG_FILE", str(FONTS_CONF))
    if _ENV_CACHE.get("_SYSLIBS"):
        env["LD_LIBRARY_PATH"] = _ENV_CACHE["_SYSLIBS"] + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def _auto_dir(parent: str, prefix: str) -> Path:
    d = concurrency.reserve_name(R().WORKDIR / parent, prefix)    # 并发调用不会拿到同一个目录
    d.mkdir(parents=True)
    return d


def _even(v: Any, name: str) -> int:
    try:
        x = int(round(float(v)))
    except (TypeError, ValueError):
        raise ValueError("%s 必须是整数像素" % name) from None
    if not 16 <= x <= MAX_SIDE:
        raise ValueError("%s 要在 16–%d 之间（收到 %s）" % (name, MAX_SIDE, v))
    return x - (x % 2)


def _run_worker(cfg: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    reg = R()
    env = _env()
    cfg = dict(cfg, ffmpeg=env["STUDIO_FFMPEG"])
    proc = subprocess.Popen([_ENV_CACHE["_NODE"], str(WORKER), "--config", "-"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(reg.WORKDIR),
                            start_new_session=True, text=True)
    procgroups.register(proc)        # 独立进程组：server 被强杀时靠这份清单收尾
    try:
        out, err = proc.communicate(json.dumps(cfg, ensure_ascii=False), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise RuntimeError("渲染超时（%d 分钟），浏览器已结束。减少时长/分辨率，或调大 timeout_min" % (timeout_s // 60))
    finally:
        try:                                   # 不留僵尸：连同 chromium、ffmpeg 子进程一起收掉
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        procgroups.release(proc)
    lines = [ln for ln in (out or "").splitlines() if ln.strip().startswith("{")]
    if not lines:
        return {"ok": False, "error": "渲染进程没有返回结果（退出码 %s）：%s" % (proc.returncode, (err or "")[-1500:])}
    try:
        return json.loads(lines[-1])
    except ValueError:
        return {"ok": False, "error": "渲染进程返回无法解析：%s" % lines[-1][:500]}


def _flatten(src: Path) -> Path:
    """透明帧铺到棋盘格上再进联系表，不然透明处看起来是黑的。"""
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    tile = 16
    bg = Image.new("RGBA", im.size, (200, 200, 200, 255))
    dark = Image.new("RGBA", (tile, tile), (150, 150, 150, 255))
    for y in range(0, im.size[1], tile):
        for x in range((y // tile) % 2 * tile, im.size[0], tile * 2):
            bg.paste(dark, (x, y))
    bg.alpha_composite(im)
    out = src.with_suffix(".view.jpg")
    bg.convert("RGB").save(out, quality=88)
    return out


def _sheet(items: List[Dict[str, Any]], out: Path, transparent: bool) -> Optional[Path]:
    files = [Path(x["file"]) for x in items if Path(x["file"]).is_file()]
    if not files:
        return None
    if transparent:
        files = [_flatten(f) for f in files]
    labels = ["%.2fs" % float(x["t"]) for x in items][:len(files)]
    if len(files) == 1:
        return media.downscale_image(files[0], out)
    return media.contact_sheet(files, labels, out)


def _diagnostics(res: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("blocked_requests", "missing_files", "console_errors", "page_errors", "warnings"):
        if res.get(key):
            out[key] = res[key]
    if res.get("rewritten"):
        out["cdn_rewritten"] = len(res["rewritten"])
    return out


def _fail(res: Dict[str, Any]) -> RuntimeError:
    msg = str(res.get("error") or "渲染失败")
    extra = []
    if res.get("blocked_requests"):
        extra.append("被拦截的外部请求：%s（外网不可用；已收录的库直接 import，见工具说明）" % "，".join(res["blocked_requests"][:5]))
    if res.get("missing_files"):
        extra.append("找不到的文件：%s" % "，".join(res["missing_files"][:5]))
    if res.get("console_errors"):
        extra.append("console.error：%s" % " | ".join(res["console_errors"][:3]))
    return RuntimeError(msg + ("；" + "；".join(extra) if extra else ""))


def render_html(entry: Optional[str] = None, html: Optional[str] = None, duration: Optional[float] = None,
                fps: float = 30, width: int = 1920, height: int = 1080, transparent: bool = False,
                times: Optional[List[float]] = None, query: Optional[str] = None, out: Optional[str] = None,
                timeout_min: float = 30, allow_errors: bool = False, workers: Optional[int] = None) -> Dict[str, Any]:
    reg = R()
    if bool(entry) == bool(html):
        raise ValueError("entry（工作目录里的 .html）和 html（内联页面）二选一")
    if html:
        page = _auto_dir("web", "page") / "index.html"
        page.write_text(html, encoding="utf-8")
    else:
        page = reg._in(entry)
        if page.suffix.lower() not in (".html", ".htm"):
            raise ValueError("entry 要是 .html 文件（three.js 场景脚本用 render_three）")
    w, h = _even(width, "width"), _even(height, "height")
    fps_f = float(fps)
    if not 1 <= fps_f <= 60:
        raise ValueError("fps 要在 1–60 之间")
    dur = None if duration is None else float(duration)
    if dur is not None and not 0 < dur <= MAX_DURATION:
        raise ValueError("duration 要在 (0, %d] 秒之间；更长的片子分段渲染再 concat" % MAX_DURATION)
    tmin = float(timeout_min or 30)
    if not 1 <= tmin <= 120:
        raise ValueError("timeout_min 要在 1–120 之间")
    rel_page = reg._rel(page)
    cfg: Dict[str, Any] = {"root": str(reg.WORKDIR), "entry": rel_page, "query": query or "", "duration": dur,
                           "fps": fps_f, "width": w, "height": h, "transparent": bool(transparent),
                           "allowErrors": bool(allow_errors), "timeoutSec": int(tmin * 60), "ffmpeg": media.FFMPEG,
                           "maxDuration": MAX_DURATION}
    if workers is not None:
        cfg.update({"workers": max(1, min(int(workers), 8)), "workersExplicit": True})

    if times:                                                   # ---- 预览静帧
        ts = sorted({round(float(t), 3) for t in times})
        if len(ts) > 48 or ts[0] < 0:
            raise ValueError("times 最多 48 个、不能为负")
        stills_dir = reg.WORKDIR / "frames" / ("%s_preview" % page.parent.name if html else page.stem + "_preview")
        stills_dir = concurrency.reserve_name(stills_dir.parent, stills_dir.name)
        cfg.update({"times": ts, "stillsDir": str(stills_dir)})
        res = _run_worker(cfg, tmin * 60 + 120)
        if not res.get("ok"):
            raise _fail(res)
        stills = res.get("stills") or []
        sheet = _sheet(stills, stills_dir / "sheet.jpg", bool(transparent))
        payload: Dict[str, Any] = {"page": rel_page, "backend": NAME, "mode": res.get("mode"),
                                   "stills": [{"t": round(float(s["t"]), 3), "file": reg._rel(Path(s["file"]))} for s in stills],
                                   "width": w, "height": h, "render_seconds": round(float(res.get("seconds") or 0), 1)}
        if sheet is not None:
            payload.update({"sheet": reg._rel(sheet), "_images": [sheet]})
        payload.update(_diagnostics(res))
        return payload

    ext = ".mov" if transparent else ".mp4"
    path = reg._out(out, "render_html", ext)
    if transparent and path.suffix.lower() != ".mov":
        raise ValueError("transparent=true 输出带 alpha 的 .mov，out 要以 .mov 结尾")
    if not transparent and path.suffix.lower() not in (".mp4", ".mov"):
        raise ValueError("out 要以 .mp4 结尾")
    part = reg._part(path)
    sheet_dir = reg.WORKDIR / "frames" / path.stem
    cfg.update({"out": str(part), "sheetDir": str(sheet_dir), "sheetCount": 8})
    res = _run_worker(cfg, tmin * 60 + 120)
    if not res.get("ok") or not part.is_file():
        for leftover in part.parent.glob(part.name + "*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise _fail(res)
    reg._commit(part, path)
    sheet = _sheet(res.get("sheet") or [], sheet_dir / "sheet.jpg", bool(transparent))
    extra: Dict[str, Any] = {"backend": NAME, "page": rel_page, "mode": res.get("mode"), "frames": res.get("frames"),
                             "fps": fps_f, "transparent": bool(transparent), "workers": res.get("workers"),
                             "render_seconds": round(float(res.get("seconds") or 0), 1)}
    if sheet is not None:
        extra.update({"sheet": reg._rel(sheet), "_images": [sheet]})
    extra.update(_diagnostics(res))
    return reg._clip_info(path, extra)


_THREE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>render_three</title>
<style>html,body{margin:0;width:100%%;height:100%%;overflow:hidden;background:%(bg)s}canvas{display:block}</style>
</head><body>
<script>window.READY = false;</script>
<script type="module">
import * as THREE from 'three';
const W = innerWidth, H = innerHeight, TRANSPARENT = %(transparent)s;
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: TRANSPARENT, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(W, H);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
if (TRANSPARENT) renderer.setClearColor(0x000000, 0);
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 2000);
camera.position.set(0, 1.5, 6);
camera.lookAt(0, 0, 0);
const ctx = { THREE, scene, camera, renderer, width: W, height: H };
const mod = await import(%(scene)s);
const setup = mod.default ?? mod.setup;
const ret = typeof setup === 'function' ? await setup(ctx) : null;
const update = typeof ret === 'function' ? ret
  : (ret && typeof ret.update === 'function') ? ret.update
  : (typeof mod.update === 'function' ? mod.update : null);
const draw = ret && typeof ret.render === 'function' ? ret.render : null;
const dur = ret && typeof ret.duration === 'number' ? ret.duration : (typeof mod.DUR === 'number' ? mod.DUR : null);
if (dur != null) window.DUR = dur;
window.render = (t) => {
  if (update) update(t);
  if (draw) draw(t); else renderer.render(scene, (ret && ret.camera) || ctx.camera);
};
window.render(0);
window.READY = true;
</script></body></html>
"""


def render_three(code: Optional[str] = None, entry: Optional[str] = None, duration: Optional[float] = None,
                 fps: float = 30, width: int = 1920, height: int = 1080, transparent: bool = False,
                 times: Optional[List[float]] = None, out: Optional[str] = None, background: Optional[str] = None,
                 timeout_min: float = 30, allow_errors: bool = False, workers: Optional[int] = None) -> Dict[str, Any]:
    reg = R()
    if bool(code) == bool(entry):
        raise ValueError("code（内联 ES module）和 entry（工作目录里的 .js）二选一")
    page_dir = _auto_dir("web", "three")
    if code:
        scene = page_dir / "scene.js"
        scene.write_text(code, encoding="utf-8")
        scene_url = "./scene.js"
    else:
        scene = reg._in(entry)
        if scene.suffix.lower() not in (".js", ".mjs"):
            raise ValueError("entry 要是 .js / .mjs 场景脚本")
        scene_url = "/" + reg._rel(scene)
    bg = background or ("transparent" if transparent else "#000")
    if not all(ch.isalnum() or ch in "#(),. %-" for ch in bg):
        raise ValueError("background 只接受 CSS 颜色，如 #101820")
    page = page_dir / "index.html"
    page.write_text(_THREE_PAGE % {"bg": bg, "transparent": "true" if transparent else "false",
                                   "scene": json.dumps(scene_url)}, encoding="utf-8")
    res = render_html(entry=reg._rel(page), duration=duration, fps=fps, width=width, height=height,
                      transparent=transparent, times=times, out=out, timeout_min=timeout_min,
                      allow_errors=allow_errors, workers=workers)
    res["scene"] = reg._rel(scene)
    return res


def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_STR = {"type": "string"}
_NUM = {"type": "number"}
_COMMON = {
    "duration": {"type": "number", "description": "时长（秒，≤600）；不给就用页面的 window.DUR"},
    "fps": {"type": "number", "description": "帧率，默认 30"},
    "width": {"type": "integer", "description": "宽（像素，偶数），默认 1920"},
    "height": {"type": "integer", "description": "高（像素，偶数），默认 1080"},
    "transparent": {"type": "boolean", "description": "透明背景：输出带 alpha 的 .mov，可叠到别的视频上"},
    "times": {"type": "array", "items": _NUM, "description": "只出这些时刻（秒）的预览静帧和联系表，不出视频"},
    "out": {"type": "string", "description": "输出文件（相对路径，.mp4；透明时 .mov），不给就自动编号"},
    "timeout_min": {"type": "number", "description": "单次渲染上限（分钟），默认 30"},
    "allow_errors": {"type": "boolean", "description": "页面脚本报错也继续录（默认报错即失败并返回错误）"},
    "workers": {"type": "integer", "description": "并行浏览器数（render(t) 页面默认 3；虚拟时钟页面默认 1）"},
}

TOOLS: Dict[str, Dict[str, Any]] = {
    "render_html": {
        "fn": render_html, "backend": NAME, "check": check,
        "desc": ("把 HTML 页面（HTML/CSS/SVG/Canvas/WebGL/three.js）逐帧渲染成视频，或用 times 出预览静帧（返回联系表图片）。"
                 "页面写在工作目录里用 entry 指定，或用 html 直接传。页面定义 window.render(t)（t 为秒；可选 window.DUR 时长、"
                 "异步加载时先设 window.READY=false、加载完设 true）就逐帧调用它；否则用虚拟时钟录 requestAnimationFrame、"
                 "CSS 动画、Web Animations、GSAP、setTimeout，帧帧精确、结果可复现（Math.random 已固定种子）。"
                 "离线前端库已配好 import map，直接 import：" + LIBS + "；unpkg/jsdelivr/esm.sh/cdnjs 上这些库的地址会改走本地，"
                 "其他外网请求一律拦截（结果里的 blocked_requests）。工作目录里的文件用相对路径或 / 开头的路径引用。"
                 "中文字体 Noto Sans CJK SC，西文 DejaVu Sans。transparent=true 出带 alpha 的 .mov（字幕条、片名、角标、"
                 "数据图叠到视频上）。页面里的 <video> 在无头浏览器里不随时间推进，要把视频和图形合成就用透明图层再叠加。"
                 "输出无声，声音用 tts/music/sfx + mix_audio。"),
        "schema": _obj(dict({"entry": {"type": "string", "description": "工作目录里的 .html"},
                             "html": {"type": "string", "description": "内联 HTML（落盘到 web/page_NNN/index.html）"},
                             "query": {"type": "string", "description": "传给页面的 location.search，如 scene=2"}},
                            **_COMMON), []),
    },
    "render_three": {
        "fn": render_three, "backend": NAME, "check": check,
        "desc": ("three.js（r170）场景渲染成视频或预览静帧，是 render_html 的便捷封装。code（或 entry 指向工作目录里的 .js）是一个 "
                 "ES module：export default async function setup({THREE, scene, camera, renderer, width, height}) "
                 "{ 搭场景…; return (t) => { 按 t 秒更新物体和相机 } }。也可以返回 {update, render, camera, duration}："
                 "render(t) 自己画（如 EffectComposer 后期），camera 换相机，duration 定时长。默认 WebGLRenderer 抗锯齿、"
                 "ACES 色调映射、sRGB、软阴影，透视相机在 (0,1.5,6) 看向原点。three/addons/ 下的扩展（"
                 "GLTFLoader、RGBELoader、EffectComposer、UnrealBloomPass、TextGeometry、FontLoader 等）可直接 import；"
                 "贴图、模型用工作目录里的文件（如 '/inputs/a.png'）。WebGL 默认软件渲染（SwiftShader），场景太重会慢。"),
        "schema": _obj(dict({"code": {"type": "string", "description": "场景 ES module 源码（落盘到 web/three_NNN/scene.js）"},
                             "entry": {"type": "string", "description": "工作目录里的场景脚本 .js"},
                             "background": {"type": "string", "description": "页面底色（CSS 颜色），默认黑；透明时无"}},
                            **_COMMON), []),
    },
}
