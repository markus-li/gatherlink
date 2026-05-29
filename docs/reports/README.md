# Reports Directory

This directory is for release roadmaps, closed audit reports, lab result
summaries, and future pipeline notes. It is not the primary spec location.

When a report contains a still-current decision, promote that decision into the
owning docs listed in [`docs/README.md`](../README.md).

## Current Files

| File | Status | Purpose |
| --- | --- | --- |
| [`v0.9-roadmap.md`](v0.9-roadmap.md) | closed release roadmap | Initial implementation order and acceptance checklist; reusable process guidance lives in [`docs/operations/release-development-process.md`](../operations/release-development-process.md). |
| [`v0.9-code-audit-followups.md`](v0.9-code-audit-followups.md) | audit handoff | Tracks source/docs findings, closure evidence, and unresolved mismatches from the early audit period. |
| [`v0.9.1-roadmap.md`](v0.9.1-roadmap.md) | closed release roadmap | Hardening and small-site operations roadmap after the initial baseline. |
| [`v0.9.2-roadmap.md`](v0.9.2-roadmap.md) | closed release evidence trail | Stability, cleanliness, polish, performance, and final gate evidence for that release. |
| [`v0.9.3-roadmap.md`](v0.9.3-roadmap.md) | closed release roadmap | Adaptive performance and real-world operation roadmap after the performance baseline. |
| [`v0.9.4-roadmap.md`](v0.9.4-roadmap.md) | active release roadmap | Public-readiness, packaging, secure local REST API, first-run setup, and support-intake roadmap. |
| [`v0.9.5-roadmap.md`](v0.9.5-roadmap.md) | future release candidate | OpenWrt feasibility and compatibility-backend planning, not active implementation authorization yet. |
| [`future-roadmap-pipeline.md`](future-roadmap-pipeline.md) | future pipeline | Shaped future ideas that are not release authorization by themselves. |
| [`three-path-scheduler-lab.md`](three-path-scheduler-lab.md) | historical lab report | Scheduler matrix and saved three-path lab evidence from the scheduler implementation period. |
| [`mvp-implementation-priorities-closed.md`](mvp-implementation-priorities-closed.md) | closed historical handoff | Former next-priority note from the MVP build; retained only as rationale/history. |

## Cleanup Rules

- Do not keep active release gates only in reports; link or promote them to
  [`docs/operations/release-checklist.md`](../operations/release-checklist.md) or the relevant release checklist.
- Do not keep stale "next priority" docs with current-sounding filenames.
- Do not commit private VM hosts, personal keys, inventories, or generated local
  reports here.
- Prefer one roadmap per active release plus one future pipeline.
- Keep historical reports clearly marked as closed or historical.
- Follow [`docs/operations/documentation-maintenance.md`](../operations/documentation-maintenance.md) for duplication,
  canonical-linking, volatile-fact, and TODO rules.
