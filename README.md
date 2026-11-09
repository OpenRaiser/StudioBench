# StudioBench

StudioBench evaluates whether a general-purpose foundation model can independently produce a
real, deliverable short video. The model gets a brief and a fixed set of atomic media tools
(generation, editing, inspection, delivery) — no shell, no file system, no outside help.
It must plan, generate assets, edit, inspect its own work, retry, and export a final cut.

This repository contains the evaluation harness, the tool layer, the task format, the metric
implementations, and the leaderboard generator.

> Status: early public release. The core pipeline runs; packaging and docs are being cleaned up.

## What is in here

```
studiobench/
  harness/      isolated runs, batch queue, resume, preflight, selftest
  tools/        MCP server + 33 atomic media tools + model backends (mock included)
  eval/         hard gates, checklist judge, VBench, audio / A-V sync, aggregation
  leaderboard/  snapshot aggregation and static leaderboard
  monitor/      read-only web dashboard for a run tree
  explorer/     static case pages (calls, files, final cut)
  pool/         optional multi-GPU tool pool planner
  results.py    run discovery and the snapshot row format
  sp.md         system prompt used by the evaluated model
tasks/          task format and demo tasks
docs/           protocol, tool contract, metrics, leaderboard
examples/       runnable examples with mock backends
tests/          import / schema / scoring tests (no GPU required)
```

## Quick start (no GPU)

```bash
git clone https://github.com/OpenRaiser/StudioBench.git
cd StudioBench
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[frames,ffmpeg]"

# 1. Check that the tool layer is importable and that ffmpeg is present
./sb preflight

# 2. Exercise every tool through the MCP server with mock generation backends
./sb selftest --mock

# 3. Run the scripted mock pipeline end to end (tools -> final.mp4 -> hard-gate eval)
./sb demo --out ./runs

# 4. Inspect the run tree
./sb monitor --runs ./runs --port 8132       # web dashboard
./sb explorer --runs ./runs --out ./explorer_site   # static case pages
./sb leaderboard --runs ./runs --out ./board_site    # static leaderboard
./sb site --runs ./runs --out ./site --cases DEMO-TINY  # combined Pages build
```

Running a task end-to-end (`sb run`) drives an evaluated model through the Claude Code CLI
with a strict MCP configuration. See `docs/protocol.md`. Set `STUDIO_BACKEND=mock` to run the
whole loop without model weights; results are marked `valid_for_bench=false`.

## Tasks

A task is a small JSON document:

```json
{
  "id": "DEMO-001",
  "domain": "commercial",
  "task": "open",
  "audio_profile": ["music"],
  "wish": "A 60-second warm title sequence ...",
  "duration_sec": [60, 75],
  "aspect": "16:9",
  "must": [], "forbid": [], "constraints": [], "plot_beats": [], "facts": [],
  "assets": [], "references": [], "given_audio": null
}
```

`docs/protocol.md` describes the eight domains, five task families, the user message the model
sees, and what the model must never see.

## Backends

Every generation tool has a pluggable backend. The public repository ships:

- `mock` — deterministic placeholders, no weights, no network;
- `ffmpeg` tools — editing, inspection, delivery (real);
- optional local backends for image / video / TTS / music / SFX / lip-sync / ASR;
- an HTTP-job backend for any provider that speaks a simple submit/poll protocol.

Backends are configured through environment variables (see `config.example.env`). Nothing is
enabled unless the corresponding variables are set; unavailable tools are marked as such in
`tools/list` and the run is flagged `valid_for_bench=false`.

## Metrics

Four main scores: film (checklist + hard duration rule), visual (VBench), audio, and audio-visual
sync. Hard gates (decodable, duration, aspect, audio track, not silent) are computed first.
Metric definitions and aggregation rules are in `docs/metrics.md`.

## Public demo

A curated static build (leaderboard + case explorer + selected final cuts) is published with
GitHub Pages from `site/`. Build and publishing steps, media curation and size limits are in
`docs/pages.md`.

## Leaderboard

`studiobench.leaderboard` turns a snapshot directory of evaluation results into a static site.
The repository ships the generator and the schema, not production data. See
`docs/leaderboard.md`.

## License

Code: Apache-2.0. Task text and documentation are released under CC BY 4.0 unless stated
otherwise. Third-party reference videos are **not** redistributed; tasks only carry brief text.

## Contributing

Please open an issue before large changes. Run `pytest` and `./sb selftest --mock` before
sending a pull request.
