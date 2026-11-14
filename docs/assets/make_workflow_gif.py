#!/usr/bin/env python3
"""Render a run's tool-call log into an animated workflow graph with generated media.

The graph shows tool types and file dependencies; the right-hand materials panel
shows the actual images / video frames / waveforms produced by the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

RUNS = Path('/taijifs_sz34/share_304169095/xenoxu/StudioBench/runs/sample160')
FFMPEG = '/data/workspace/miniconda3/lib/python3.14/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2'
FONT = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf'
FONT_B = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Bold.otf'
FONT_M = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Medium.otf'
THUMB_CACHE = Path('/tmp/workflow_thumb_cache')

W, H = 1600, 900
BG = (13, 17, 23)
PANEL = (21, 27, 35)
LINE = (40, 49, 66)
TEXT = (230, 237, 243)
MUTED = (139, 152, 169)
ACCENT = (88, 166, 255)
GREEN = (63, 185, 80)
AMBER = (210, 153, 34)
PURPLE = (188, 140, 255)
CYAN = (57, 197, 207)

STAGES = [
    ('检索 / 脚本', (88, 166, 255), ['search', 'fetch_url']),
    ('画面', (57, 197, 207), ['t2i', 'image_edit', 'keyframe_pair', 'character_sheet', 'prop_sheet',
                             'scene_sheet', 'remove_bg', 'render_html', 'render_three', 'render_blender']),
    ('视频', (63, 185, 80), ['t2v', 'i2v', 'ref_video', 'ken_burns', 'pose_flip', 'parallax_shot',
                            'shot_retake']),
    ('声音', (210, 153, 34), ['tts', 'music', 'sfx', 'lipsync', 'asr', 'beat_detect', 'beat_grid']),
    ('剪辑 / 检查', (188, 140, 255), ['trim', 'concat', 'transition', 'overlay', 'composite',
                                    'mix_audio', 'audio_mix', 'audio_edit', 'color', 'reframe',
                                    'speed', 'scene_detect', 'sample_frames', 'inspect', 'motion_audit',
                                    'pacing_report', 'sequence_audit', 'take_compare', 'sound_audit']),
    ('交付', (248, 81, 73), ['export']),
]
TOOL_STAGE = {t: i for i, (_, _, ts) in enumerate(STAGES) for t in ts}
STAGE_COLORS = [s[1] for s in STAGES]
ICONS = {'search': '⌕', 'fetch_url': '↧', 't2i': '▧', 'image_edit': '✎',
         'keyframe_pair': '◫', 'character_sheet': '☺', 'prop_sheet': '◇', 'scene_sheet': '▤',
         'render_html': '</>', 'render_three': '◈', 'render_blender': '⬡', 'remove_bg': '◌',
         't2v': '▶', 'i2v': '▶', 'ref_video': '↻', 'ken_burns': '◐', 'pose_flip': '⇄',
         'parallax_shot': '▱', 'shot_retake': '↺', 'tts': '◉', 'music': '♪', 'sfx': '≈',
         'lipsync': '▭', 'asr': '⌁', 'beat_detect': '♩', 'beat_grid': '♬',
         'trim': '✂', 'concat': '⇥', 'transition': '⇢', 'overlay': '≡', 'composite': '▣',
         'mix_audio': '≋', 'audio_mix': '≋', 'audio_edit': '≈', 'color': '◑', 'reframe': '□',
         'speed': '»', 'scene_detect': '⌗', 'sample_frames': '▤', 'inspect': '◉',
         'motion_audit': '↯', 'pacing_report': '⌁', 'sequence_audit': '☷',
         'take_compare': '⇆', 'sound_audit': '♬', 'export': '↗'}
LABELS = {'claude-opus-5-5': 'Claude Opus 5.5', 'gemini-4-argon': 'Gemini 4 Argon',
          'gpt-6-astra': 'GPT-6 Astra', 'deepseek-v4.1-flash-1017': 'DeepSeek V4.1 Flash',
          'doubao-seed-2-1-pro-260915': 'Doubao Seed 2.1 Pro', 'qwen3.8-max-0902': 'Qwen3.8 Max',
          'qwen3.8-27b': 'Qwen3.8 27B', 'Hy4-A49B-1008-merge-prod-long-agent-rl_step_600': 'Hy4'}
ASSET_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.wav', '.mp3', '.m4a'}
GRAPH_X, GRAPH_W = 40, 1050
COL_W, COL_GAP = 160, 18
PANEL_X, PANEL_W = 1132, 428


def norm(path: str) -> str:
    return Path(str(path)).name


def call_inputs(call: dict) -> list[str]:
    args = call.get('args') or {}
    out = []
    for key in ('video', 'audio', 'src', 'layer', 'path', 'first_frame', 'last_frame',
                'image', 'reference', 'ref', 'out'):
        v = args.get(key)
        if isinstance(v, str):
            out.append(v)
    for key in ('videos', 'files'):
        v = args.get(key)
        if isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
    for key in ('tracks', 'layers'):
        v = args.get(key)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and isinstance(x.get('file'), str):
                    out.append(x['file'])
    return [norm(x) for x in out if x and not x.startswith('[internet]')]


def load_calls(task: str, model: str) -> list[dict]:
    p = RUNS / task / model / 'work' / '.calls.jsonl'
    calls = []
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if c.get('tool'):
            calls.append(c)
    return calls


def build(calls: list[dict]):
    produced: dict[str, str] = {}
    first_seen: dict[str, int] = {}
    counts: Counter = Counter()
    edges: Counter = Counter()
    for i, c in enumerate(calls):
        tool = c['tool']
        counts[tool] += 1
        first_seen.setdefault(tool, i)
        for f in call_inputs(c):
            src = produced.get(f)
            if src and src != tool:
                edges[(src, tool)] += 1
        for f in c.get('outputs') or []:
            if isinstance(f, str):
                produced[norm(f)] = tool
    return first_seen, counts, edges


def font(path: str, size: int):
    return ImageFont.truetype(path, size)


def text(draw, x, y, s, f, fill=TEXT):
    draw.text((x, y), s, font=f, fill=fill)


def rounded(draw, box, r, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def fit_image(im: Image.Image, w: int, h: int) -> Image.Image:
    im = im.convert('RGB')
    im = ImageOps.contain(im, (w, h), Image.LANCZOS)
    canvas = Image.new('RGB', (w, h), (10, 14, 20))
    canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    return canvas


def thumb_for(path: Path) -> Image.Image | None:
    if not path.is_file():
        return None
    THUMB_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1((str(path) + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:16]
    cache = THUMB_CACHE / (key + '.png')
    if cache.is_file():
        try:
            return Image.open(cache).convert('RGB')
        except OSError:
            pass
    ext = path.suffix.lower()
    try:
        if ext in ('.png', '.jpg', '.jpeg', '.webp'):
            im = fit_image(Image.open(path), 320, 180)
        elif ext in ('.mp4', '.mov'):
            tmp = THUMB_CACHE / (key + '_v.png')
            subprocess.run([FFMPEG, '-v', 'error', '-y', '-ss', '0.5', '-i', str(path),
                            '-frames:v', '1', '-vf', 'scale=320:180:force_original_aspect_ratio=decrease,'
                            'pad=320:180:(ow-iw)/2:(oh-ih)/2', str(tmp)],
                           check=True, timeout=60)
            im = Image.open(tmp).convert('RGB')
        elif ext in ('.wav', '.mp3', '.m4a'):
            tmp = THUMB_CACHE / (key + '_a.png')
            subprocess.run([FFMPEG, '-v', 'error', '-y', '-i', str(path),
                            '-filter_complex', 'showwavespic=s=320x180:colors=0x58a6ff',
                            '-frames:v', '1', str(tmp)], check=True, timeout=60)
            im = Image.open(tmp).convert('RGB')
        else:
            return None
        im.save(cache)
        return im
    except Exception:
        return None


def precompute_assets(calls: list[dict], task: str, model: str):
    work = RUNS / task / model / 'work'
    run = RUNS / task / model
    out = []
    seen = set()
    for i, c in enumerate(calls):
        for name in c.get('outputs') or []:
            if not isinstance(name, str):
                continue
            p = work / name
            if not p.is_file():
                p = run / name
            if not p.is_file() or p.suffix.lower() not in ASSET_EXTS:
                continue
            key = (i, str(p))
            if key in seen:
                continue
            seen.add(key)
            im = thumb_for(p)
            if im is not None:
                out.append({'i': i, 'tool': c.get('tool', ''), 'name': Path(name).name,
                            'path': p, 'image': im})
    return out


def layout(counts):
    pos = {}
    lane_top, lane_h, step, node_h = 160, 636, 48, 40
    for si, (_, _, tools) in enumerate(STAGES):
        present = [t for t in tools if counts.get(t)]
        total = len(present) * step
        start_y = lane_top + max(0, (lane_h - total) // 2)
        x = GRAPH_X + si * (COL_W + COL_GAP)
        for j, tool in enumerate(present):
            pos[tool] = (x, start_y + j * step, COL_W, node_h)
    return pos


def bezier(p0, p1, p2, p3, n=20):
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        x = u ** 3 * p0[0] + 3 * u ** 2 * t * p1[0] + 3 * u * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = u ** 3 * p0[1] + 3 * u ** 2 * t * p1[1] + 3 * u * t ** 2 * p2[1] + t ** 3 * p3[1]
        pts.append((x, y))
    return pts


def truncate(draw, s: str, f, max_w: float) -> str:
    if draw.textlength(s, font=f) <= max_w:
        return s
    while s and draw.textlength(s + '…', font=f) > max_w:
        s = s[:-1]
    return s + '…'


def render(calls, assets, all_counts, *, task, model, frame_i, out):
    visible_calls = calls[:frame_i + 1]
    visible_assets = [a for a in assets if a['i'] <= frame_i]
    counts = Counter(c['tool'] for c in visible_calls)
    produced = {}
    edges = {}
    for c in visible_calls:
        for f in c.get('outputs') or []:
            if isinstance(f, str):
                produced[norm(f)] = c['tool']
    for c in visible_calls:
        for f in call_inputs(c):
            src = produced.get(f)
            if src and src != c['tool']:
                edges[(src, c['tool'])] = edges.get((src, c['tool']), 0) + 1
    pos = layout(all_counts)
    im = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(im)
    text(d, 54, 30, 'STUDIOBENCH · WORKFLOW EMERGENCE', font(FONT_B, 15), CYAN)
    text(d, 54, 54, f'{LABELS.get(model, model)}  ·  {task}', font(FONT_B, 27), TEXT)
    text(d, 54, 91, '节点 = 工具类型，数字 = 调用次数，连线 = 文件依赖；右侧展示这次运行实际生成/编辑的素材。',
         font(FONT, 14), MUTED)
    for si, (name, color, _) in enumerate(STAGES):
        x = GRAPH_X + si * (COL_W + COL_GAP)
        if si:
            d.line((x - 9, 154, x - 9, 800), fill=(29, 36, 47), width=1)
        d.rectangle((x, 142, x + COL_W, 146), fill=color)
        text(d, x, 120, f'0{si + 1}  {name}', font(FONT_M, 13), color)
    for (a, b), n in edges.items():
        if a not in pos or b not in pos:
            continue
        xa, ya, wa, ha = pos[a]; xb, yb, wb, hb = pos[b]
        p0 = (xa + wa, ya + ha / 2); p3 = (xb, yb + hb / 2)
        dx = max(40, abs(p3[0] - p0[0]) * 0.55)
        pts = bezier(p0, (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1]), p3)
        base = STAGE_COLORS[TOOL_STAGE.get(a, 4)]
        col = tuple(int(BG[i] * 0.62 + base[i] * 0.38) for i in range(3))
        d.line(pts, fill=col, width=1, joint='curve')
        (x1, y1), (x2, y2) = pts[-3], pts[-1]
        ang = math.atan2(y2 - y1, x2 - x1)
        ah = 7
        d.polygon([(x2, y2), (x2 - ah * math.cos(ang - 0.45), y2 - ah * math.sin(ang - 0.45)),
                   (x2 - ah * math.cos(ang + 0.45), y2 - ah * math.sin(ang + 0.45))], fill=col)
    is_final = frame_i == len(calls) - 1
    current = 'export' if is_final else (visible_calls[-1]['tool'] if visible_calls else '')
    latest = {}
    for a in visible_assets:
        latest[a['tool']] = a
    for tool, (x, y, w, h) in pos.items():
        c = STAGE_COLORS[TOOL_STAGE.get(tool, 4)]
        active = bool(counts.get(tool))
        hot = tool == current
        fill = (30, 41, 55) if not hot else (38, 58, 82)
        outline = c if hot else (48, 60, 78)
        if not active:
            fill, outline = (20, 26, 34), (36, 45, 59)
        rounded(d, (x, y, x + w, y + h), 8, fill, outline, 2 if hot else 1)
        d.rectangle((x + 1, y + 7, x + 5, y + h - 7), fill=c if active else (44, 54, 68))
        asset = latest.get(tool) if active else None
        if asset:
            thumb = asset['image'].resize((32, 18), Image.LANCZOS)
            im.paste(thumb, (x + 9, y + 11))
            text(d, x + 47, y + 11, truncate(d, tool, font(FONT_M, 11), w - 68),
                 font(FONT_M, 11), TEXT)
        else:
            text(d, x + 10, y + 11, ICONS.get(tool, '·'), font(FONT_M, 12),
                 c if active else (72, 84, 102))
            text(d, x + 28, y + 11, truncate(d, tool, font(FONT_M, 11), w - 64),
                 font(FONT_M, 11), TEXT if active else (92, 105, 124))
        if active:
            cnt = str(counts[tool])
            tw = d.textlength(cnt, font=font(FONT_B, 11))
            text(d, x + w - 8 - tw, y + 11, cnt, font(FONT_B, 11), c)

    # materials panel
    rounded(d, (PANEL_X, 112, PANEL_X + PANEL_W, 792), 12, PANEL, LINE, 1)
    text(d, PANEL_X + 18, 128, 'MATERIALS  /  生成素材', font(FONT_B, 14), CYAN)
    text(d, PANEL_X + 18, 149, f'{len(visible_assets)} assets from this run', font(FONT, 11), MUTED)
    preview_box = (PANEL_X + 18, 174, PANEL_X + PANEL_W - 18, 174 + 214)
    d.rectangle(preview_box, fill=(10, 14, 20), outline=LINE)
    cur_asset = visible_assets[-1] if visible_assets else None
    if cur_asset:
        im.paste(cur_asset['image'].resize((preview_box[2] - preview_box[0],
                                            preview_box[3] - preview_box[1]), Image.LANCZOS),
                 (preview_box[0], preview_box[1]))
        label = f"{cur_asset['tool']}  ·  {cur_asset['name']}"
        text(d, PANEL_X + 18, 396, truncate(d, label, font(FONT_M, 12), PANEL_W - 36),
             font(FONT_M, 12), TEXT)
    else:
        text(d, PANEL_X + 18, 270, '等待第一个素材…', font(FONT_M, 14), MUTED)
    text(d, PANEL_X + 18, 432, 'RECENT', font(FONT_B, 11), MUTED)
    recent = visible_assets[-9:]
    tw, th = 126, 71
    for idx, asset in enumerate(recent):
        r, c = divmod(idx, 3)
        x = PANEL_X + 18 + c * (tw + 8)
        y = 454 + r * (th + 27)
        d.rectangle((x, y, x + tw, y + th), fill=(10, 14, 20), outline=LINE)
        im.paste(asset['image'].resize((tw, th), Image.LANCZOS), (x, y))
        name = truncate(d, asset['name'], font(FONT, 9), tw)
        text(d, x, y + th + 3, name, font(FONT, 9), MUTED)

    # progress / ticker
    progress = (frame_i + 1) / max(1, len(calls))
    bar_x, bar_y, bar_w = 54, H - 52, W - 108
    d.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 3), fill=(32, 41, 56))
    d.rectangle((bar_x, bar_y, bar_x + bar_w * progress, bar_y + 3), fill=ACCENT)
    last = visible_calls[-1] if visible_calls else {}
    outs = last.get('outputs') or []
    out_s = ', '.join(Path(str(x)).name for x in outs[:2]) or '—'
    tick_tool = 'export' if is_final else last.get('tool', '—')
    tick_out = 'final.mp4' if is_final else out_s
    text(d, 54, H - 36, f'call {frame_i + 1:03d} / {len(calls):03d}', font(FONT_M, 14), MUTED)
    text(d, 226, H - 36, f'{tick_tool}  →  {tick_out}', font(FONT_M, 14), TEXT)
    text(d, W - 360, H - 36, f"nodes {len(counts)} · edges {len(edges)}", font(FONT_M, 13), MUTED)

    if frame_i == len(calls) - 1:
        rounded(d, (W - 382, 26, W - 54, 88), 10, (18, 42, 28), GREEN, 1)
        text(d, W - 364, 36, 'final.mp4  delivered', font(FONT_B, 16), GREEN)
        text(d, W - 364, 60, f'{len(calls)} calls · {len(visible_assets)} assets',
             font(FONT, 12), (170, 220, 178))
    im.save(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', default='COM-AST-002')
    ap.add_argument('--model', default='claude-opus-5-5')
    ap.add_argument('--out', default='docs/assets/workflow_emergence.gif')
    ap.add_argument('--step', type=int, default=4)
    args = ap.parse_args()
    calls = load_calls(args.task, args.model)
    assets = precompute_assets(calls, args.task, args.model)
    all_counts = Counter(c["tool"] for c in calls)
    print('assets', len(assets), flush=True)
    step = max(1, args.step)
    frames_dir = Path('/tmp/workflow_gif_frames')
    frames_dir.mkdir(exist_ok=True)
    for f in frames_dir.glob('*.png'):
        f.unlink()
    indices = list(range(0, len(calls), step))
    if indices[-1] != len(calls) - 1:
        indices.append(len(calls) - 1)
    for n, i in enumerate(indices):
        p = frames_dir / f'f{n:04d}.png'
        render(calls, assets, all_counts, task=args.task, model=args.model, frame_i=i, out=str(p))
        if n % 10 == 0:
            print(n, len(indices), flush=True)
    imgs = [Image.open(frames_dir / f'f{n:04d}.png').convert('RGB') for n in range(len(indices))]
    imgs += [imgs[-1]] * 10
    imgs[0].save(args.out, save_all=True, append_images=imgs[1:], duration=180, loop=0,
                 optimize=True, disposal=2)
    print('wrote', args.out, len(imgs), 'frames', Path(args.out).stat().st_size)


if __name__ == '__main__':
    main()
