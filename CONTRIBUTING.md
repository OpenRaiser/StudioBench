# Contributing

Thanks for taking a look. A few practical notes:

1. Open an issue before a large change so we can agree on the shape.
2. Keep the tool contract stable: names, arguments, return fields and file conventions are
   consumed by both the evaluator and external editors. If you must change one, update
   `docs/tool-contract.md` and call it out in the pull request.
3. Tests must pass without a GPU:
   ```bash
   pip install -e ".[dev]"
   pytest -q
   ./sb selftest --mock
   ```
   `selftest --mock` reports tools that are unavailable on your machine as `SKIP`, not failure.
4. Never commit run artifacts, weights, credentials or internal endpoints. Use
   `config.example.env` and environment variables.
5. New tasks need a synthetic brief or cleared rights. Do not add third-party videos, links
   to them, or frames from them.
