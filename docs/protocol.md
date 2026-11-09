# Evaluation protocol

This document is the normative description of a StudioBench run. Code lives in
`studiobench/harness/`; task validation in `studiobench/harness/taskspec.py`.

## 1. Task format

A task is a JSON object with these fields:

| field | required | meaning |
|---|---|---|
| `id` | yes | stable task id, e.g. `DEMO-001` |
| `domain` | yes | one of `narrative`, `commercial`, `product`, `education`, `music`, `documentary`, `creator`, `dialogue` |
| `task` | yes | one of `open`, `constraint`, `assets`, `reference`, `audio-led` |
| `wish` | yes | the creative brief shown to the model |
| `duration_sec` | yes | `[min, max]`, inside 60–180 s |
| `aspect` | yes | e.g. `16:9`, `9:16`, `1:1` |
| `audio_profile` | recommended | non-empty subset of `speech`, `music`, `sfx`, `dialogue` |
| `must` | optional | checklist items that must be true in the final cut |
| `forbid` | optional | checklist items; any hit zeroes the film score |
| `constraints` | optional | additional checklist items |
| `plot_beats` | optional | expected narrative beats |
| `facts` | optional | factual claims to verify |
| `assets` | optional | input asset paths, required for `task=assets` |
| `references` | optional | reference paths, required for `task=reference` |
| `given_audio` | optional | fixed audio track, used by `audio-led` |

`must`, `forbid`, `constraints`, `plot_beats` and `facts` are **never shown to the model**;
they are used only by the judge. The model sees `wish`, input assets, references, the given
audio, the duration range, and the aspect ratio.

## 2. Isolation

A run drives the evaluated model through the Claude Code CLI with:

- `--tools ""` — no built-in shell, file, or search tools;
- `--strict-mcp-config` — only the StudioBench MCP server;
- `--system-prompt-file studiobench/sp.md` — the benchmark system prompt, not the default
  coding-assistant prompt;
- a fresh `HOME` and `--setting-sources project` so user-level instructions do not leak in;
- a per-task wall clock (4 h for tasks under 2 minutes, 8 h otherwise) and a call budget (200).

Every run records which tools were actually available and whether any non-StudioBench tool
appeared; a run is `valid_for_bench=false` if isolation or backend checks fail.

## 3. Artifacts

```
runs/<batch>/<task>/<model>/
  prompt.txt        the exact user message
  sp.md             the system prompt used
  result.json       model, timings, budget, tool call log summary, validity flags
  final.mp4         the submitted cut (produced only by the export tool)
  mcp.json          MCP config (no secrets)
  .calls.jsonl      every tool call: name, arguments, backend, output, duration, ok/error
```

A run is submitted only if the `export` tool was called. `valid_for_bench` additionally
requires: submitted, no unexpected tools, all required backends available, and the final file
decodable.

## 4. Scoring

Scoring is optional and separate from inference. See `metrics.md`.
