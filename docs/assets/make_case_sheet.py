#!/usr/bin/env python3
"""Build the six-model case contact sheet used in the README."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
CASES = HERE / 'cases'
OUT = HERE / 'model_cases.png'
FONT = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf'
FONT_B = '/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Bold.otf'
MODELS = [
    ('claude.mp4', 'Claude Opus 5.5', '92.2', (88, 166, 255)),
    ('gemini.mp4', 'Gemini 4 Argon', '92.0', (57, 197, 207)),
    ('gpt6.mp4', 'GPT-6 Astra', '88.8', (63, 185, 80)),
    ('deepseek.mp4', 'DeepSeek V4.1 Flash', '88.3', (57, 197, 207)),
    ('doubao.mp4', 'Doubao Seed 2.1 Pro', '86.9', (210, 153, 34)),
    ('qwenmax.mp4', 'Qwen3.8 Max', '83.9', (255, 123, 114)),
]
TW, TH = 640, 360
GAP = 16
LABEL_H = 54
COLS, ROWS = 3, 2
OUTER = 24
TOP = 74
W = OUTER * 2 + COLS * TW + (COLS - 1) * GAP
H = TOP + OUTER + ROWS * (TH + LABEL_H) + (ROWS - 1) * GAP

im = Image.new('RGB', (W, H), (13, 17, 23))
d = ImageDraw.Draw(im)
d.text((OUTER, 22), 'SAME BRIEF · DIFFERENT MODELS', font=ImageFont.truetype(FONT_B, 22), fill=(57, 197, 207))
d.text((OUTER, 50), 'COM-AST-002 · 6-second excerpt from each delivered film', font=ImageFont.truetype(FONT, 14), fill=(139, 152, 169))

# extract one poster frame from each clip
for i, (name, model, score, color) in enumerate(MODELS):
    r, c = divmod(i, COLS)
    x = OUTER + c * (TW + GAP)
    y = TOP + OUTER + r * (TH + LABEL_H + GAP)
    frame = Image.open(f'/tmp/case_{name}.png').convert('RGB')
    im.paste(frame.resize((TW, TH), Image.LANCZOS), (x, y))
    d.rectangle((x, y + TH, x + TW, y + TH + LABEL_H), fill=(21, 27, 35))
    d.rectangle((x, y + TH, x + 5, y + TH + LABEL_H), fill=color)
    d.text((x + 16, y + TH + 12), model, font=ImageFont.truetype(FONT_B, 17), fill=(230, 237, 243))
    d.text((x + 16, y + TH + 33), 'overall', font=ImageFont.truetype(FONT, 12), fill=(139, 152, 169))
    d.text((x + TW - 76, y + TH + 14), score, font=ImageFont.truetype(FONT_B, 22), fill=color)
im.save(OUT, optimize=True)
print(OUT, im.size, OUT.stat().st_size)
