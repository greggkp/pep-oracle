# Documentation guide

Use this page to distinguish current operating guidance from implementation history.
Completed migration records are retained for design context, but their commands and
infrastructure assumptions must not be used to operate the current service.

## Current guidance

- [Project overview and quickstart](../README.md)
- [Development setup](../SETUP.md)
- [Architecture and repository working agreements](../AGENTS.md)
- [Release and rollback runbook](aws/phase4-cicd-runbook.md)
- [Modal transcription and diarization](../cloud/README.md)
- [Security hardening status](security-hardening.md)
- [Cold-path measurements](aws/cold-path-measurement.md)
- [Prebuilt BM25 index](aws/prebuilt-bm25-index.md)

## Historical records

The Phase 2 and Phase 3 runbooks document the completed migration from the retired
OptiPlex/ChromaDB deployment to AWS. The temporal-reranking and voice-speaker-ID plans
describe features that have since been implemented. Files under `superpowers/` are
design and implementation records from earlier work.

Do not execute commands from these records as current deployment, rollback, or
maintenance procedures. Start with the current guidance above and inspect the live
code and workflows when exact behavior matters.
