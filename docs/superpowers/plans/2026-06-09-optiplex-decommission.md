# OptiPlex Decommission Implementation Plan

> **For agentic workers:** Part A (operational teardown) is delicate, irreversible, and runs `sudo`/`rm -rf`/tunnel-delete on the LIVE box — execute it **inline** with the gates below, NOT via subagents. Part B (repo cleanup) is a normal PR. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fully retire the OptiPlex's pep-oracle serving / ingest / DNS-rollback-fallback roles (services + cloudflared tunnel + local data), then clean the repo to match.

**Architecture:** Two halves. (A) An ordered, gated operational teardown on the OptiPlex (pre-flight AWS health → verified final B2 backup → remove systemd units → delete cloudflared tunnel → drop `~/.pep-oracle`); each gate aborts on failure and nothing irreversible runs until the backup verifies. (B) A repo PR removing `deploy/`, the stale backfill runbook, and the OptiPlex bits of `CLAUDE.md`.

**Tech Stack:** systemd, cloudflared, rclone (B2 remote `pep-backup`), `curl`, git.

**Spec:** `docs/superpowers/specs/2026-06-09-optiplex-decommission-design.md`

---

## Execution notes

- **Part A is irreversible (full retire).** Run it inline, in order. Each task's verification is a GATE — if it fails, STOP and report; do not proceed to the next task.
- Part A tasks make **no git commits**. Only Part B (Tasks 6–7) commit — those go through the commit gate (`uv run pytest -x -q` + `.claude/.md-reviewed`); `touch .claude/.md-reviewed` before each.
- `PROD=https://pep-oracle.iicapn.com` (the AWS-served prod URL).

---

## Part A — Operational teardown (on the OptiPlex)

### Task 1: Pre-flight — confirm AWS is the healthy sole serving path

GATE: if any check fails, ABORT the entire teardown (the fallback must stay until AWS is confirmed healthy).

- [ ] **Step 1: Health**

Run: `curl -fsS https://pep-oracle.iicapn.com/health`
Expected: `{"status":"ok"}` (exit 0).

- [ ] **Step 2: Version (valid release + corpus served)**

Run: `curl -fsS https://pep-oracle.iicapn.com/version`
Expected: JSON whose `code_semver` matches `^v[0-9]+\.[0-9]+\.[0-9]+` (currently `v1.1.0` or newer) AND contains a `corpus_version` field. Verify:
`curl -fsS https://pep-oracle.iicapn.com/version | python3 -c "import sys,json,re; d=json.load(sys.stdin); assert re.match(r'^v\d+\.\d+\.\d+', d.get('code_semver','')), d; assert d.get('corpus_version'), d; print('version OK:', d['code_semver'], d['corpus_version'])"`
Expected: `version OK: vX.Y.Z vNNNN`.

- [ ] **Step 3: MCP requires auth (product gate intact)**

Run: `curl -s -o /dev/null -w '%{http_code}\n' https://pep-oracle.iicapn.com/mcp`
Expected: `401`.

If all three pass, AWS is confirmed serving prod; proceed. Otherwise STOP.

---

### Task 2: Final off-site backup of the keepers → B2, verified

GATE: do not proceed to Task 3 until the backup is confirmed present on the remote with non-zero size.

- [ ] **Step 1: Find the backup bucket on the remote**

Run: `rclone lsd pep-backup:`
Expected: lists the bucket prior backups used (likely `pep-oracle-backup`). Note its name as `<BUCKET>`.

- [ ] **Step 2: Tar the keepers**

Run:
```bash
tar -czf /tmp/peporacle-decommission-final.tar.gz -C "$HOME" \
  .pep-oracle/cache .pep-oracle/corpus .pep-oracle/oauth.db \
  .pep-oracle/oauth_signing_key .pep-oracle/speaker_profiles.json
ls -l /tmp/peporacle-decommission-final.tar.gz
```
Expected: a multi-hundred-MB tarball (cache/ is ~403M). (Excludes `chroma/`, `topics.json`, `backup/` per the spec.)

- [ ] **Step 3: Copy to B2**

Run: `rclone copy /tmp/peporacle-decommission-final.tar.gz pep-backup:<BUCKET>/decommission-final/ --progress`
Expected: completes with no errors.

- [ ] **Step 4: VERIFY it landed (the gate)**

