# -*- coding: utf-8 -*-
"""在 Blender 里跑的入口（render_blender 工具用）：执行模型写的场景脚本，再按调用参数覆盖、渲染。

    blender -b --factory-startup -noaudio --python-exit-code 3 --python blender_entry.py -- <config.json>

config.json（由 extra/blender.py 写）：
    script      场景脚本绝对路径；workdir 工作目录（cwd，脚本里的相对路径从这里算）
    frames_dir  帧序列输出目录（节点本地临时目录）；result 结果 JSON 路径
    frames      只渲这些帧（预览）；为空则渲整段 frame_start..frame_end
    overrides   只含调用里显式给了的参数：start end fps width height engine samples device transparent
    save_blend  可选：覆盖参数以后、渲染之前另存一份 .blend

stdout 里以 STUDIO_ 开头的行给工具端解析进度；出错时 traceback 原样打印，并写进 result.json。
"""
import json
import os
import sys
import tempfile
import time
import traceback

import bpy

# --------------------------------------------------------------------------- 模型脚本的隔离
# 不给 shell 以后，render_blender 是唯一能跑任意代码的地方（下面直接 exec 脚本）。
# 所以在 exec 之前装一个 sys.addaudithook：拦掉 subprocess / socket，以及工作目录、Blender 安装目录、
# 本次 scratch 目录以外的文件读写；final.mp4 和 .calls.jsonl 单独拉黑。
#
# 只在跑模型脚本那一段严格（strict=True），exec 完立刻放开——后面的覆盖参数和渲染是 harness 自己的代码，
# 不该被自己的钩子误伤。strict 这个开关在钩子函数的闭包里，而 addaudithook 不暴露钩子函数，脚本改不了它。
# 在钩子里抛异常会真的把那次操作拦下来（Python 的审计钩子就是这个语义），脚本会在这里断掉。
BLOCK_EVENTS = {
    "subprocess.Popen": "起子进程", "os.system": "跑 shell 命令", "os.exec": "换掉进程映像",
    "os.posix_spawn": "起进程", "os.spawn": "起进程", "os.fork": "fork 子进程",
    "os.forkpty": "fork 一个 pty", "socket.__new__": "开 socket",
    "socket.connect": "连网络", "socket.bind": "监听端口",
}
# 事件 → 要检查的参数下标（rename / link / symlink / copyfile 两头都要查）
PATH_EVENTS = {
    "open": (0,), "os.chdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,),
    "os.removedirs": (0,), "os.listdir": (0,), "os.scandir": (0,), "os.chmod": (0,),
    "os.chown": (0,), "os.truncate": (0,), "os.mkfifo": (0,), "os.mknod": (0,),
    "os.link": (0, 1), "os.rename": (0, 1), "os.renames": (0, 1), "os.symlink": (0, 1),
    "shutil.copyfile": (0, 1), "shutil.copystat": (0, 1), "shutil.copymode": (0, 1),
}
DENY_NAMES = ("final.mp4", ".calls.jsonl")       # 交卷视频和调用记录：脚本不许碰


class GuardBlocked(RuntimeError):
    """模型脚本越界，被审计钩子拦下。"""


def _real(path) -> str:
    """规范化 + 解软链：相对路径按当前目录算，../.. 之类也在这里被还原成真路径。"""
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return ""


def _under(path: str, root: str) -> bool:
    return bool(root) and (path == root or path.startswith(root + os.sep))


