# -*- coding: utf-8 -*-
"""render_blender：用 Blender 5.2.0 跑模型写的 bpy 场景脚本，出静帧预览或整段无声视频。

脚本负责搭场景（模型可以用 shell 先写好 .py，也可以把代码内联传进来）；入口脚本
（workers/blender_entry.py）执行完它以后，只覆盖调用里显式给了的参数，再渲染：
· 给了 frames：只渲这些帧，返回联系表；帧原图留在 blender/preview_NNN/，可以直接当 i2v 首帧
· 没给 frames：渲 frame_start..frame_end 的帧序列（先写节点本地临时目录），ffmpeg 编码后立即删帧；
  不透明出 mp4（H.264 yuv420p），transparent 出 .mov（ProRes 4444，带 alpha，可再叠到别的画面上）
Cycles 固定 seed；device=auto 时先找 CUDA（H20 没有 RT 核，不默认 OptiX），找不到就用 CPU。

安装：共享盘只放官方安装包 toolbox/downloads/blender-5.2.0-linux-x64.tar.xz（共享盘建小文件太慢，
不在上面解包）。第一次调用时加 flock 解到节点本机缓存 ${STUDIO_TOOLBOX_CACHE:-/tmp/studiobench-toolbox}/
blender-5.2.0-linux-x64/（先解到临时目录、写 marker、再 rename），之后直接复用；toolbox/bin/blender
（给 shell 用）走同一把锁、同一个目录。STUDIO_BLENDER 指定现成的二进制时跳过这套逻辑。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import media
from .. import procgroups
from ._base import TOOLBOX, R

NAME = "blender-5.2.0"
VERSION = "5.2.0"
DIRNAME = "blender-5.2.0-linux-x64"
TARBALL = TOOLBOX / "downloads" / (DIRNAME + ".tar.xz")       # 共享盘只放官方安装包
SHA_FILE = TOOLBOX / "downloads" / "blender-5.2.0.sha256"
MARKER = ".studiobench_ready"                                 # 解包完成标记（写在解好的目录里，随 rename 一起出现）
LOCK_NAME = ".blender-5.2.0.lock"                             # 和 toolbox/bin/blender 用同一把 flock
ENTRY = Path(__file__).resolve().parents[1] / "workers" / "blender_entry.py"
ENGINES = ("eevee", "cycles", "workbench")
DEVICES = ("auto", "gpu", "cpu")
MAX_PREVIEW = 16
MAX_SIDE = 7680
TIMEOUT_MIN, TIMEOUT_MAX = 30, 60
LOCK_WAIT_S = 900
_VERIFIED: Dict[str, str] = {}                                # 二进制路径 → --version 首行（每个进程验一次）


def _cache_root() -> Path:
    return Path(os.environ.get("STUDIO_TOOLBOX_CACHE") or "/tmp/studiobench-toolbox")


def _local_dir() -> Path:
    return _cache_root() / DIRNAME


def _ready(d: Path) -> bool:
    return (d / MARKER).is_file() and os.access(d / "blender", os.X_OK)


def check() -> Tuple[bool, str]:
    """只看安装包在不在、本机缓存好没好；不在这里解包，也不启动 Blender。"""
    explicit = os.environ.get("STUDIO_BLENDER")
    if explicit:
        ok = os.access(explicit, os.X_OK)
        return ok, ("STUDIO_BLENDER=%s" % explicit) if ok else "STUDIO_BLENDER 指向的文件不可执行：%s" % explicit
    if not TARBALL.is_file():
        return False, "共享盘上没有 Blender 安装包：%s" % TARBALL
    d = _local_dir()
    if _ready(d):
        return True, "Blender %s（本机缓存 %s）" % (VERSION, d)
    return True, "Blender %s：首次调用时从共享盘安装包解到本机缓存 %s（约 20–60 秒）" % (VERSION, d)


def _verify_tarball() -> None:
    if not SHA_FILE.is_file():
        return
    want = ""
    for line in SHA_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == TARBALL.name:
            want = parts[0].lower()
    if not want:
        return
    digest = hashlib.sha256()
    with TARBALL.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != want:
        raise RuntimeError("Blender 安装包校验失败（sha256 不符）：%s" % TARBALL)


def _extract(root: Path) -> None:
    """在已持有锁的前提下解包：先解到临时目录、写 marker，再 rename 成正式目录。"""
    for stale in root.glob(".tmp-blender-*"):               # 之前被打断的残留（持锁时不会有人在解）
        shutil.rmtree(stale, ignore_errors=True)
    target = root / DIRNAME
    if target.exists():                                       # 没有 marker 的残缺目录：挪开再删
        aside = root / (".stale-blender-%d" % os.getpid())
        target.rename(aside)
        shutil.rmtree(aside, ignore_errors=True)
    _verify_tarball()
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-blender-", dir=str(root)))
    try:
        try:
            subprocess.run(["tar", "-xJf", str(TARBALL), "-C", str(tmp)], check=True, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=1800)
        except (OSError, subprocess.CalledProcessError):      # 没有 xz / tar 时用 Python 自带的 lzma
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            import tarfile
            with tarfile.open(TARBALL, "r:xz") as tf:
                tf.extractall(tmp, filter="tar") if hasattr(tarfile, "data_filter") else tf.extractall(tmp)
        src = tmp / DIRNAME
        if not os.access(src / "blender", os.X_OK):
            raise RuntimeError("解包后找不到 blender 可执行文件：%s" % src)
        (src / MARKER).write_text(json.dumps({"version": VERSION, "tarball": str(TARBALL), "by": "render_blender",
                                              "host": os.uname().nodename,
                                              "at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        src.rename(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def blender_bin() -> Tuple[Path, float]:
    """返回 (可执行文件, 这次准备用了几秒)。本机缓存没好就加锁解包；STUDIO_BLENDER 优先。"""
    explicit = os.environ.get("STUDIO_BLENDER")
    if explicit:
        return Path(explicit), 0.0
    target = _local_dir()
    if _ready(target):
        return target / "blender", 0.0
    if not TARBALL.is_file():
        from ..backends import Unavailable
        raise Unavailable("共享盘上没有 Blender 安装包：%s" % TARBALL)
    t0 = time.time()
    root = _cache_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() - t0 > LOCK_WAIT_S:
                    raise RuntimeError("等别的进程解包 Blender 超过 %d 分钟：%s" % (LOCK_WAIT_S // 60, root / LOCK_NAME))
                time.sleep(1)
        try:
            if not _ready(target):                            # 拿到锁以后再看一次：可能别人刚解完
                _extract(root)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return target / "blender", time.time() - t0


def _verify_binary(binary: Path) -> str:
    """每个进程对同一个二进制只跑一次 --version，确认这台机器上真能起来、版本是 5.2.0。"""
    key = str(binary)
    if key in _VERIFIED:
        return _VERIFIED[key]
    try:
        proc = subprocess.run([key, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Blender 启动失败：%s" % exc) from None
    first = (proc.stdout.strip().splitlines() or [""])[0]
    if proc.returncode != 0 or ("Blender %s" % VERSION) not in first:
        raise RuntimeError("Blender 版本不对或起不来（%s）：%s" % (key, first or proc.stderr.strip()[-300:]))
    _VERIFIED[key] = first
    return first


# --------------------------------------------------------------------------- 小工具
def _next(rel_dir: str, prefix: str, ext: str) -> Path:
    """工作目录下 rel_dir/prefix_NNN.ext 自动编号并当场占位（ext 为空时建目录），并发调用也不会撞名。"""
    base = R().WORKDIR / rel_dir
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = base / ("%s_%03d%s" % (prefix, n, ext))
        try:
            if ext:
                os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            else:
                path.mkdir()
            return path
        except FileExistsError:
            n += 1


def _int(name: str, value: Any, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是整数（收到 %r）" % (name, value)) from None
    if float(value) != v or not lo <= v <= hi:
        raise ValueError("%s 要是 %d–%d 之间的整数（收到 %r）" % (name, lo, hi, value))
    return v


def _tail(text: str, n: int = 40) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("STUDIO_")]
    return "\n".join(lines[-n:])


def _checker_view(png: Path, out: Path) -> Path:
    """带 alpha 的帧铺在棋盘格上再看，透明的地方一眼能看出来。"""
    from PIL import Image

    img = Image.open(png)
    if img.mode != "RGBA":
        return png
    w, h = img.size
    cell = max(8, min(w, h) // 24)
    board = Image.new("RGBA", (w, h), (205, 205, 205, 255))
    dark = Image.new("RGBA", (cell, cell), (150, 150, 150, 255))
    for y in range(0, h, cell):
        for x in range((y // cell) % 2 * cell, w, cell * 2):
            board.paste(dark, (x, y))
    board.alpha_composite(img)
    board.convert("RGB").save(out, quality=90)
    return out


def _sheet(pngs: List[Path], labels: List[str], out: Path, scratch: Path) -> Path:
    views = [_checker_view(p, scratch / ("view_%03d.jpg" % i)) for i, p in enumerate(pngs)]
    return media.contact_sheet(views, labels, out)


def _overrides(start, end, fps, width, height, engine, samples, device, transparent) -> Dict[str, Any]:
    ov: Dict[str, Any] = {}
    if start is not None:
        ov["start"] = _int("start", start, 0, 1048574)
    if end is not None:
        ov["end"] = _int("end", end, 0, 1048574)
    if "start" in ov and "end" in ov and ov["end"] < ov["start"]:
        raise ValueError("end 不能小于 start")
    if fps is not None:
        f = float(fps)
        if not 0 < f <= 240:
            raise ValueError("fps 要在 (0, 240] 之间")
        ov["fps"] = f
    for key, val in (("width", width), ("height", height)):
        if val is not None:
            v = _int(key, val, 16, MAX_SIDE)
            if v % 2:
                raise ValueError("%s 要是偶数（H.264 yuv420p 的要求），收到 %d" % (key, v))
            ov[key] = v
    if engine is not None:
        e = str(engine).strip().lower()
        if e not in ENGINES:
            raise ValueError("engine 只支持 %s" % " / ".join(ENGINES))
        ov["engine"] = e
    if samples is not None:
        ov["samples"] = _int("samples", samples, 1, 4096)
    d = str(device or "auto").strip().lower()
    if d not in DEVICES:
        raise ValueError("device 只支持 %s" % " / ".join(DEVICES))
    ov["device"] = d
    if transparent is not None:
        ov["transparent"] = bool(transparent)
    return ov


# 给 Blender 的环境：脚本是模型写的，所以按 run.py 里 _shell_env 的口径去掉密钥和出网代理。
# Blender 自己要的（STUDIO_TOOLBOX、STUDIO_TOOLBOX_CACHE、LD_LIBRARY_PATH 等）照旧留着。
SECRET_MARKS = ("TOKEN", "KEY", "SECRET", "PASSWORD")
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
              "ftp_proxy", "FTP_PROXY", "no_proxy", "NO_PROXY")


def _audit_log(msg: str) -> None:
    """审计钩子记下的东西写进 mcp_server.log（和 server.py 的 _log_server 同一个文件，不引 server 免得循环）。"""
    log = Path(os.environ.get("STUDIO_RUN_DIR") or reg_workdir()) / "mcp_server.log"
    try:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def reg_workdir() -> str:
    try:
        return str(R().WORKDIR)
    except Exception:  # noqa: BLE001
        return "."


def _env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not any(m in k.upper() for m in SECRET_MARKS) and k not in PROXY_VARS}
    cuda = os.environ.get("STUDIO_BLENDER_CUDA_DEVICES")
    if cuda:                                  # 只给 Blender 指定卡，不影响 server 里的其他模型
        env["CUDA_VISIBLE_DEVICES"] = cuda
    env.pop("OCIO", None)                     # 不吃外部 OCIO 配置，用 Blender 自带的
    return env


def _run(binary: Path, cfg_path: Path, workdir: Path, log_path: Path, timeout_s: float) -> Tuple[int, str, float]:
    """跑 Blender（独立进程组，超时整组杀掉），返回 (returncode, 日志, 秒)。"""
    cmd = [str(binary), "-b", "--factory-startup",
           "-t", os.environ.get("STUDIO_BLENDER_THREADS", "8"), "-noaudio", "--python-exit-code", "3",
           "--python", str(ENTRY), "--", str(cfg_path)]
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, env=_env(), start_new_session=True)
        procgroups.register(proc)        # 独立进程组：server 被强杀时靠这份清单收尾
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            rc = -9
        finally:
            procgroups.release(proc)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return rc, text, time.time() - t0


def _keep_log(text: str) -> Path:
    path = _next("blender/logs", "render", ".log")
    path.write_text(text[-2_000_000:], encoding="utf-8")
    return path


_HINT_GPU = ("EEVEE / Workbench 需要 GPU 图形上下文（OpenGL/Vulkan），这台机器上起不来。"
             "换 engine=cycles（有 CUDA 用 GPU，没有就用 CPU）。")
_RE_GPU_FAIL = re.compile(r"(GPU|OpenGL|EGL|Vulkan|GHOST|backend).{0,80}(fail|unable|not support|could not|error)",
                          re.IGNORECASE)


# --------------------------------------------------------------------------- 工具
def render_blender(script: Optional[str] = None, code: Optional[str] = None,
                   frames: Optional[List[int]] = None, start: Optional[int] = None,
                   end: Optional[int] = None, fps: Optional[float] = None, width: Optional[int] = None,
                   height: Optional[int] = None, engine: Optional[str] = None,
                   samples: Optional[int] = None, device: str = "auto",
                   transparent: Optional[bool] = None, save_blend: Optional[str] = None,
                   out: Optional[str] = None, timeout_min: float = TIMEOUT_MIN) -> Dict[str, Any]:
    reg = R()
    ok, detail = check()
    if not ok:
        from ..backends import Unavailable
        raise Unavailable(detail)
    binary, setup_s = blender_bin()                    # 第一次用：从共享盘安装包解到本机缓存（加锁）
    version = _verify_binary(binary)
    if bool(script) == bool(code):
        raise ValueError("script（工作目录里的 .py）和 code（内联 Python）二选一，必须给一个")
    if script:
        src = reg._in(script)
        if src.suffix.lower() != ".py":
            raise ValueError("script 要是 .py 文件")
    else:
        src = _next("blender", "scene", ".py")
        src.write_text(str(code), encoding="utf-8")
    ov = _overrides(start, end, fps, width, height, engine, samples, device, transparent)
    preview: List[int] = []
    if frames:
        if not isinstance(frames, list):
            raise ValueError("frames 要是帧号列表，例如 [1, 48, 96]")
        preview = [_int("frames[]", f, 0, 1048574) for f in frames]
        if len(preview) > MAX_PREVIEW:
            raise ValueError("frames 一次最多 %d 帧" % MAX_PREVIEW)
    blend_path = None
    if save_blend:
        blend_path = reg._resolve(save_blend)
        if blend_path.suffix.lower() != ".blend":
            raise ValueError("save_blend 要以 .blend 结尾")
        blend_path.parent.mkdir(parents=True, exist_ok=True)
    want_alpha = bool(ov.get("transparent"))
    video_out = None
    if not preview:
        if out and Path(out).suffix.lower() not in (".mp4", ".mov"):
            raise ValueError("out 只能是 .mp4 或 .mov")
        if want_alpha and out and Path(out).suffix.lower() != ".mov":
            raise ValueError("transparent 要输出 .mov（ProRes 4444 带 alpha），把 out 换成 .mov")
        video_out = reg._out(out, "render_blender", ".mov" if want_alpha else ".mp4")
    timeout_s = max(1.0, min(float(timeout_min), TIMEOUT_MAX)) * 60

    scratch_root = os.environ.get("STUDIO_RENDER_SCRATCH") or None
    if scratch_root:
        Path(scratch_root).mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="studio_blender_", dir=scratch_root))
    try:
        frames_dir = scratch / "frames"
        frames_dir.mkdir()
        cfg = {"script": str(src), "workdir": str(reg.WORKDIR), "frames_dir": str(frames_dir),
               "result": str(scratch / "result.json"), "frames": preview, "overrides": ov,
               "save_blend": str(blend_path) if blend_path else None}
        cfg_path = scratch / "config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        rc, log, secs = _run(binary, cfg_path, reg.WORKDIR, scratch / "blender.log", timeout_s)
        res_path = scratch / "result.json"
        res = json.loads(res_path.read_text(encoding="utf-8")) if res_path.is_file() else {}
        if rc == -9:
            done = len(list(frames_dir.glob("f_*.png")))
            log_file = _keep_log(log)
            raise RuntimeError("渲染超时（%.0f 分钟，已渲 %d 帧），已强制结束。降分辨率/采样、缩短帧段，"
                               "或分几段渲再 concat。日志：%s" % (timeout_s / 60, done, reg._rel(log_file)))
        if not res.get("ok"):
            log_file = _keep_log(log)
            msg = res.get("error") or "Blender 退出码 %d，没有写结果" % rc
            hint = ""
            eng = str(res.get("engine") or ov.get("engine") or "")
            if res.get("stage") == "render" and "CYCLES" not in eng.upper() and _RE_GPU_FAIL.search(log):
                hint = "\n提示：" + _HINT_GPU
            elif not res and _RE_GPU_FAIL.search(log):
                hint = "\n提示：" + _HINT_GPU
            elif "CYCLES" in eng.upper() and re.search(r"CUDA (error|kernel)|cuda.*(failed|unsupported)", log, re.I):
                hint = "\n提示：Cycles 的 CUDA 出错了，可以先用 device=cpu 渲（慢一些）"
            tb = res.get("traceback") or _tail(log)
            raise RuntimeError("Blender 渲染失败（阶段 %s）：%s%s\n--- 输出末尾 ---\n%s\n（完整日志：%s）"
                               % (res.get("stage", "启动"), msg, hint, "\n".join(tb.splitlines()[-40:]),
                                  reg._rel(log_file)))
        pngs = sorted(frames_dir.glob("f_*.png"))
        if not pngs:
            log_file = _keep_log(log)
            raise RuntimeError("Blender 没有输出任何帧。%s\n--- 输出末尾 ---\n%s\n（完整日志：%s）"
                               % ("提示：" + _HINT_GPU if _RE_GPU_FAIL.search(log) else "",
                                  _tail(log), reg._rel(log_file)))
        info: Dict[str, Any] = {
            "backend": NAME, "engine": res.get("engine"), "device": res.get("device"),
            "width": res.get("width"), "height": res.get("height"), "fps": res.get("fps"),
            "transparent": res.get("transparent"), "render_seconds": res.get("render_seconds"),
            "wall_seconds": round(secs, 1), "script": reg._rel(src), "blender": version,
        }
        if setup_s > 0.5:
            info["setup_seconds"] = round(setup_s, 1)      # 首次调用：解包到本机缓存用的时间
        if res.get("samples") is not None:
            info["samples"] = res["samples"]
        per = res.get("frame_seconds") or []
        if per:
            info["sec_per_frame"] = round(sum(per) / len(per), 2)
        if res.get("warnings"):
            info["warnings"] = res["warnings"]
        if blend_path:
            info["blend"] = reg._rel(blend_path)
        guard = res.get("guard") or {}
        if guard.get("blocked"):             # warn 模式下脚本真的越界了：记进调用记录，事后能查
            info["guard"] = {"mode": guard.get("mode"), "blocked": guard["blocked"][:20]}
            _audit_log("render_blender 审计钩子记下 %d 条越界：%s"
                       % (len(guard["blocked"]), "; ".join(guard["blocked"][:5])))

        if preview:                          # 预览：帧原图留在工作目录，联系表给模型看
            keep = _next("blender", "preview", "")
            kept = []
            for p in pngs:
                dst = keep / p.name
                shutil.copy2(p, dst)
                kept.append(dst)
            num = lambda p: int(p.stem.split("_")[1])  # noqa: E731
            sheet = _sheet(kept, ["f%d" % num(p) for p in kept], keep / "sheet.jpg", scratch)
            info.update({"mode": "preview", "frames": [{"frame": num(p), "file": reg._rel(p)} for p in kept],
                         "sheet": reg._rel(sheet), "_images": [sheet]})
            return info

        # 整段：帧序列 → 视频
        nums = [int(p.stem.split("_")[1]) for p in pngs]
        fps_arg = "%d/%s" % (res["fps_num"], res["fps_base"]) if res.get("fps_base", 1) != 1 else str(res["fps_num"])
        if (res.get("frame_step") or 1) != 1 or nums != list(range(nums[0], nums[0] + len(nums))):
            for i, p in enumerate(pngs):     # 帧号不连续（frame_step>1）：先按顺序重命名
                p.rename(frames_dir / ("seq_%06d.png" % i))
            pattern, start_no = frames_dir / "seq_%06d.png", 0
        else:
            pattern, start_no = frames_dir / "f_%04d.png", nums[0]
            if max(nums) > 9999:
                for i, p in enumerate(pngs):
                    p.rename(frames_dir / ("seq_%06d.png" % i))
                pattern, start_no = frames_dir / "seq_%06d.png", 0
        part = reg._part(video_out)
        if video_out.suffix.lower() == ".mov" and want_alpha:
            codec = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]
        elif video_out.suffix.lower() == ".mov":
            codec = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
        else:
            codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart"]
        try:
            media.run(["-y", "-framerate", fps_arg, "-start_number", str(start_no), "-i", str(pattern),
                       *codec, str(part)])
        except Exception:
            part.unlink(missing_ok=True)
            raise
        reg._commit(part, video_out)
        # 抽样联系表：最多 12 帧，均匀挑，标帧号和时间
        seq = sorted(frames_dir.glob("*.png"))
        idx = sorted({int(round(i * (len(seq) - 1) / 11.0)) for i in range(12)}) if len(seq) > 12 \
            else list(range(len(seq)))
        fpsv = float(res.get("fps") or 24)
        labels = ["f%d %.2fs" % (nums[i], i / fpsv) for i in idx]
        sheet_path = _next("blender", "sheet", ".jpg")
        sheet = _sheet([seq[i] for i in idx], labels, sheet_path, scratch)
        info.update({"mode": "video", "frame_count": len(nums), "frame_start": nums[0], "frame_end": nums[-1],
                     "sheet": reg._rel(sheet), "_images": [sheet]})
        info.pop("width", None)              # 宽高、时长以编码后的文件为准
        info.pop("height", None)
        return reg._clip_info(video_out, info)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOLS: Dict[str, Dict[str, Any]] = {
    "render_blender": {
        "fn": render_blender,
        "backend": NAME,
        "check": check,
        "desc": "用 Blender 5.2.0 渲染 3D：执行 bpy 场景脚本（script 给工作目录里的 .py，或 code 内联代码，"
                "二选一），脚本负责建模、材质、灯光、相机和关键帧动画，不用自己调 render 或设输出路径。"
                "工作目录是 cwd，贴图等素材用相对路径。下面的参数只在显式给出时覆盖脚本里的设置。"
                "给 frames（帧号列表）就只渲这些静帧并返回联系表，先用它检查构图和光照；不给 frames 就渲 "
                "start..end 整段，输出无声视频（mp4；transparent=true 时输出带 alpha 的 .mov，可再叠加到别的画面上）。"
                "engine：cycles（device=auto 时有 CUDA 用 GPU，没有就用 CPU；固定种子，可复现）、eevee、workbench"
                "（后两者需要 GPU 图形上下文）。单次调用默认 30 分钟、最多 60 分钟。"
                "超时会强制结束，所以长镜头请分段渲再 concat。脚本出错时返回 traceback。",
        "schema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
            "script": {"type": "string", "description": "工作目录里的场景脚本 .py（相对路径）"},
            "code": {"type": "string", "description": "内联的 bpy 场景脚本；会存到 blender/scene_NNN.py"},
            "frames": {"type": "array", "items": _INT, "description": "只渲这些帧做预览（最多 16 帧）"},
            "start": {"type": "integer", "description": "起始帧（覆盖 scene.frame_start）"},
            "end": {"type": "integer", "description": "结束帧（含，覆盖 scene.frame_end）"},
            "fps": {"type": "number", "description": "帧率（覆盖场景设置），可以是 23.976 这类小数"},
            "width": {"type": "integer", "description": "宽（像素，偶数）"},
            "height": {"type": "integer", "description": "高（像素，偶数）"},
            "engine": {"type": "string", "enum": list(ENGINES)},
            "samples": {"type": "integer", "description": "采样数（Cycles 的 samples / EEVEE 的 TAA samples）"},
            "device": {"type": "string", "enum": list(DEVICES), "description": "Cycles 用的设备，默认 auto"},
            "transparent": {"type": "boolean", "description": "背景透明（film_transparent）；整段输出 .mov"},
            "save_blend": {"type": "string", "description": "另存 .blend（覆盖参数后、渲染前）"},
            "out": {"type": "string", "description": "输出视频（.mp4 / .mov），不给就自动编号"},
            "timeout_min": {"type": "number", "description": "本次渲染的超时（分钟），默认 30，最多 60"},
        }},
    },
}
