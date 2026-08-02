---
name: claude-md-improver
description: Review CLAUDE.md against the current state of the codebase — verify every factual claim it makes still holds, fix drift, and record the review. Run before committing changes that touch architecture, commands, environment variables, deployment, or infra alarms. Required by .claude/hooks/pre-commit.sh, which blocks `git commit` until this has run.
---

# CLAUDE.md reviewer

CLAUDE.md is loaded into context at the start of every session in this repo. Wrong
guidance there is worse than no guidance: it is stated authoritatively and gets acted
on without verification. The job of this review is to make sure every claim in the file
is still true, and that the file earns its length.

The pre-commit hook consumes the review flag on each successful commit, so this runs
once per commit that changes anything CLAUDE.md describes.

## 1. Establish scope

Find what changed since the file was last accurate:

```bash
git status --short
git diff HEAD --stat
git log --oneline -15
```

Read CLAUDE.md in full. It is long; read all of it, not the sections that look
relevant — drift usually sits in a section nobody thought to open.

## 2. Verify claims against the code

CLAUDE.md is dense with specifics. Each is a testable assertion. Check them — do not
assume a claim holds because it reads plausibly, and do not "fix" a claim you have not
verified against the code.

**Commands** — every command in the `## Commands` block must exist and be spelled
correctly. Console scripts come from `[project.scripts]` in `pyproject.toml`;
subcommands from `src/pep_oracle/cli.py`. Flags like `--backfill` and `--corpus` must
be real parameters.

**Environment variables** — every `PEP_ORACLE_*` / `BEDROCK_*` / `MODAL_*` name in the
`## Environment` section must be read somewhere in `src/` or `infra/`. Grep each one.
Both directions matter: a documented variable the code no longer reads, and a variable
the code reads that is undocumented.

**Architecture and key design decisions** — module paths, function names, class names,
and constants cited in prose (`hybrid_search`, `store._chunk_metadata`,
`SEMANTIC_WEIGHT=0.8`, `VOICE_MATCH_MAX_DISTANCE=0.5`, `HALF_LIFE_DAYS`,
`SEARCH_TOOL_NAME`, `CORPUS_REFRESH_TTL_SECONDS`) must exist with the stated values.
Numeric constants drift silently — check the number, not just the name.

**Deployment and infra** — stack names, construct IDs, alarm names, metric names,
schedule rates, and IAM/permission claims must match `infra/pep_oracle_infra/`. Alarms
are a common drift point: an alarm that was renamed or whose metric changed leaves the
prose describing monitoring that no longer exists.

**Workflows** — job names, triggers, and the OIDC trust conditions described for
`.github/workflows/` must match the YAML.

**File paths** — every path referenced (`docs/aws/*.md`, `tests/*.py`,
`cloud/*.py`, `speaker_profiles.json`) must exist.

Use `Grep` and `Read` for these. Where a claim is cheap to execute, execute it.

## 3. Find what is missing

Look at the recent diff for changes that CLAUDE.md should describe but doesn't. The
bar: **would a future session make a worse decision without knowing this?** Things that
clear it are non-obvious constraints, gotchas that cost real debugging time, and the
reasoning behind a choice that looks arbitrary in the code.

Things that do not clear it: what the code plainly says, a changelog of what was fixed,
or restating a function's behavior. CLAUDE.md is not a commit log.

## 4. Prune

Length is a real cost — it is paid on every session. Remove:

- Guidance about code that no longer exists
- Restatement of what the surrounding code makes obvious
- Duplication across sections (the same fact stated in Architecture and again in Key
  design decisions)

Preserve the *why* behind decisions even when it is verbose. The explanations of the
temporal event horizon, why `max_speakers` must not be capped, why the MCP tool name is
front-loaded, and why the corpus stays lazy are load-bearing — they exist because the
obvious alternative is wrong and someone will otherwise "fix" it back.

## 5. Apply and report

Edit CLAUDE.md directly. Then report to the user:

- **Drift corrected** — claim, what it actually is now
- **Added** — what, and why it clears the bar
- **Removed** — what, and why it does not
- **Unverifiable** — anything you could not check, stated plainly rather than assumed

If the review found nothing to change, say so — that is a valid outcome, not a failure
to look hard enough.

## 6. Record the review

Only after the edits are complete:

```bash
touch .claude/.md-reviewed
```

Then remind the user to stage CLAUDE.md alongside their other changes, so the
documentation and the code it describes land in the same commit. The hook deletes the
flag on a successful commit, so the next commit requires a fresh review.
