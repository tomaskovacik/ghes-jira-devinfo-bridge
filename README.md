# ghes-jira-devinfo-bridge

One-way sync of **GitHub Enterprise Server** commits, branches and pull requests
into the **Jira Cloud** development panel.

It polls the GHES REST API, extracts Jira issue keys from commit messages, branch
names and PR titles/bodies, and pushes the result to the Jira Cloud
[Development Information API](https://developer.atlassian.com/cloud/jira/software/rest/api-group-development-information/)
using OAuth 2.0 client credentials.

## Why this exists

The official *GitHub for Jira* app and the built-in DVCS connector both require
Jira Cloud to make **inbound** calls to your GitHub instance. That is a
non-starter for a GHES box with no public ingress. This bridge runs **inside**
your network and only makes **outbound** HTTPS calls — to your GHES API and to
`api.atlassian.com`. No inbound, no webhooks, no GitHub admin.

## What it does / does not do

| Does | Does not |
| --- | --- |
| Commits, branches, pull requests into the Jira dev panel | Jira → GitHub (create branch, smart-commit transitions) |
| Issue-key detection in message / branch / PR title + body | GitHub Issues sync, field mapping |
| Poll on an interval, or one-shot for cron | Real-time (webhook) delivery |
| Runs fully outbound-only, read-only on GHES | Builds / deployments (see the devinfo build/deploy APIs) |

## Prerequisites

**GHES** — a fine-grained personal access token, read-only:
`Contents: Read`, `Metadata: Read`, `Pull requests: Read` for the target repos.

**Jira Cloud** — a site admin creates OAuth credentials for a self-hosted tool:
*Settings → Apps → OAuth credentials → Create credentials*, grant the
**Development information** permission. This yields a client id + secret and is
write-only (the tool cannot read Jira). See
[Integrate with self-hosted tools using OAuth](https://support.atlassian.com/jira-cloud-administration/docs/integrate-with-self-hosted-tools-using-oauth/).

Get your cloud id from `https://<site>.atlassian.net/_edge/tenant_info`, or set
`JIRA_SITE_URL` and let the bridge resolve it.

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GHES_BASE_URL` | yes | — | e.g. `https://ghe.example.com` |
| `GHES_API_URL` | no | `${GHES_BASE_URL}/api/v3` | REST API root |
| `GHES_TOKEN` | yes | — | fine-grained PAT, read-only |
| `GHES_ORG` | one of | — | default owner; prefixes bare `GHES_REPOS` names; alone = discover the whole org |
| `GHES_REPOS` | one of | — | comma list; bare names use `GHES_ORG`, `owner/name` used as-is |
| `GHES_ORGS` | one of | — | comma list of additional org logins to fully discover |
| `GHES_BRANCH_EXCLUDE` | no | — | comma list of fnmatch globs to skip, e.g. `renovate/*,dependabot/*` |
| `SYNC_KEYED_BRANCHES_ONLY` | no | `false` | only sync branches whose name contains an issue key (plus the default branch) |
| `SYNC_DEFAULT_BRANCH_ONLY` | no | `false` | only sync the default branch |
| `SYNC_CONCURRENCY` | no | `8` | parallel GHES requests per repo (`1` = serial) |
| `SYNC_LOG_ENTITIES` | no | `false` | log every commit/branch/PR sent to Jira (dry or wet), like `inspect --full` |
| `GHES_USE_GRAPHQL` | no | `false` | scan branches via GraphQL (~3 calls/repo, active branches only, trunk-independent); needs GraphQL enabled |
| `JIRA_OAUTH_CLIENT_ID` | unless `DRY_RUN` | — | |
| `JIRA_OAUTH_CLIENT_SECRET` | unless `DRY_RUN` | — | |
| `JIRA_CLOUD_ID` | one of | — | Jira Cloud tenant id |
| `JIRA_SITE_URL` | one of | — | e.g. `https://site.atlassian.net` |
| `JIRA_PROJECT_KEYS` | no | — | comma list, e.g. `PROJ,TEAM`; restricts key detection to these projects — avoids matches like `UTF-8`, `SHA-1` |
| `JIRA_ISSUE_KEY_REGEX` | no | `\b[A-Z][A-Z0-9]{1,9}-\d+\b` | used only when `JIRA_PROJECT_KEYS` is unset |
| `SYNC_INTERVAL_SECONDS` | no | `0` | `0` = run once and exit; `>0` = loop |
| `SYNC_LOOKBACK_DAYS` | no | `14` | first-run history cutoff per branch |
| `SYNC_INCLUDE_PRS` | no | `true` | |
| `SYNC_PREVENT_TRANSITIONS` | no | `true` | never let a synced commit transition an issue |
| `STATE_PATH` | no | `/data/state.json` | |
| `DRY_RUN` | no | `false` | log payloads, do not POST to Jira |
| `LOG_LEVEL` / `LOG_FORMAT` | no | `INFO` / `text` | `text` or `json` |
| `HTTP_TIMEOUT` / `MAX_RETRIES` | no | `30` / `4` | |

## Branch scanning: REST vs GraphQL

Default (**REST**): list branches, then per branch either walk its history since
the lookback (first sight) or diff `previous_head...head` (thereafter). One
request per branch. Fine for small repos; on a repo with hundreds of branches
the first run is hundreds of serial requests, and if the default branch isn't a
shared trunk the compare-vs-default degrades badly.

**GraphQL** (`GHES_USE_GRAPHQL=true`): one paginated query (~1 call per 100
branches) returns every branch's head date plus its commits since the lookback,
inline. Only branches with activity in the window are processed; the default
branch is irrelevant. A 282-branch repo goes from ~550 requests to ~3. Requires
GraphQL enabled on the instance (`POST {base}/api/graphql`).

Both honour `GHES_BRANCH_EXCLUDE`, `SYNC_KEYED_BRANCHES_ONLY`,
`SYNC_DEFAULT_BRANCH_ONLY`, and skip branches whose head is unchanged since the
last run.

## Run

The container runs `python -m bridge` and nothing else — there is **no cron
daemon in the image**. Choose one of two scheduling models.

### Daemon (internal loop)

Set `SYNC_INTERVAL_SECONDS` > 0. The process runs a pass, sleeps, repeats, and
exits cleanly on SIGINT/SIGTERM. `docker-compose.yml` is set up for this:

```
docker compose up -d
```

One long-lived container, logs on stdout (`docker logs`), `restart: unless-stopped`
covers crashes.

### Scheduled (one-shot + host scheduler)

Set `SYNC_INTERVAL_SECONDS=0`. The container runs a single pass and exits — `0`
on success, `1` if any repo failed, `2` on bad configuration. Schedule it from
the **host**, e.g. cron:

```cron
*/30 * * * * docker run --rm --env-file /opt/bridge/.env -v /opt/bridge/data:/data ghes-jira-devinfo-bridge:local
```

or a systemd timer, or (on Fargate) EventBridge Scheduler → `RunTask`.

### Why no cron inside the image

A cron daemon as PID 1 does not forward SIGTERM to the job (so `docker stop`
kills it mid-run), writes to syslog rather than stdout, and runs with a scrubbed
environment so `--env-file` values are invisible to the job unless dumped to a
file. It also hides failures: the job can crash while the container stays "up".
Running the job as PID 1 — looping itself, or one-shot under the host scheduler —
avoids all of that. Use the host's cron/systemd to call `docker run`; do not add
cron to the image.

## State

`STATE_PATH` holds per-repo branch heads and a PR high-water mark. It is written
atomically and only after a successful push, so an interrupted run replays
instead of losing data. Deleting it triggers a lookback-bounded re-sync.
Pushes are idempotent (`updateSequenceId` is monotonic); Jira de-duplicates.
A branch head is recorded only once the bridge has processed it, so a branch
that was inactive or filtered out stays "new" and gets a full backfill when it
later becomes relevant.

## Debugging & recovery

Run subcommands by appending them to the image (entrypoint is `python -m bridge`):

```
docker run --rm --env-file .env IMAGE inspect --repo OWNER/NAME
docker run --rm --env-file .env IMAGE inspect --all
docker run --rm --env-file .env IMAGE delete-repo --repo OWNER/NAME --yes
```

- **`inspect`** — what Jira has stored for a repo's development information:
  counts by default, every stored commit hash / branch / PR with `--full`, the
  raw document with `--json`. `--all` walks every repo in `state.json`.
- **`delete-repo`** — purge all devinfo for a repo in Jira. `--reset-state`
  also drops it from `state.json` so the next sync rebuilds it. Confirms unless
  `--yes`.

**Wedged repo.** If a repo's Development panel is empty or stale even though the
sync logs `202` and `inspect` shows commits (`branches: 0` is the tell), Jira's
async processing for that repo has jammed — usually from many rapid re-pushes.
Fix: `delete-repo --repo OWNER/NAME --reset-state --yes`, then one normal sync.

**Missing info on an issue** — check, in order: the commit/branch is within
`SYNC_LOOKBACK_DAYS`; with `GHES_USE_GRAPHQL` the *branch head* (not just the
commit) is inside the window; the key matches `JIRA_PROJECT_KEYS`; the sync log
has no `did not recognise issue keys` line. Devinfo also takes minutes to
surface and the per-issue panel is built lazily on view.

## Development

```
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

## License

MIT — see [LICENSE](LICENSE).
