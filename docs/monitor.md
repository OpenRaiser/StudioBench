# Monitor

A read-only web dashboard for a run tree. It never writes to the runs and never talks to a
scheduler; it only reads `result.json`, `eval/eval.json` and `work/.calls.jsonl`.

```bash
python -m studiobench monitor --runs ./runs --host 0.0.0.0 --port 8132
```

- `/` — dashboard (auto-refreshes every 5 s)
- `/api/status` — the same data as JSON
- `/healthz` — liveness

Status values:

| status | meaning |
|---|---|
| `running` | run directory exists (has `prompt.txt`) but no `result.json` yet |
| `done` | submitted and `valid_for_bench=true` |
| `invalid` | submitted but flagged invalid (mock backend, missing tool, isolation failure) |
| `unsubmitted` | finished without calling `export` |
| `failed` | non-zero return code |

The dashboard shows totals, per-model counts, average wall time, tool-call totals and errors,
estimated model cost (`result.json.llm.total_cost_usd`), per-tool usage, and recent runs.
Results are cached for `STUDIO_MONITOR_TTL` seconds (default 10).

The monitor is intentionally dependency-free (standard library only) so it can run next to
the harness on any machine that can read the run tree.
