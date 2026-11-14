#!/usr/bin/env python3
"""Render a model's tool-call log into an animated 'workflow graph emerging' GIF.

Nodes are tool types (counts increase as calls arrive); edges are file dependencies
reconstructed from the calls' input/output paths.  The animation is meant for the
README showcase, not for scoring.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

RUNS = Path('/taijifs_sz34/share_304169095/xenoxu/StudioBench/runs/sample160')
FONT = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf'
FONT_B = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Bold.otf'
FONT_M = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Medium.otf'

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
LABELS = {'claude-opus-5-5': 'Claude Opus 5.5', 'gemini-4-argon': 'Gemini 4 Argon',
          'gpt-6-astra': 'GPT-6 Astra', 'deepseek-v4.1-flash-1017': 'DeepSeek V4.1 Flash',
          'doubao-seed-2-1-pro-260915': 'Doubao Seed 2.1 Pro', 'qwen3.8-max-0902': 'Qwen3.8 Max',
          'qwen3.8-27b': 'Qwen3.8 27B', 'Hy4-A49B-1008-merge-prod-long-agent-rl_step_600': 'Hy4'}


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
    edge_at: dict[tuple[str, str], int] = {}
    for i, c in enumerate(calls):
        tool = c['tool']
        counts[tool] += 1
        first_seen.setdefault(tool, i)
        inputs = call_inputs(c)
        for f in inputs:
            src = produced.get(f)
            if src and src != tool:
                edges[(src, tool)] += 1
                edge_at.setdefault((src, tool), i)
        for f in c.get('outputs') or []:
            if isinstance(f, str):
                produced[norm(f)] = tool
    return first_seen, counts, edges, edge_at


def font(path: str, size: int):
    return ImageFont.truetype(path, size)


def text(draw, x, y, s, f, fill=TEXT):
    draw.text((x, y), s, font=f, fill=fill)


def rounded(draw, box, r, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def layout(calls, counts):
    """Place one node per tool type, grouped by stage; returns {tool: (x,y,w,h)}."""
    col_w = 220
    gap = 34
    left = (W - (len(STAGES) * col_w + (len(STAGES) - 1) * gap)) // 2
    pos = {}
    for si, (_, _, tools) in enumerate(STAGES):
        present = [t for t in tools if counts.get(t)]
        heights = 54
        total = len(present) * heights
        start_y = max(150, (H - 160 - total) // 2 + 120)
        x = left + si * (col_w + gap)
        for j, tool in enumerate(present):
            pos[tool] = (x, start_y + j * heights, col_w, 42)
    return pos


def bezier(p0, p1, p2, p3, n=24):
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        x = u ** 3 * p0[0] + 3 * u ** 2 * t * p1[0] + 3 * u * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = u ** 3 * p0[1] + 3 * u ** 2 * t * p1[1] + 3 * u * t ** 2 * p2[1] + t ** 3 * p3[1]
        pts.append((x, y))
    return pts


def render(calls, *, task, model, frame_i, step, out):
    first_seen, counts_all, edges_all, edge_at = build(calls)
    visible_calls = calls[:frame_i + 1]
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
    pos = layout(visible_calls, counts)
    im = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(im)
    # header
    text(d, 54, 34, 'STUDIOBENCH · WORKFLOW EMERGENCE', font(FONT_B, 15), CYAN)
    text(d, 54, 60, f'{LABELS.get(model, model)}  ·  {task}', font(FONT_B, 28), TEXT)
    text(d, 54, 98, '每个节点是一种工具；节点数字是调用次数；连线表示一次调用消费了另一个工具生成的文件。',
         font(FONT, 15), MUTED)
    # stage headers
    col_w = 220; gap = 34
    left = (W - (len(STAGES) * col_w + (len(STAGES) - 1) * gap)) // 2
    for si, (name, color, _) in enumerate(STAGES):
        x = left + si * (col_w + gap)
        d.rectangle((x, 138, x + col_w, 142), fill=color)
        text(d, x + 2, 116, name, font(FONT_M, 14), color)
    # edges behind nodes
    for (a, b), n in edges.items():
        if a not in pos or b not in pos:
            continue
        xa, ya, wa, ha = pos[a]; xb, yb, wb, hb = pos[b]
        p0 = (xa + wa, ya + ha / 2); p3 = (xb, yb + hb / 2)
        dx = max(60, abs(p3[0] - p0[0]) * 0.55)
        pts = bezier(p0, (p0[0] + dx, p0[1]), (p3[0] - dx, p3[1]), p3)
        alpha = min(180, 55 + n * 16)
        col = (78, 96, 122) if alpha < 130 else (106, 130, 162)
        d.line(pts, fill=col, width=1, joint='curve')
        # arrow head
        (x1, y1), (x2, y2) = pts[-3], pts[-1]
        ang = math.atan2(y2 - y1, x2 - x1)
        ah = 7
        d.polygon([(x2, y2), (x2 - ah * math.cos(ang - 0.45), y2 - ah * math.sin(ang - 0.45)),
                   (x2 - ah * math.cos(ang + 0.45), y2 - ah * math.sin(ang + 0.45))], fill=col)
    # nodes
    current = visible_calls[-1]['tool'] if visible_calls else ''
    for tool, (x, y, w, h) in pos.items():
        si = TOOL_STAGE.get(tool, 4)
        c = STAGE_COLORS[si]
        hot = tool == current
        rounded(d, (x, y, x + w, y + h), 9, (29, 39, 52) if not hot else (38, 58, 82),
                c if hot else (48, 60, 78), 2 if hot else 1)
        text(d, x + 12, y + 10, tool, font(FONT_M, 15), TEXT)
        cnt = counts[tool]
        tw = d.textlength(str(cnt), font=font(FONT_B, 15))
        text(d, x + w - 12 - tw, y + 10, str(cnt), font(FONT_B, 15), c)
    # progress and ticker
    progress = (frame_i + 1) / max(1, len(calls))
    bar_x, bar_y, bar_w = 54, H - 86, W - 108
    d.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 3), fill=(32, 41, 56))
    d.rectangle((bar_x, bar_y, bar_x + bar_w * progress, bar_y + 3), fill=ACCENT)
    last = visible_calls[-1] if visible_calls else {}
    outs = last.get('outputs') or []
    out_s = ', '.join(Path(str(x)).name for x in outs[:2]) or '—'
    text(d, 54, H - 66, f'call {frame_i + 1:03d} / {len(calls):03d}', font(FONT_M, 15), MUTED)
    text(d, 230, H - 66, f"{last.get('tool', '—')}  →  {out_s}", font(FONT_M, 15), TEXT)
    text(d, W - 330, H - 66, f"nodes {len(counts)} · edges {len(edges)}", font(FONT_M, 14), MUTED)
    # final banner
    if frame_i == len(calls) - 1:
        rounded(d, (W - 360, 40, W - 54, 104), 10, (18, 42, 28), GREEN, 1)
        text(d, W - 342, 50, 'final.mp4  delivered', font(FONT_B, 17), GREEN)
        text(d, W - 342, 76, f'{len(calls)} tool calls · {len(counts)} tool types', font(FONT, 13), (170, 220, 178))
    im.save(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', default='COM-AST-002')
    ap.add_argument('--model', default='claude-opus-5-5')
    ap.add_argument('--out', default='docs/assets/workflow_emergence.gif')
    ap.add_argument('--step', type=int, default=3)
    ap.add_argument('--size', type=int, default=10)
    args = ap.parse_args()
    calls = load_calls(args.task, args.model)
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
        render(calls, task=args.task, model=args.model, frame_i=i, step=step, out=str(p))
        if n % 10 == 0:
            print(n, len(indices), flush=True)
    imgs = [Image.open(frames_dir / f'f{n:04d}.png').convert('RGB') for n in range(len(indices))]
    # hold the final frame
    imgs += [imgs[-1]] * 10
    imgs[0].save(args.out, save_all=True, append_images=imgs[1:], duration=160, loop=0,
                 optimize=True, disposal=2)
    print('wrote', args.out, len(imgs), 'frames', Path(args.out).stat().st_size)


if __name__ == '__main__':
    main()