def _guard_roots(cfg: dict) -> list:
    """脚本能碰的目录：工作目录、脚本目录、本次 scratch、Blender 安装目录和它的 Python，
    再加上 sys.path（import 要能读库）和临时目录 / 设备文件。"""
    roots = []

    def add(p):
        real = _real(p) if p else ""
        if real and real not in roots:
            roots.append(real)

    for key in ("workdir", "script", "frames_dir", "result", "save_blend"):
        val = cfg.get(key)
        if val:
            add(os.path.dirname(val))       # 目录本身；script / result 是文件，文件在允许目录里
    add(cfg.get("workdir"))
    add(tempfile.gettempdir())
    try:
        add(bpy.app.tempdir)
    except Exception:  # noqa: BLE001
        pass
    try:
        add(os.path.dirname(bpy.app.binary_path))
        add(os.path.dirname(os.path.dirname(bpy.app.binary_path)))
    except Exception:  # noqa: BLE001
        pass
    add(sys.prefix)
    add(sys.exec_prefix)
    add(sys.executable)
    for p in sys.path:
        add(p)
    home = os.environ.get("HOME")
    if home:                                # 只放开 Blender 的配置/缓存，不是整个 HOME（那里有密钥文件）
        add(os.path.join(home, ".cache"))
        add(os.path.join(home, ".config", "blender"))
    for p in ("/dev", "/proc", "/sys", "/dev/shm"):
        add(p)
    return roots


def _install_guard(cfg: dict):
    """装审计钩子，返回 (放开严格模式的函数, 记下的越界列表, 模式)。

    模式（环境变量 STUDIO_BLENDER_GUARD，config.json 里的 guard 也认）：
      block 默认，越界就抛异常把操作拦下来
      warn  只记进 result.json，不拦——用来确认有没有误伤
      off   完全不装钩子
    """
    mode = str(cfg.get("guard") or os.environ.get("STUDIO_BLENDER_GUARD") or "block").strip().lower()
    if mode == "off":
        return (lambda: None), [], "off"
    if mode not in ("block", "warn"):
        mode = "block"
    roots = _guard_roots(cfg)
    work = _real(cfg.get("workdir") or os.getcwd())
    # runs/<这次运行>/ 里放着题目原件、别的运行的结果；再往上一级是 runs/ 根（同题别的模型）。
    # 这两层里除工作目录以外的文件一律拉黑——`..` 和 `../..` 正好够到这两层。
    deny_roots = []
    for cand in (os.path.dirname(work), os.path.dirname(os.path.dirname(work)),
                 os.environ.get("STUDIO_RUN_DIR") or ""):
        real = _real(cand) if cand else ""
        if real and real not in (os.sep, work) and real not in deny_roots:
            deny_roots.append(real)
    hits: list = []
    strict = [True]

    def why(path) -> str:
        real = _real(path)
        if not real:
            return ""
        if os.path.basename(real) in DENY_NAMES:
            return "交卷视频和调用记录不许脚本碰"
        if not _under(real, work):
            for d in deny_roots:
                if _under(real, d):
                    return "运行目录 / runs 里、工作目录以外的文件（题目原件、别的运行的结果）"
        if not any(_under(real, r) for r in roots):
            return "工作目录 / Blender 安装目录 / 本次 scratch 以外"
        return ""

    def hook(event, args):
        if not strict[0]:                   # exec 已经跑完，后面是 harness 自己的代码
            return
        msg = ""
        if event in BLOCK_EVENTS:
            msg = "render_blender 的脚本不许%s（拦下 %s）" % (BLOCK_EVENTS[event], event)
        else:
            for i in PATH_EVENTS.get(event, ()):
                if i < len(args):
                    reason = why(args[i])
                    if reason:
                        msg = "render_blender 的脚本想读写 %s：%s" % (args[i], reason)
                        break
            if not msg:
                return
        hits.append(msg)
        if mode == "block":
            raise GuardBlocked(msg)

    sys.addaudithook(hook)
    return (lambda: strict.__setitem__(0, False)), hits, mode


ENGINE_ALIASES = {
    "eevee": ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"),
    "cycles": ("CYCLES",),
    "workbench": ("BLENDER_WORKBENCH",),
}
GPU_TYPES = ("CUDA", "HIP", "ONEAPI", "METAL", "OPTIX")       # H20 没有 RT 核：CUDA 优先，OptiX 放最后


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def _set_engine(scene, name):
    """engine 是动态枚举（Cycles 由插件注册），列不全；按候选标识逐个试着赋值。"""
    key = str(name).strip()
    candidates = list(ENGINE_ALIASES.get(key.lower(), ())) or [key.upper()]
    for ident in candidates:
        try:
            scene.render.engine = ident
            return ident
        except TypeError:
            continue
    raise ValueError("engine 只支持 eevee / cycles / workbench（试过 %s 都不可用），收到 %r"
                     % ("、".join(candidates), name))


