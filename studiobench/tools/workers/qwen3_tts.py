#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen3-TTS 合成 worker（音色设计 / 克隆 / 预置音色）。在自己的环境里运行（qwen-tts 要 transformers 4.57）。

    python qwen3_tts.py --request req.json --response resp.json     # 单次
    python qwen3_tts.py --serve                                     # 常驻：逐行读 stdin 的 {"request", "response"}

三个模型按需加载、常驻显存（1.7B 各约 4.5GB）：
  design  Qwen3-TTS-12Hz-1.7B-VoiceDesign：按文字描述造一个新音色
  clone   Qwen3-TTS-12Hz-1.7B-Base：拿一段参考音频（+ 原文）克隆音色说新台词；同一参考的 prompt 会缓存
  custom  Qwen3-TTS-12Hz-1.7B-CustomVoice：9 个预置音色，可带语气指令

req: op = design | clone | custom
  design: text, instruct, language, out
  clone:  text, ref_audio, ref_text（可空，空时只用说话人向量）, instruct（Base 模型不支持，忽略）, language, out
  custom: text, speaker, instruct（可空）, language, out
结果先写临时文件再改名；stdout 会混进模型库的日志，所以不用 stdout 回传。
"""
import argparse
import hashlib
import json
import os
import sys
import time
import traceback

_MODELS = {}
_PROMPTS = {}
_DIRS = {"design": "Qwen3-TTS-12Hz-1.7B-VoiceDesign", "clone": "Qwen3-TTS-12Hz-1.7B-Base",
         "custom": "Qwen3-TTS-12Hz-1.7B-CustomVoice"}


def _model(root, kind):
    if kind not in _MODELS:
        import torch
        from qwen_tts import Qwen3TTSModel
        _MODELS[kind] = Qwen3TTSModel.from_pretrained(os.path.join(root, _DIRS[kind]), device_map="cuda:0",
                                                      dtype=torch.bfloat16, attn_implementation="sdpa")
    return _MODELS[kind]


def _clone_prompt(model, ref_audio, ref_text):
    st = os.stat(ref_audio)
    key = hashlib.sha1(("%s|%d|%d|%s" % (ref_audio, st.st_size, int(st.st_mtime), ref_text or "")).encode()).hexdigest()
    if key not in _PROMPTS:
        if ref_text:
            _PROMPTS[key] = model.create_voice_clone_prompt(ref_audio=ref_audio, ref_text=ref_text)
        else:
            _PROMPTS[key] = model.create_voice_clone_prompt(ref_audio=ref_audio, ref_text=None, x_vector_only_mode=True)
    return _PROMPTS[key]


def synthesize(req):
    try:
        import soundfile as sf
        op, root = req["op"], req["root"]
        lang = req.get("language") or "Auto"
        t0 = time.time()
        if op == "design":
            wavs, sr = _model(root, "design").generate_voice_design(text=req["text"], instruct=req["instruct"],
                                                                     language=lang)
        elif op == "clone":
            model = _model(root, "clone")
            prompt = _clone_prompt(model, req["ref_audio"], (req.get("ref_text") or "").strip())
            wavs, sr = model.generate_voice_clone(text=req["text"], language=lang, voice_clone_prompt=prompt)
        elif op == "custom":
            wavs, sr = _model(root, "custom").generate_custom_voice(text=req["text"], speaker=req["speaker"],
                                                                     language=lang, instruct=req.get("instruct") or None)
        elif op == "speakers":
            m = _model(root, "custom")
            return {"ok": True, "speakers": m.get_supported_speakers(), "languages": m.get_supported_languages()}
        else:
            raise ValueError("未知 op：%s" % op)
        if not len(wavs) or not len(wavs[0]):
            raise RuntimeError("Qwen3-TTS 没有产出音频")
        sf.write(req["out"], wavs[0], sr)
        return {"ok": True, "sample_rate": sr, "seconds": round(len(wavs[0]) / float(sr), 3),
                "synth_s": round(time.time() - t0, 2)}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _release() -> None:
    """每次合成后把激活缓存还给显卡。模型本身常驻（按需加载，各约 4.5GB），这里只放每次请求的峰值。
    常驻 worker 不主动释放的话，torch 的缓存分配器会把最大那次的块一直攒着：asr 就是这么涨到 34.7GB、
    把整张卡占满、让同卡的 lipsync / image_edit 每次调用都失败的（2026-10-31）。"""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request")
    ap.add_argument("--response")
    ap.add_argument("--serve", action="store_true")
    args = ap.parse_args()
    if not args.serve:
        _write(args.response, synthesize(json.load(open(args.request, encoding="utf-8"))))
        return 0
    for line in sys.stdin:
        if not line.strip():
            continue
        cmd = json.loads(line)
        try:
            resp = synthesize(json.load(open(cmd["request"], encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            resp = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            _release()
        _write(cmd["response"], resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