Run: `rclone ls pep-backup:<BUCKET>/decommission-final/`
Expected: lists `peporacle-decommission-final.tar.gz` with a byte size matching the local file (`ls -l /tmp/peporacle-decommission-final.tar.gz`). If the size is 0 or the file is absent, STOP — do not drop any data.

---

### Task 3: Stop, disable, and remove the systemd units

- [ ] **Step 1: Disable + stop the units**

Run:
```bash
sudo systemctl disable --now pep-oracle-api.service
sudo systemctl disable --now pep-oracle-ingest.timer 2>/dev/null || true
sudo systemctl stop pep-oracle-ingest.service 2>/dev/null || true
```
Expected: api.service stops (removes the `multi-user.target.wants` symlink); no errors.

- [ ] **Step 2: Remove the unit files + reload**

Run:
```bash
sudo rm -f /etc/systemd/system/pep-oracle-api.service \
           /etc/systemd/system/pep-oracle-ingest.service \
           /etc/systemd/system/pep-oracle-ingest.timer
sudo systemctl daemon-reload
```

- [ ] **Step 3: Verify the services are gone (gate)**

Run: `systemctl list-units --all 'pep-oracle*' --no-pager; echo "---"; curl -s --max-time 3 http://localhost:8000/health || echo "server down (expected)"`
Expected: NO `pep-oracle*` units listed, and `server down (expected)` (connection refused). If a unit still shows, investigate before continuing.

---

### Task 4: Disable + delete the cloudflared tunnel

- [ ] **Step 1: Disable the cloudflared service**

Run: `sudo systemctl disable --now cloudflared`
Expected: cloudflared stops; no errors.

- [ ] **Step 2: Delete the named tunnel from Cloudflare**

Run: `cloudflared tunnel delete d04c95e4-6b9f-4694-9140-08022cf37f97`
Expected: "Deleted tunnel ...". If it errors on auth (no account cert available non-interactively), do NOT block the decommission: leave the service disabled, remove the local creds (next step), and record "delete tunnel `d04c95e4-…` from the Cloudflare dashboard" as a manual follow-up.

- [ ] **Step 3: Remove the local cloudflared config + creds**

Run: `sudo rm -f /etc/cloudflared/config.yml /etc/cloudflared/d04c95e4-6b9f-4694-9140-08022cf37f97.json`

- [ ] **Step 4: Verify (gate)**

Run: `systemctl is-active cloudflared; cloudflared tunnel list 2>/dev/null | grep d04c95e4 || echo "tunnel gone"`
Expected: `inactive`, and `tunnel gone` (unless the dashboard-delete follow-up was deferred — note it if so).

---

### Task 5: Drop the local data

GATE: only run after Task 2 Step 4 verified the backup. This is the irreversible drop.

- [ ] **Step 1: Confirm the backup is still verifiable, then drop**

Run:
```bash
rclone ls pep-backup:<BUCKET>/decommission-final/   # re-confirm the tarball is there
rm -rf ~/.pep-oracle
echo "dropped: $(test -e ~/.pep-oracle && echo PRESENT || echo gone)"
```
Expected: tarball listed, then `dropped: gone`.

- [ ] **Step 2: Sanity — this session still works**

Run: `uv run pytest -q 2>&1 | tail -1`
Expected: tests still pass (the suite mocks external state and does not read `~/.pep-oracle`). Confirms dropping the data didn't break the dev loop.

- [ ] **Step 3: Clean up the local tarball**

