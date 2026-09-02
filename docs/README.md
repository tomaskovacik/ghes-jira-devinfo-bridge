# docs

Reference material for the Jira Cloud **Development Information API** ("devinfo")
that this bridge writes to. Captured 2026-09-02 from the public Atlassian docs and
the source of shipping first-party clients; re-verify field-length numbers against
the live docs before relying on an exact cutoff.

| File | What it is |
| --- | --- |
| [`devinfo-api.md`](devinfo-api.md) | The current devinfo API: every operation, the full request/response schema, `updateSequenceId` semantics, `operationType`, `properties`, field caps, rate limiting, OAuth 2LO, and the adjacent Builds/Deployments/Feature-Flags/Security APIs. |
| [`devinfo-reference-implementations.md`](devinfo-reference-implementations.md) | How `atlassian/github-for-jira`, `gitlab-org/gitlab`, the Atlassian Jenkins plugin and the CircleCI Jira orb each build the payload, sequence `updateSequenceId`, chunk, and delete entities — with a consensus matrix. |
| [`devinfo-rewrite-notes.md`](devinfo-rewrite-notes.md) | Why the bridge's `transform.py` / `jira.py` / `sync.py` are shaped the way they are: per-entity `updateSequenceId` (shared-SHA collision fix), `associations` alongside `issueKeys`, `operationType: BACKFILL` on first sight, per-entity branch delete, chunk size. |

The operational side (recovery with `reprocess`, `inspect`, `delete-repo`, the
"commits stored but missing from panels" failure mode) is in the top-level
[`README.md`](../README.md#debugging--recovery).
