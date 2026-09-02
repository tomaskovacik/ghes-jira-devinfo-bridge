#!/usr/bin/env bash
#
# devinfo-admin.sh — talk to the Jira Cloud Development Information API directly,
# using the same OAuth 2LO credentials the bridge uses.
#
# Reads JIRA_* / GHES_* from an env file (default: ./.env next to this script,
# override with  ENV_FILE=/path/to/.env  ).
#
# Commands:
#   token                              print an access token and exit
#   inspect        [owner/name]        counts + lastUpdated for the repo doc
#   commits        [owner/name]        list every stored commit  (id / ts / keys / subject)
#   commit-seq     [owner/name]        stored commits sorted by updateSequenceId,
#                                     a seq-value histogram, and a HEAD marker for
#                                     commits that are a branch's lastCommit
#   branches       [owner/name]        list every stored branch  (id / keys / lastCommit / seq)
#   has  <sha>     [owner/name]        show one stored commit (id, keys, updateSequenceId)
#   delete-commit  <sha>     [owner/name]
#   delete-branch  <branchId> [owner/name]     branchId as shown by `branches` / inspect --full
#   delete-pr      <number>  [owner/name]
#   delete-repo    [owner/name]        purge ALL devinfo for the repo
#   push-commit    <sha> [owner/name] [k1,k2]  re-submit ONE commit via the bulk
#                                     endpoint and print the raw response
#                                     (acceptedDevinfoEntities / unknownIssueKeys
#                                     / failedDevinfoEntities / unknownAssociations).
#                                     Keys default to those matched in the commit
#                                     subject; override with a 3rd arg or KEYS=.
#   repush-all     [owner/name]       read back every stored commit/branch/PR and
#                                     re-submit them all with a fresh
#                                     updateSequenceId. Non-destructive recovery:
#                                     forces Jira to rebuild issue associations /
#                                     per-issue Development panels. No GHES walk,
#                                     no lookback limit, keeps deleted-branch
#                                     commits.
#
# owner/name defaults to $REPO (env). Set $REPO_ID to skip the GHES lookup that
# maps owner/name -> numeric repo id. Set $USID to make a delete conditional
# (only applied when _updateSequenceId is greater than the stored value).
#
# Examples:
#   ./devinfo-admin.sh commits StateTreasurySK/ManEx
#   REPO=StateTreasurySK/ManEx ./devinfo-admin.sh delete-commit 3cc9085beb89d5b81f413ee1b7c6efd69d7203d1

set -euo pipefail

# --------------------------------------------------------------------------
# env
# --------------------------------------------------------------------------
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$here/.env}"
if [ -f "$ENV_FILE" ]; then
	set -a
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
else
	echo "env file not found: $ENV_FILE (set ENV_FILE=)" >&2
	exit 2
fi

JIRA_API_BASE="${JIRA_API_BASE:-https://api.atlassian.com}"
JIRA_API_BASE="${JIRA_API_BASE%/}"
JIRA_TOKEN_URL="${JIRA_TOKEN_URL:-https://api.atlassian.com/oauth/token}"
GHES_API_URL="${GHES_API_URL:-${GHES_BASE_URL:-}/api/v3}"
GHES_API_URL="${GHES_API_URL%/}"

: "${JIRA_OAUTH_CLIENT_ID:?set JIRA_OAUTH_CLIENT_ID in $ENV_FILE}"
: "${JIRA_OAUTH_CLIENT_SECRET:?set JIRA_OAUTH_CLIENT_SECRET in $ENV_FILE}"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

# --------------------------------------------------------------------------
# lazily-resolved token / cloud id / repo id
# --------------------------------------------------------------------------
_TOKEN=""
get_token() {
	if [ -z "$_TOKEN" ]; then
		_TOKEN="$(
			curl -fsS -X POST "$JIRA_TOKEN_URL" \
				-H 'Content-Type: application/json' \
				-d "$(jq -n \
					--arg id "$JIRA_OAUTH_CLIENT_ID" \
					--arg secret "$JIRA_OAUTH_CLIENT_SECRET" \
					'{audience:"api.atlassian.com",grant_type:"client_credentials",client_id:$id,client_secret:$secret}')" \
				| jq -r '.access_token // empty'
		)"
		[ -n "$_TOKEN" ] || { echo "could not obtain access token (check client id/secret)" >&2; exit 1; }
	fi
	printf '%s' "$_TOKEN"
}

