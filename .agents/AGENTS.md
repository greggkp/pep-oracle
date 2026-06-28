# PEP Oracle Agent Guidelines

These rules were adapted from the existing `CLAUDE.md` file in the project root and from recent troubleshooting sessions.

## Environment & Commands
- **Dependency Management**: This project uses `uv`. The virtual environment is managed automatically. Do not manually activate venvs or use `pip` directly unless specifying `uv pip`. If `uv` is not on PATH, install it persistently (`curl -LsSf https://astral.sh/uv/install.sh | sh`) — do NOT rely on a `/tmp` install, which is wiped on reboot.
- **Testing**: Run tests using `uv run pytest`. To run infra tests, use `cd infra && uv run pytest -q`. (Exclude live tests by default; they require AWS creds/Mocks).
- **Linting & Typing**: 
  - Code must pass `ruff format` and `ruff check`. Run `uv run ruff format .` and `uv run ruff check --fix .`.
  - Type checking is enforced with `mypy src` (run via `uv run mypy src`). `check_untyped_defs = true` is explicitly enabled.
- **Secret Scanning**: The project uses `gitleaks` in CI. If adding mock secrets to tests, ensure you whitelist their specific git fingerprint (e.g. `commit_sha:file_path:rule:line_num`) in `.gitleaksignore`. Do NOT use a TOML `.gitleaks.toml` syntax in the `.gitleaksignore` file.

## CI & Git Gotchas
- **Gitleaks CI crash**: Gitleaks in GitHub Actions uses `git log base_commit..head_commit` for secret scanning. Always ensure `actions/checkout` uses `fetch-depth: 0` in the CI configuration; otherwise, gitleaks will fail with an "unknown revision" fatal error because the base commit was not downloaded.
- **Ruff Format vs Mypy**: When fixing typing issues or adding `# type: ignore[...]` pragmas, ALWAYS run `uv run ruff format .` locally. Ruff can wrap lines and push the `# type: ignore` comment to the next line, causing Mypy to still fail on the original line. Always run both format and mypy back-to-back to verify.

## Architecture Context
- **MCP Server**: The core product is a Model Context Protocol (MCP) server that exposes a single tool (`search_us_politics_commentary`) for a frontier model to retrieve grounded podcast excerpts.
- **Authentication**: JWT bearer verification against an in-app OAuth 2.1 provider (`oauth.py`). The server handles token minting, rotation, and revocation.
- **Ingestion**: Fetches RSS feeds, transcribes audio (using faster-whisper on Modal GPUs), diarizes speakers (pyannote on Modal GPUs), chunks text, generates AWS Bedrock Titan embeddings, and publishes an atomic corpus artifact (Parquet).
- **Search**: Uses a hybrid BM25 and semantic (Bedrock embeddings) approach, scored with weighted Reciprocal Rank Fusion, followed by intent-gated temporal reranking (e.g., exponential recency decay).

## Development Workflows
- **Infra (CDK)**: Infrastructure is managed in `infra/` using AWS CDK Python. The CI handles deployments (`.github/workflows/deploy.yml`) on `v*` tags.
- **Local File State**: Persistent data (SQLite OAuth DB, speaker profiles, transcripts) defaults to `~/.pep-oracle/`.
- **Docker**: Do not use Docker locally if the `docker` command isn't present in the environment; fallback to `uv` directly.