Run: `rm -f /tmp/peporacle-decommission-final.tar.gz` (it's safely on B2 now).

**Part A complete — the OptiPlex no longer serves, ingests, or tunnels pep-oracle, and the local data is gone (backed up to B2).**

---

## Part B — Repo cleanup (committed → PR)

### Task 6: Delete the obsolete deploy units + stale runbook

**Files:**
- Delete: `deploy/` (the whole directory: `pep-oracle-api.service`, `pep-oracle-ingest.service`, `pep-oracle-ingest.timer`, `restore.md`, `s3-backup-setup.md`)
- Delete: `docs/aws/phase1-backfill-runbook.md`

- [ ] **Step 1: Remove the files**

Run:
```bash
git rm -r deploy/
git rm docs/aws/phase1-backfill-runbook.md
```

- [ ] **Step 2: Confirm nothing surviving references them**

Run: `grep -rnE "deploy/pep-oracle|phase1-backfill-runbook|pep-oracle-api\.service|pep-oracle-ingest\.(service|timer)" --include=*.py --include=*.md --include=*.yml . | grep -v "docs/superpowers/" | grep -v "^./CLAUDE.md"`
Expected: no hits outside `docs/superpowers/` (specs/plans, history — fine) and `CLAUDE.md` (fixed in Task 7). If a workflow or code references a removed unit, investigate.

- [ ] **Step 3: Tests + commit**

Run: `uv run pytest -q 2>&1 | tail -1` → PASS.
```bash
touch .claude/.md-reviewed
git add -A
git status   # confirm only deploy/ + the runbook are removed
git commit -m "chore: remove OptiPlex deploy units + stale phase1 backfill runbook"
```

---

### Task 7: Update CLAUDE.md, then PR

**Files:**
- Modify: `CLAUDE.md` (Deployment section + Hooks section)

- [ ] **Step 1: Read the two sections to edit**

Run: `grep -nA6 "^## Deployment" CLAUDE.md; echo "---"; grep -nA4 "^## Hooks" CLAUDE.md`
This shows the exact current text for the edits below.

- [ ] **Step 2: Edit the Deployment section**

Remove the OptiPlex intro + the two unit bullets at the top of `## Deployment` (the lines describing `deploy/ contains systemd units for the OptiPlex fallback box`, `pep-oracle-api.service — ... DNS-rollback fallback`, and `pep-oracle-ingest.service + pep-oracle-ingest.timer — ...`). KEEP the `**AWS prod (infra/, CDK Python ...):**` block and everything under it. The section should open directly with a one-line lead-in to the AWS prod content, e.g. replace the removed lead-in with: `The product runs entirely on AWS (`infra/`, CDK Python; isolated `infra/.venv`, excluded from root pytest via `--ignore=infra`, tested with `cd infra && .venv/bin/python -m pytest`):` if that framing isn't already present in the AWS-prod bullet.

- [ ] **Step 3: Edit the Hooks section**

Remove the second bullet describing the OptiPlex-only `restart-server.sh` `PostToolUse` hook (it was deleted in PR #15; `CLAUDE.md` shouldn't still document it). KEEP the first bullet (the `pre-commit.sh` gate).

- [ ] **Step 4: claude-md-improver + size check**

Invoke `/claude-md-improver` to validate the edited `CLAUDE.md` against the repo (no references to removed `deploy/`/units/restart-server.sh remain) and apply any targeted fixes. Then `wc -l CLAUDE.md` → ≤300.

- [ ] **Step 5: Tests + commit**

Run: `uv run ruff check . && uv run pytest -q 2>&1 | tail -1` → PASS.
```bash
touch .claude/.md-reviewed   # ensure fresh AFTER the CLAUDE.md edits + improver
git add CLAUDE.md
git commit -m "docs: drop OptiPlex fallback + stale restart-server.sh from CLAUDE.md"
```

- [ ] **Step 6: Push + open the PR**

Run:
```bash
git push -u origin optiplex-decommission
gh pr create --base main --title "Decommission the OptiPlex (repo cleanup)" \
  --body "Removes the obsolete OptiPlex deploy units, the stale phase1 backfill runbook, and the OptiPlex-fallback + restart-server.sh mentions from CLAUDE.md. The operational teardown (services, cloudflared tunnel, ~/.pep-oracle data) was done out-of-band per the spec; the product is AWS-only (v1.1.0 live). Spec + plan in docs/superpowers/."
```

CI is the remote gate (ruff + pytest + infra pytest + cdk synth + docker builds).

---

## Self-review notes (coverage vs spec)

- Part A operational sequence (spec) → Tasks 1–5, in the spec's strict order, each gated: pre-flight (T1), backup+verify (T2), unit teardown (T3), cloudflared disable+delete (T4), drop data (T5). The "abort if AWS unhealthy" and "don't drop before backup verifies" gates are explicit in T1 and T5/T2.
- `cloudflared tunnel delete` auth risk (spec) → T4 Step 2 fallback (disable + remove creds + dashboard follow-up).
- B2 bucket-path risk (spec) → T2 Step 1 discovers `<BUCKET>` via `rclone lsd`.
- `/version` semver risk (spec) → T1 Step 2 asserts a valid `v#.#.#` + `corpus_version`, not the literal `v1.1.0`.
- Part B repo cleanup (spec) → T6 (delete `deploy/` + runbook) + T7 (CLAUDE.md Deployment + Hooks, via claude-md-improver).
- Out of scope (spec) → not implemented: wiping the box, minting the scoped dev AWS credential.