_CLOUD=""
get_cloud_id() {
	if [ -z "$_CLOUD" ]; then
		if [ -n "${JIRA_CLOUD_ID:-}" ]; then
			_CLOUD="$JIRA_CLOUD_ID"
		elif [ -n "${JIRA_SITE_URL:-}" ]; then
			_CLOUD="$(curl -fsS "${JIRA_SITE_URL%/}/_edge/tenant_info" | jq -r '.cloudId // empty')"
			[ -n "$_CLOUD" ] || { echo "tenant_info returned no cloudId" >&2; exit 1; }
		else
			echo "set JIRA_CLOUD_ID or JIRA_SITE_URL in $ENV_FILE" >&2
			exit 2
		fi
	fi
	printf '%s' "$_CLOUD"
}

# get_repo_id <owner/name>
get_repo_id() {
	local full="$1"
	if [ -n "${REPO_ID:-}" ]; then
		printf '%s' "$REPO_ID"
		return
	fi
	[ -n "${GHES_TOKEN:-}" ] || { echo "need GHES_TOKEN (or set \$REPO_ID) to resolve '$full'" >&2; exit 2; }
	local id
	id="$(
		curl -fsS \
			-H "Authorization: Bearer $GHES_TOKEN" \
			-H "Accept: application/vnd.github+json" \
			"$GHES_API_URL/repos/$full" | jq -r '.id // empty'
	)"
	[ -n "$id" ] || { echo "GHES did not return an id for '$full'" >&2; exit 1; }
	printf '%s' "$id"
}

# repo full name from $1 or $REPO
repo_arg() {
	local full="${1:-${REPO:-}}"
	[ -n "$full" ] || { echo "repo required: pass owner/name or set \$REPO" >&2; exit 2; }
	printf '%s' "$full"
}

devinfo_base() {
	printf '%s/jira/devinfo/0.1/cloud/%s' "$JIRA_API_BASE" "$(get_cloud_id)"
}

# usid_qs — "?_updateSequenceId=N" when $USID is set, else empty
usid_qs() {
	[ -n "${USID:-}" ] && printf '?_updateSequenceId=%s' "$USID" || true
}

# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
cmd="${1:-}"
[ -n "$cmd" ] || { sed -n '2,40p' "$0"; exit 2; }
shift

case "$cmd" in
token)
	get_token
	echo
	;;

