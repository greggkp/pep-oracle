# OptiPlex decommission — design spec

**Date:** 2026-06-09
**Status:** Approved (brainstorm complete; ready for implementation plan)
**Related:** `project_portable_dev_env` memory, `2026-06-09-cli-gui-removal-design.md`, `2026-06-09-portable-dev-env-design.md`

## Goal

Retire the OptiPlex's pep-oracle **serving / ingest / DNS-rollback-fallback** roles
now that the product is AWS-only (MCP Lambda + Fargate ingestion, live as `v1.1.0`)
and dev is portable + verified in the Claude cloud env. This is a **full retire**:
stop, disable, and remove the local services + the cloudflared tunnel, and drop the
local data after one final off-site backup. No rollback target remains.

**Not** in scope: wiping the box (it stays usable as a dev box) and minting the
scoped dev AWS credential / rotating `optiplex-cli` keys (a separate gated AWS op).

## Decisions (approved)

- **Full retire** — no cold standby; the rollback target is removed.
- **Final backup, then drop** — one last `rclone copy` of the keepers to B2, verified,
  before dropping `~/.pep-oracle`.
- **Cloudflare tunnel: delete** — `cloudflared tunnel delete` removes the orphan from
  the Cloudflare account (not just disable the local service).
- **`docs/aws/phase1-backfill-runbook.md`: delete** (stale; references removed
  `export`/`backfill`; the one-time backfill is long done).
- **Nothing preserved on the box** beyond the B2 backup.

## Current state (verified 2026-06-09)

- `pep-oracle-api.service` — active + enabled, serving `localhost:8000` from
  `~/.pep-oracle/corpus` (the rollback fallback). Installed at
  `/etc/systemd/system/pep-oracle-api.service` (+ a `multi-user.target.wants` symlink).
- `pep-oracle-ingest.service` (static) + `pep-oracle-ingest.timer` (already disabled) —
  obsolete (the `ingest` command they called was removed); installed in
  `/etc/systemd/system/`. `pep-oracle-backup.service` already gone.
- `cloudflared` — active; `/etc/cloudflared/config.yml` has a **single** ingress
  (`pep-oracle.iicapn.com → http://127.0.0.1:8000`, else `http_status:404`), tunnel id
  `d04c95e4-6b9f-4694-9140-08022cf37f97`, creds at
  `/etc/cloudflared/d04c95e4-…json`. Since `pep-oracle.iicapn.com` is NS-delegated to
  AWS Route 53, this tunnel is **not** receiving prod traffic — it's a cold rollback target.
- `~/.pep-oracle/` (~810 MB): `cache/` 403M (transcript+diarization), `chroma/` 142M
  (**unused** by AWS-only code), `backup/` 111M (already-off-site tarballs), `corpus/` 17M,
  `oauth.db` 36K, `oauth_signing_key`, `speaker_profiles.json` 20K, `topics.json` (GUI-era, unused).
- `rclone` v1.74.2 installed; B2 remote name is **`pep-backup`**.
- Passwordless `sudo` available (the old restart hook used `sudo -n systemctl`).

## Part A — Operational teardown (run on the OptiPlex; NOT committed)

Strict order; each step gates the next.

1. **Pre-flight: confirm AWS is the healthy sole serving path.**
   - `curl -fsS https://pep-oracle.iicapn.com/health` → `{"status":"ok"}`
   - `curl -fsS https://pep-oracle.iicapn.com/version` → `code_semver` = `v1.1.0`, has a `corpus_version`
   - `curl -s -o /dev/null -w '%{http_code}' https://pep-oracle.iicapn.com/mcp` (no token) → `401`
   - **Abort the whole teardown if any check fails.**

2. **Final off-site backup of the keepers → B2, then verify.**
   - Tar the keepers and copy to the remote:
     `tar -czf /tmp/peporacle-decommission-final.tar.gz -C ~ .pep-oracle/cache .pep-oracle/corpus .pep-oracle/oauth.db .pep-oracle/oauth_signing_key .pep-oracle/speaker_profiles.json`
     `rclone copy /tmp/peporacle-decommission-final.tar.gz pep-backup:pep-oracle-backup/decommission-final/ --progress`
   - **Verify it landed** (non-zero size, matching bytes):
     `rclone ls pep-backup:pep-oracle-backup/decommission-final/`
   - Exclude `chroma/` (unused), `topics.json` (GUI-era), `backup/` (those tarballs are already off-site).