def _set_fps(scene, fps):
    fps = float(fps)
    if fps <= 0 or fps > 240:
        raise ValueError("fps 要在 (0, 240] 之间")
    whole = int(round(fps))
    scene.render.fps = whole
    scene.render.fps_base = whole / fps if abs(whole - fps) > 1e-6 else 1.0


def _cycles_device(scene, want, warnings):
    """返回实际用的设备：CPU 或 CUDA/HIP/…（auto：有 GPU 用 GPU，没有退回 CPU）。"""
    if want == "cpu":
        scene.cycles.device = "CPU"
        return "CPU"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    tried = []
    for dtype in GPU_TYPES:
        try:
            prefs.compute_device_type = dtype
        except TypeError:                      # 这版 Blender 没编这种后端
            continue
        refresh = getattr(prefs, "refresh_devices", None)
        (refresh or prefs.get_devices)()
        devices = [d for d in prefs.devices if d.type == dtype]
        tried.append("%s:%d" % (dtype, len(devices)))
        if devices:
            for d in prefs.devices:
                d.use = d.type == dtype
            scene.cycles.device = "GPU"
            return "%s(%s)" % (dtype, ", ".join(d.name for d in devices))
    if want == "gpu":
        raise RuntimeError("device=gpu，但没找到可用的 GPU（试过 %s）。用 device=auto 或 cpu"
                           % (", ".join(tried) or "无"))
    prefs.compute_device_type = "NONE"
    scene.cycles.device = "CPU"
    warnings.append("没找到可用 GPU（试过 %s），Cycles 改用 CPU" % (", ".join(tried) or "无"))
    return "CPU"


def _image_settings(scene, transparent):
    settings = scene.render.image_settings
    if hasattr(settings, "media_type"):        # 5.x：先选图像，才能选 PNG
        try:
            settings.media_type = "IMAGE"
        except (TypeError, AttributeError):
            pass
    settings.file_format = "PNG"
    settings.color_mode = "RGBA" if transparent else "RGB"
    settings.color_depth = "8"
    settings.compression = 15