inspect)
	full="$(repo_arg "${1:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	curl -fsS -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid" |
		jq '{topLevelKeys: (keys),
		     lastUpdated,
		     updateSequenceId,
		     commits: ((.commits // []) | length),
		     branches: ((.branches // []) | length),
		     pullRequests: ((.pullRequests // []) | length),
		     builds: ((.builds // []) | length),
		     deployments: ((.deployments // []) | length),
		     commitSeqRange: [((.commits // []) | map(.updateSequenceId) | min), ((.commits // []) | map(.updateSequenceId) | max)]}'
	;;

commits)
	full="$(repo_arg "${1:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	curl -fsS -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid" |
		jq -r '.commits[]? |
			"\(.id)  \(.authorTimestamp)  \(((.issueKeys // []) | join(",")) as $k | if $k == "" then "-" else $k end)  \(.message | split("\n")[0])"'
	;;

branches)
	full="$(repo_arg "${1:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	curl -fsS -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid" |
		jq -r '.branches[]? |
			"\(.id // .name)  \(((.issueKeys // []) | join(",")) as $k | if $k == "" then "-" else $k end)  lastCommit=\(.lastCommit.id // "-")  seq=\(.updateSequenceId)"'
	;;

commit-seq)
	# every stored commit sorted by updateSequenceId, + a histogram of seq values
	# and whether each commit is some branch's lastCommit. A big cluster sharing
	# one seq == a mass re-push. Divergent seq for branch-head commits == the
	# commit-vs-branch.lastCommit collision.
	full="$(repo_arg "${1:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	curl -fsS -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid" |
		jq -r '
			([ .branches[]?.lastCommit.id ] | map(select(. != null))) as $heads
			| (.commits // []) as $c
			| "commits: \($c | length)",
			  "seq histogram (updateSequenceId -> #commits):",
			  ( $c | group_by(.updateSequenceId)[] | "  \(.[0].updateSequenceId)  x\(length)" ),
			  "",
			  "seq            authorTimestamp       head  keys                  subject",
			  ( $c | sort_by(.updateSequenceId)[]
			    | (.id) as $cid
			    | "\(.updateSequenceId)  \(.authorTimestamp)  " +
			      "\(if ($heads | index($cid)) != null then "HEAD" else "    " end)  " +
			      "\((((.issueKeys // []) | join(",")) as $k | if $k == "" then "-" else $k end) + "                    " | .[:20])  " +
			      "\(.message | split("\n")[0][:50])"
			  )
		'
	;;

has)
	sha="${1:?sha required}"
	full="$(repo_arg "${2:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	curl -fsS -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid" |
		jq --arg s "$sha" '.commits[]? | select(.id == $s or .hash == $s) |
			{id, issueKeys, updateSequenceId, message: (.message | split("\n")[0])}'
	;;

delete-commit)
	sha="${1:?sha required}"
	full="$(repo_arg "${2:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	echo "DELETE commit $sha${USID:+ (_updateSequenceId=$USID)}" >&2
	curl -i -sS -X DELETE -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid/commit/$sha$(usid_qs)"
	;;

delete-branch)
	br="${1:?branch id required}"
	full="$(repo_arg "${2:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	echo "DELETE branch $br${USID:+ (_updateSequenceId=$USID)}" >&2
	curl -i -sS -X DELETE -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid/branch/$br$(usid_qs)"
	;;

delete-pr)
	pr="${1:?pr number required}"
	full="$(repo_arg "${2:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	echo "DELETE pull_request $pr${USID:+ (_updateSequenceId=$USID)}" >&2
	curl -i -sS -X DELETE -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid/pull_request/$pr$(usid_qs)"
	;;

delete-repo)
	full="$(repo_arg "${1:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2
	read -r -p "purge ALL devinfo for $full (id $rid)? [y/N] " ans
	[ "$ans" = y ] || [ "$ans" = Y ] || { echo "aborted" >&2; exit 1; }
	curl -i -sS -X DELETE -H "Authorization: Bearer $(get_token)" \
		"$(devinfo_base)/repository/$rid"
	;;

push-commit)
	sha="${1:?sha required}"
	full="$(repo_arg "${2:-}")"
	keys_csv="${3:-${KEYS:-}}"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2

	[ -n "${GHES_TOKEN:-}" ] || { echo "need GHES_TOKEN to fetch commit $sha" >&2; exit 2; }
	cjson="$(
		curl -fsS \
			-H "Authorization: Bearer $GHES_TOKEN" \
			-H "Accept: application/vnd.github+json" \
			"$GHES_API_URL/repos/$full/commits/$sha"
	)"

	msg="$(printf '%s' "$cjson" | jq -r '.commit.message')"
	if [ -z "$keys_csv" ]; then
		keys_csv="$(printf '%s\n' "$msg" | grep -oE '[A-Z][A-Z0-9]+-[0-9]+' | sort -u | paste -sd, -)"
	fi
	[ -n "$keys_csv" ] || { echo "no issue keys found; pass them as 3rd arg or KEYS=" >&2; exit 2; }
	echo "issueKeys: $keys_csv" >&2

	usid="$(( $(date +%s) * 1000 ))"

	payload="$(
		printf '%s' "$cjson" | jq \
			--arg rid "$rid" \
			--arg name "$full" \
			--arg sha "$sha" \
			--argjson usid "$usid" \
			--argjson keys "$(printf '%s' "$keys_csv" | jq -R 'split(",")')" \
			'{
				repositories: [{
					id: $rid,
					name: $name,
					url: (.html_url | sub("/commit/.*$"; "")),
					updateSequenceId: $usid,
					commits: [{
						id: $sha,
						hash: $sha,
						displayId: ($sha[0:7]),
						message: .commit.message,
						issueKeys: $keys,
						author: { name: .commit.author.name, email: .commit.author.email },
						authorTimestamp: .commit.author.date,
						url: .html_url,
						fileCount: ((.files // []) | length),
						updateSequenceId: $usid
					}]
				}],
				preventTransitions: true,
				providerMetadata: { product: "devinfo-admin.sh" }
			}'
	)"

	echo "--- payload ---" >&2
	printf '%s\n' "$payload" | jq . >&2
	echo "--- response ---" >&2
	curl -sS -X POST \
		-H "Authorization: Bearer $(get_token)" \
		-H 'Content-Type: application/json' \
		"$(devinfo_base)/bulk" \
		--data-binary "$payload" | jq .
	;;

repush-all)
	full="$(repo_arg "${1:-}")"
	rid="$(get_repo_id "$full")"
	echo "repo $full -> id $rid" >&2

	doc="$(curl -fsS -H "Authorization: Bearer $(get_token)" "$(devinfo_base)/repository/$rid")"
	nc="$(printf '%s' "$doc" | jq '(.commits // []) | length')"
	nb="$(printf '%s' "$doc" | jq '(.branches // []) | length')"
	np="$(printf '%s' "$doc" | jq '(.pullRequests // []) | length')"
	echo "stored: commits=$nc branches=$nb pullRequests=$np" >&2
	[ "$nc" != 0 ] || [ "$nb" != 0 ] || [ "$np" != 0 ] || { echo "nothing stored to re-push" >&2; exit 1; }

	# A plain re-push (update) does NOT make Jira rebuild a stale commit->issue
	# association; delete-then-recreate does. So: delete every stored commit
	# entity first, then re-POST them all (from the stored objects) as fresh
	# entities. Branches are left to the update path (they surface fine).
	tok="$(get_token)"
	printf '%s' "$doc" | jq -r '.commits[]?.id' | while read -r sha; do
		[ -n "$sha" ] || continue
		code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
			-H "Authorization: Bearer $tok" \
			"$(devinfo_base)/repository/$rid/commit/$sha")"
		echo "  DELETE $sha -> $code" >&2
	done
	echo "waiting 10s for deletes to settle..." >&2
	sleep 10

	# Recreate the commits in SMALL CHUNKS. A single large bulk payload is what
	# lost the associations in the first place (Jira's async association pass
	# drops large-ish batches); 1-commit recreates provably work. CHUNK tunes it.
	base="$(( $(date +%s) * 1000 ))"
	rurl="$(printf '%s' "$doc" | jq -r --arg d "${GHES_BASE_URL%/}/$full" '.url // $d')"
	CHUNK="${CHUNK:-4}"
	mapfile -t COMMITS < <(printf '%s' "$doc" | jq -c '.commits[]?')
	total="${#COMMITS[@]}"
	echo "recreating $total commits in chunks of $CHUNK" >&2

	i=0
	cn=0
	while [ "$i" -lt "$total" ]; do
		slice=("${COMMITS[@]:i:CHUNK}")
		cn=$((cn + 1))
		commits_json="$(
			printf '%s\n' "${slice[@]}" |
				jq -s --argjson base "$base" --argjson off "$i" \
					'[ to_entries[] | .value + { updateSequenceId: ($base + $off + .key) } ]'
		)"
		payload="$(
			jq -n \
				--arg rid "$rid" --arg name "$full" --arg rurl "$rurl" \
				--argjson base "$base" --argjson commits "$commits_json" \
				'{
					repositories: [{
						id: $rid, name: $name, url: $rurl,
						updateSequenceId: ($base + 1000000),
						commits: $commits
					}],
					preventTransitions: true,
					providerMetadata: { product: "devinfo-admin.sh repush-all" }
				}'
		)"
		resp="$(
			curl -sS -X POST \
				-H "Authorization: Bearer $tok" \
				-H 'Content-Type: application/json' \
				"$(devinfo_base)/bulk" \
				--data-binary "$payload"
		)"
		echo "  chunk $cn (${#slice[@]} commits): $(
			printf '%s' "$resp" | jq -c '{
				accepted: ([.acceptedDevinfoEntities[]?.commits[]?] | length),
				unknownIssueKeys, failed: (.failedDevinfoEntities | length)
			}' 2>/dev/null || printf '%s' "$resp"
		)" >&2
		i=$((i + CHUNK))
		[ "$i" -lt "$total" ] && sleep 3
	done
	echo "done. give Jira ~15 min, then spot-check a few issue panels." >&2
	;;

*)
	echo "unknown command: $cmd" >&2
	sed -n '2,54p' "$0"
	exit 2
	;;
esac
