# GitHub Pages

The public demo site is the process canvas (stage / timeline / shot board / similarity views)
for a curated set of cases, plus the leaderboard. It is served from `site/` on `main` through
GitHub Actions.

Layout:

```
site/_canvas/            canvas app, data/*.js and thumbnails
site/<task>/<model>/     assets referenced by the canvas
site/leaderboard/        quantitative leaderboard
site/index.html          redirects to _canvas/index.html
```

## Build

The canvas bundle is produced by the process-canvas tooling (`build.py` +
`make_share.py` in the canvas project) from a built `_canvas` directory:

```bash
python make_share.py --canvas /path/to/runs/_canvas \
  --cases COM-AST-002,NAR-REF-007 \
  --out site --final-dir /path/to/720p-exports --profile slim --jobs 8
```

`--final-dir` is a tree shaped `<dir>/<task>/<model>/final_720p.mp4`; models without a
pre-encoded cut are transcoded from the run directory. The script copies only referenced
assets, rewrites media references in `data/<case>.js`, and keeps the whole bundle near the
slim profile target.

After packaging, sanitize machine paths and internal arm names from `_canvas/data/*.js`,
add the root redirect and `.nojekyll`, keep `leaderboard/` from `sb site`, then commit.

The simpler `sb site` builder (landing + leaderboard + per-run explorer pages) is still
available for metadata-only builds; it writes `explorer/` instead of `_canvas/`.

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

The quantitative leaderboard is generated either from `eval/eval.json` snapshots
(`sb leaderboard`) or from existing machine/human CSV exports (`sb leaderboard-import`).
The currently published board is imported from the v406 machine board (160 tasks, 8 models)
and the 1106 human board; `qwen3.8-27b` is not covered by either and is only shown in the
canvas.

```bash
./sb leaderboard-import \
  --machine exports/.../leaderboard160_v406.csv \
  --human exports/.../human_leaderboard.csv \
  --out site/leaderboard \
  --machine-label "machine v406 (160 tasks)" --human-label "human 1106"
```