3. **Stop + disable + remove the systemd units.**
   - `sudo systemctl disable --now pep-oracle-api.service`
   - `sudo systemctl disable --now pep-oracle-ingest.timer`  (already disabled; harmless)
   - `sudo systemctl stop pep-oracle-ingest.service 2>/dev/null || true`
   - `sudo rm -f /etc/systemd/system/pep-oracle-api.service /etc/systemd/system/pep-oracle-ingest.service /etc/systemd/system/pep-oracle-ingest.timer`
   - `sudo systemctl daemon-reload`
   - **Verify:** `systemctl list-units --all 'pep-oracle*'` → none; `curl -s --max-time 3 localhost:8000/health` → fails (connection refused).

4. **Disable + delete the cloudflared tunnel.**
   - `sudo systemctl disable --now cloudflared`
   - `cloudflared tunnel delete d04c95e4-6b9f-4694-9140-08022cf37f97` (removes the orphan from Cloudflare; run as the user whose `cloudflared` is logged in — may need the cert/origin token).
   - `sudo rm -f /etc/cloudflared/d04c95e4-6b9f-4694-9140-08022cf37f97.json /etc/cloudflared/config.yml`
   - **Verify:** `systemctl is-active cloudflared` → inactive; `cloudflared tunnel list` no longer shows the id.

5. **Drop the local data** (only after step 2 verified): `rm -rf ~/.pep-oracle`.
   - Safe for this session: the repo (`/opt/pep-oracle/app`) is separate; tests mock external state and don't read `~/.pep-oracle`.

## Part B — Repo cleanup (committed → PR)

1. **Delete `deploy/`** entirely — `pep-oracle-api.service`, `pep-oracle-ingest.service`,
   `pep-oracle-ingest.timer`, `restore.md`, `s3-backup-setup.md` are all obsolete after retire.
2. **Delete `docs/aws/phase1-backfill-runbook.md`.**
3. **`CLAUDE.md`:**
   - Deployment section: remove the "`deploy/` … OptiPlex fallback box" bullets
     (`pep-oracle-api.service` / `pep-oracle-ingest.*`). Keep the AWS prod (Phases 2c/3/4) content.
   - Hooks section: remove the **stale `restart-server.sh`** bullet (that hook was deleted in
     PR #15 but `CLAUDE.md` still describes it). Keep the `pre-commit.sh` gate description.
   - Run through `/claude-md-improver`; stays ≤300 lines.
4. Commit (the `CLAUDE.md` change goes through the commit gate), push, open a PR.

## Safety / verification

- Full retire is irreversible by design. Mitigations: the pre-flight AWS health gate
  (step 1), the **verified** final backup (step 2 — data recoverable from B2), and the
  code + CDK in-repo (a fresh fallback could be redeployed if ever needed).
- Order is strict: pre-flight → backup+verify → unit teardown → cloudflared delete →
  drop data. Do not drop data before the backup verifies.
- Part B (repo) is independent of Part A and can land before or after; recommended after,
  so the repo matches the retired reality.

## Risks / things to confirm during implementation

- **`cloudflared tunnel delete` auth**: deleting a named tunnel needs the account
  cert/credentials `cloudflared` was set up with. If the CLI can't authenticate
  non-interactively, fall back to disabling the service + removing the local creds, and
  delete the tunnel from the Cloudflare dashboard manually (note it for the operator).
- **B2 remote path**: confirm the exact `pep-backup:` bucket/path used by prior backups
  (`rclone lsd pep-backup:`) and copy into the same bucket so it's findable later.
- **`/version` semver**: if a newer release than `v1.1.0` has shipped by execution time,
  the pre-flight should assert "a valid semver + corpus block," not the literal `v1.1.0`.
