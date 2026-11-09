# Mock end-to-end example

This example exercises the harness without model weights or network access:

```bash
pip install -e ".[dev]"
./sb preflight
./sb selftest --mock
```

What it does:

1. `preflight` builds the tool registry, marks real backends unavailable, and checks
   that ffmpeg is reachable.
2. `selftest --mock` starts the MCP server with mock generation backends and calls every tool
   once through the same JSON-RPC path the evaluated model uses. Editing, inspection and
   export are real ffmpeg operations; generated clips are placeholders.

To drive a full task with an LLM you need the Claude Code CLI and an Anthropic-compatible
gateway. Then:

```bash
export STUDIO_BACKEND=mock
./sb run --task tasks/demo/DEMO-001.json --model claude-opus-5-5 --mock
```

Mock runs are always marked `valid_for_bench=false` and must not be reported on a leaderboard.
