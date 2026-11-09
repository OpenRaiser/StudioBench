# GitHub Pages

The public demo site is a static build of the leaderboard and case explorer plus a curated
set of final cuts. It is served from the `site/` directory on `main` through GitHub Actions.

## Build

```bash
./sb site --runs /path/to/runs \
  --out site \
  --cases COM-AST-002,NAR-REF-007,MUS-AST-001 \
  --models claude-opus-5-5,gemini-4-argon,gpt-6-astra,qwen3.8-27b \
  --media-from /path/to/720p-exports \
  --profile 540 --max-size-mb 900
```

- `--media-from` may be a comma-separated list of trees shaped
  `<root>/<task>/<model>/final_720p.mp4`; without it the builder transcodes `run/final.mp4`.
- The builder copies/transcodes only the selected runs, stops before `--max-size-mb`, and
  records skipped runs in `site/site.json`.
- Output: `index.html`, `leaderboard/`, `explorer/`, `media/<task>/<model>/final.mp4`.

## Publish

Commit `site/` and push to `main`. `.github/workflows/pages.yml` uploads it as the Pages
artifact and deploys. Pages must be enabled for the repository with **Source: GitHub Actions**
(Settings → Pages), or via:

```bash
gh api -X POST repos/OWNER/REPO/pages -f build_type=workflow
```

## Limits

- Published site: 1 GB. Keep the curated set comfortably below that.
- Bandwidth: ~100 GB/month soft limit; 540p CRF31 keeps a 3-case / 6-model set around 100 MB.
- No server-side code: the site is static HTML/JS/CSS. `final.mp4` files are served as static
  assets.
- Private repositories require a paid GitHub plan for Pages; this repository is public.

## Notes

The quantitative leaderboard is generated from `eval/eval.json` snapshots. Runs without an
eval snapshot still appear in the explorer, but the leaderboard page stays empty until those
runs are scored.
