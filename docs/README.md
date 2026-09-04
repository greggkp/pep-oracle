# Documentation guide

Use this page to distinguish current repository guidance from implementation history.
The AWS service was fully decommissioned on 2026-09-04. Completed migration and
operations records are retained for design context, but their commands and
infrastructure assumptions must not be treated as a live environment.

## Current guidance

- [Project overview and quickstart](../README.md)
- [Development setup](../SETUP.md)
- [Architecture and repository working agreements](../AGENTS.md)
- [Decommissioning status and cost record](aws/hibernation-runbook.md#status)
- [Modal transcription and diarization](../cloud/README.md)
- [Security hardening status](security-hardening.md)
- [Cold-path measurements](aws/cold-path-measurement.md)
- [Prebuilt BM25 index](aws/prebuilt-bm25-index.md)

## Historical records

The Phase 2 and Phase 3 runbooks document the completed migration from the retired
OptiPlex/ChromaDB deployment to AWS. The Phase 4 runbook documents the former release
and rollback process, and the hibernation runbook retains the design that preceded
full teardown. The temporal-reranking and voice-speaker-ID plans describe features
that have since been implemented. Files under `superpowers/` are design and
implementation records from earlier work.

Do not execute commands from these records as current deployment, rollback, or
maintenance procedures. Start with the current guidance above and inspect the code
and disabled workflows when exact historical behavior matters.
