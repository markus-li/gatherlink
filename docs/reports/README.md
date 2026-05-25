# Reports Directory

This directory is for release roadmaps, closed audit reports, lab result
summaries, and future pipeline notes. It is not the primary spec location.

When a report contains a still-current decision, promote that decision into the
canonical docs under `docs/architecture`, `docs/protocol`, `docs/runtime`,
`docs/helpers`, `docs/operations`, or `docs/labs`.

## Current Files

| File | Status | Purpose |
| --- | --- | --- |
| `v0.9-roadmap.md` | closed release roadmap | V0.9 implementation order and acceptance checklist; reusable process guidance lives in `docs/operations/release-development-process.md`. |
| `v0.9-code-audit-followups.md` | v0.9 audit handoff | Tracks source/docs findings, closure evidence, and any unresolved v0.9 mismatches. |
| `v0.9.1-roadmap.md` | closed release roadmap | V0.9.1 hardening and small-site operations roadmap after the v0.9.0 baseline. |
| `v0.9.2-roadmap.md` | release-candidate evidence trail | V0.9.2 stability, cleanliness, and polish roadmap plus final gate evidence. |
| `future-roadmap-pipeline.md` | future pipeline | Shaped future ideas that are not release authorization by themselves. |
| `three-path-scheduler-lab.md` | historical lab report | Scheduler matrix and saved three-path lab evidence from the scheduler implementation period. |
| `mvp-implementation-priorities-closed.md` | closed historical handoff | Former next-priority note from the MVP build; retained only as rationale/history. |

## Cleanup Rules

- Do not keep active release gates only in reports; link or promote them to
  `docs/operations/v0.9-release-checklist.md` or the relevant release checklist.
- Do not keep stale "next priority" docs with current-sounding filenames.
- Do not commit private VM hosts, personal keys, inventories, or generated local
  reports here.
- Prefer one roadmap per active release plus one future pipeline.
- Keep historical reports clearly marked as closed or historical.
- Follow `docs/operations/documentation-maintenance.md` for duplication,
  canonical-linking, volatile-fact, and TODO rules.
