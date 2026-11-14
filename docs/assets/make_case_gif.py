#!/usr/bin/env python3
"""Build a 2x3 animated contact sheet of the six model case clips."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
CASES = HERE / 'cases'
OUT = HERE / 'model_cases.gif'
FRAMES = Path('/tmp/case_gif_frames')
FONT = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf'
FONT_B = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Bold.otf'
MODELS = [
    ('claude', 'Claude Opus 5.5', '92.2', (88, 166, 255)),
    ('gemini', 'Gemini 4 Argon', '92.0', (57, 197, 207)),
    ('gpt6', 'GPT-6 Astra', '88.8', (63, 185, 80)),
    ('deepseek', 'DeepSeek V4.1 Flash', '88.3', (57, 197, 207)),
    ('doubao', 'Doubao Seed 2.1 Pro', '86.9', (210, 153, 34)),
    ('qwenmax', 'Qwen3.8 Max', '83.9', (255, 123, 114)),
]
TW, TH = 480, 270
GAP, LABEL_H, OUTER, TOP = 10, 42, 18, 58
COLS, ROWS = 3, 2
W = OUTER * 2 + COLS * TW + (COLS - 1) * GAP
H = TOP + OUTER + ROWS * (TH + LABEL_H) + (ROWS - 1) * GAP

frames = []
n = 1
while True:
    im = Image.new('RGB', (W, H), (13, 17, 23))
    d = ImageDraw.Draw(im)
    d.text((OUTER, 14), 'SAME BRIEF · DIFFERENT MODELS', font=ImageFont.truetype(FONT_B, 18), fill=(57, 197, 207))
    d.text((OUTER, 36), 'COM-AST-002 · 1.6s excerpt from each delivered film', font=ImageFont.truetype(FONT, 11), fill=(139, 152, 169))
    any_frame = False
    for i, (key, model, score, color) in enumerate(MODELS):
        r, c = divmod(i, COLS)
        x = OUTER + c * (TW + GAP)
        y = TOP + OUTER + r * (TH + LABEL_H + GAP)
        p = FRAMES / key / ('%03d.png' % n)
        if p.is_file():
            im.paste(Image.open(p).convert('RGB').resize((TW, TH), Image.LANCZOS), (x, y))
            any_frame = True
        d.rectangle((x, y + TH, x + TW, y + TH + LABEL_H), fill=(21, 27, 35))
        d.rectangle((x, y + TH, x + 4, y + TH + LABEL_H), fill=color)
        d.text((x + 12, y + TH + 8), model, font=ImageFont.truetype(FONT_B, 13), fill=(230, 237, 243))
        d.text((x + TW - 46, y + TH + 8), score, font=ImageFont.truetype(FONT_B, 16), fill=color)
        d.text((x + 12, y + TH + 25), 'overall', font=ImageFont.truetype(FONT, 10), fill=(139, 152, 169))
    if not any_frame:
        break
    frames.append(im)
    n += 1
    if n > 60:
        break

frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=100, loop=0,
               optimize=True, disposal=2)
print(OUT, len(frames), OUT.stat().st_size, im.size)
