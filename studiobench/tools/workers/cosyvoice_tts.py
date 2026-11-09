#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CosyVoice3 合成 worker。在 CosyVoice 自己的环境里运行（py3.8+ 语法）。

    python cosyvoice_tts.py --request req.json --response resp.json     # 单次：加载模型、合成一句、退出
    python cosyvoice_tts.py --serve                                     # 常驻：模型只加载一次，逐行读 stdin 的请求

常驻模式下 stdin 每行一个 JSON：{"request": "<req.json>", "response": "<resp.json>"}；结果先写临时文件再改名，
调用方看到 resp.json 就是完整的。stdout 会混进模型库的日志，所以不用 stdout 回传结果。
req: text, out, repo, model, prompt_wav, prompt_text, mode(zero_shot|cross_lingual), speed
CosyVoice3 要求 `<|endofprompt|>` 出现在 prompt_text（zero-shot）或待合成文本（cross-lingual）里，
否则 llm.py 会 assert 失败；这里统一补上官方的系统前缀。
"""
import argparse
import json
import os
import sys
import traceback

SYSTEM = "You are a helpful assistant.<|endofprompt|>"
_MODELS = {}


def _with_system(text):
    return text if "<|endofprompt|>" in text else SYSTEM + text


def _model(req):
    if req["model"] not in _MODELS:
        for p in (req["repo"] + "/third_party/Matcha-TTS", req["repo"]):
            if p not in sys.path:
                sys.path.insert(0, p)
        from cosyvoice.cli.cosyvoice import AutoModel
        _MODELS[req["model"]] = AutoModel(model_dir=req["model"])
    return _MODELS[req["model"]]


def synthesize(req):
    resp = {"ok": False}
    try:
        model = _model(req)
        import soundfile as sf
        import torch

        speed = float(req.get("speed") or 1.0)
        if req.get("mode") == "zero_shot":
            chunks = model.inference_zero_shot(req["text"], _with_system(req["prompt_text"]),
                                               req["prompt_wav"], stream=False, speed=speed)
        else:
            chunks = model.inference_cross_lingual(_with_system(req["text"]), req["prompt_wav"],
                                                   stream=False, speed=speed)
        pieces = [item["tts_speech"] for item in chunks]
        if not pieces:
            raise RuntimeError("CosyVoice 没有产出音频")
        speech = torch.cat(pieces, dim=-1)
        sf.write(req["out"], speech.squeeze(0).detach().cpu().numpy(), model.sample_rate)
        resp = {"ok": True, "sample_rate": model.sample_rate,
                "seconds": round(speech.shape[-1] / float(model.sample_rate), 3)}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        resp = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return resp


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _release() -> None:
    """每次合成后把激活缓存还给显卡。模型本身常驻，这里只放每次请求的峰值；
    常驻 worker 不释放的话缓存分配器会把最大那次的块一直攒着（asr 曾因此涨到 34.7G 占满整张卡）。"""
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
            req = json.load(open(cmd["request"], encoding="utf-8"))
            resp = synthesize(req)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            resp = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            _release()
        _write(cmd["response"], resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