def main():
    cfg_path = sys.argv[sys.argv.index("--") + 1]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    result = {"ok": False, "version": bpy.app.version_string, "warnings": []}
    warnings = result["warnings"]
    stage = "script"
    guard_stop, guard_hits, guard_mode = _install_guard(cfg)     # 必须在 exec 之前
    try:
        os.chdir(cfg["workdir"])
        for p in (cfg["workdir"], os.path.dirname(cfg["script"])):
            if p not in sys.path:
                sys.path.insert(0, p)
        with open(cfg["script"], encoding="utf-8") as fh:
            source = fh.read()
        print("STUDIO_STAGE script", flush=True)
        exec(compile(source, cfg["script"], "exec"), {"__name__": "__main__", "__file__": cfg["script"]})
        guard_stop()                                             # 脚本跑完：后面是 harness 自己的代码

        stage = "settings"
        scene = bpy.context.scene
        ov = cfg.get("overrides") or {}
        if "engine" in ov:
            _set_engine(scene, ov["engine"])
        engine = scene.render.engine
        if "fps" in ov:
            _set_fps(scene, ov["fps"])
        if "width" in ov:
            scene.render.resolution_x = int(ov["width"])
        if "height" in ov:
            scene.render.resolution_y = int(ov["height"])
        if "width" in ov or "height" in ov:
            scene.render.resolution_percentage = 100
        if "start" in ov:
            scene.frame_start = int(ov["start"])
        if "end" in ov:
            scene.frame_end = int(ov["end"])
        if scene.frame_end < scene.frame_start:
            raise ValueError("frame_end（%d）小于 frame_start（%d）" % (scene.frame_end, scene.frame_start))
        if "transparent" in ov:
            scene.render.film_transparent = bool(ov["transparent"])
        transparent = bool(scene.render.film_transparent)
        device = "图形上下文"
        if engine == "CYCLES":
            device = _cycles_device(scene, str(ov.get("device", "auto")).lower(), warnings)
            if "samples" in ov:
                scene.cycles.samples = int(ov["samples"])
            scene.cycles.seed = 0                 # 固定种子：同一份脚本渲两次结果一致
            scene.cycles.use_animated_seed = False
        elif "samples" in ov and hasattr(scene, "eevee") and engine.startswith("BLENDER_EEVEE"):
            scene.eevee.taa_render_samples = int(ov["samples"])
        _image_settings(scene, transparent)
        scene.render.use_file_extension = True
        scene.render.use_overwrite = True
        scene.render.use_placeholder = False
        if scene.camera is None:
            raise RuntimeError("场景里没有活动相机（scene.camera 为空）：脚本里建一个相机并设 scene.camera = cam")

        fps_real = scene.render.fps / scene.render.fps_base
        result.update({
            "engine": engine, "device": device, "transparent": transparent,
            "width": int(scene.render.resolution_x * scene.render.resolution_percentage / 100),
            "height": int(scene.render.resolution_y * scene.render.resolution_percentage / 100),
            "fps": fps_real, "fps_num": scene.render.fps, "fps_base": scene.render.fps_base,
            "frame_start": scene.frame_start, "frame_end": scene.frame_end, "frame_step": scene.frame_step,
            "scene": scene.name,
        })
        if engine == "CYCLES":
            result["samples"] = scene.cycles.samples
        if cfg.get("save_blend"):
            bpy.ops.wm.save_as_mainfile(filepath=cfg["save_blend"], copy=True)
            result["blend"] = cfg["save_blend"]

        stage = "render"
        frames_dir = cfg["frames_dir"]
        times = []
        last = [time.time()]

        def _on_write(sc, *_args):
            now = time.time()
            times.append(round(now - last[0], 3))
            last[0] = now
            print("STUDIO_FRAME %d %.3f" % (sc.frame_current, times[-1]), flush=True)

        bpy.app.handlers.render_write.append(_on_write)
        print("STUDIO_STAGE render", flush=True)
        t0 = time.time()
        if cfg.get("frames"):
            done = []
            for f in cfg["frames"]:
                scene.frame_set(int(f))
                scene.render.filepath = os.path.join(frames_dir, "f_%04d" % int(f))
                last[0] = time.time()
                bpy.ops.render.render(write_still=True)
                done.append(int(f))
            result["frames"] = done
        else:
            scene.render.filepath = os.path.join(frames_dir, "f_####")
            last[0] = time.time()
            bpy.ops.render.render(animation=True)
            result["frames"] = list(range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
        result["render_seconds"] = round(time.time() - t0, 2)
        result["frame_seconds"] = times
        if engine != "CYCLES":                    # EEVEE / Workbench：报实际的图形上下文（NVIDIA，还是 llvmpipe 软件渲染）
            try:
                import gpu
                renderer = gpu.platform.renderer_get()
                result["device"] = "%s %s" % (gpu.platform.backend_type_get(), renderer)
                if any(k in renderer.lower() for k in ("llvmpipe", "softpipe", "swiftshader", "lavapipe")):
                    warnings.append("图形上下文是软件渲染（%s），%s 会很慢；要快就换 engine=cycles（有 CUDA 时走 GPU）"
                                    % (renderer, engine))
            except Exception as exc:  # noqa: BLE001
                result["device"] = "图形上下文（未知：%s）" % exc
        result["ok"] = True
    except BaseException as exc:  # noqa: BLE001  模型写的脚本里什么都可能抛（包括 SystemExit）
        result.update({"stage": stage, "error": "%s: %s" % (type(exc).__name__, exc),
                       "traceback": traceback.format_exc()})
        traceback.print_exc()
        sys.stdout.flush()
    guard_stop()
    result["guard"] = {"mode": guard_mode, "blocked": list(guard_hits)}
    _write(cfg["result"], result)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result["ok"] else 3)


main()
