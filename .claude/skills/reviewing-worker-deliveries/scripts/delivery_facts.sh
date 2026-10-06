#!/usr/bin/env bash
# Mechanical facts about a claimed delivery — what git actually contains, before any judgement.
# Usage: delivery_facts.sh <branch|sha|PR#> [base=origin/main]
# Read-only (fetches). Prints RED FLAGS for the failure modes this repo has actually hit.
set -u
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO" || exit 1
TARGET="${1:?usage: delivery_facts.sh <branch|sha|PR#> [base]}"
BASE="${2:-origin/main}"
FLAGS=()
git fetch -q origin 2>/dev/null

if [[ "$TARGET" =~ ^#?[0-9]+$ && ${#TARGET} -lt 6 ]]; then        # PR number (SHAs are longer)
  PR="${TARGET#\#}"
  read -r STATE HEAD_OID MERGE_OID < <(gh pr view "$PR" --json state,headRefOid,mergeCommit \
    -q '"\(.state) \(.headRefOid) \(.mergeCommit.oid // "-")"' 2>/dev/null)
  [ -n "${HEAD_OID:-}" ] || { echo "PR #$PR not found"; exit 1; }
  gh pr view "$PR" --json url,mergeable -q '"PR: \(.url) mergeable=\(.mergeable)"'
  echo "state=$STATE head=${HEAD_OID:0:7}"
  git cat-file -e "$HEAD_OID^{commit}" 2>/dev/null || git fetch -q origin "pull/$PR/head" 2>/dev/null
  TARGET="$HEAD_OID"
  # A merged PR is reviewed against main as it was just before the merge.
  [ "$STATE" = "MERGED" ] && [ "$MERGE_OID" != "-" ] && BASE="$MERGE_OID^1"
fi
if ! git rev-parse --verify -q "$TARGET^{commit}" >/dev/null; then
  if git rev-parse --verify -q "origin/$TARGET^{commit}" >/dev/null; then TARGET="origin/$TARGET"
  else echo "RED FLAG: '$TARGET' does not exist locally or on origin — the claimed commit/branch is not in git"; exit 2; fi
fi

MB="$(git merge-base "$BASE" "$TARGET")"
echo "== commits $BASE..$TARGET"
git log --format='%h %an %ad %s' --date=short "$MB..$TARGET"
N="$(git rev-list --count "$MB..$TARGET")"
[ "$N" -gt 0 ] || FLAGS+=("no commits ahead of $BASE — nothing was delivered (or it is already merged: check git branch -r --contains)")

echo "== diffstat"
git diff --stat "$MB" "$TARGET" | tail -25
FILES="$(git diff --name-only "$MB" "$TARGET")"
[ -n "$FILES" ] || [ "$N" -eq 0 ] || FLAGS+=("commits exist but the net diff is EMPTY")

SRC="$(grep -E '^(src|scripts|web/src|config)/' <<<"$FILES" || true)"
TESTS="$(grep -E '^(tests/|web/src/.*\.test\.)' <<<"$FILES" || true)"
[ -z "$SRC" ] || [ -n "$TESTS" ] || FLAGS+=("code changed with no test file changed")
grep -qE '^(\.ai/|docs/)' <<<"$FILES" && [ -z "$SRC" ] && echo "(docs-only delivery)"
if grep -q '^src/control/db.py$' <<<"$FILES"; then
  OLD="$(git show "$MB:src/control/db.py" | grep -oE '^\s+\(([0-9]+), """' | grep -oE '[0-9]+' | sort -n | tail -1)"
  NEW="$(git show "$TARGET:src/control/db.py" | grep -oE '^\s+\(([0-9]+), """' | grep -oE '[0-9]+' | sort -n | tail -1)"
  [ "$OLD" = "$NEW" ] || FLAGS+=("adds DB migration(s) $OLD -> $NEW: Level 3, deploy needs a DB backup")
fi
UNRELATED="$(git log --format='%an' "$MB..$TARGET" | sort -u | wc -l)"
[ "$UNRELATED" -le 1 ] || FLAGS+=("$UNRELATED distinct authors in range — another loop's commits may be riding along")
git log --merges --format='%h %s' "$MB..$TARGET" | grep -v "origin/main\|from .*/main" | sed 's/^/merge in range: /'

echo "== merge check vs $BASE"
if git merge-tree --write-tree "$BASE" "$TARGET" >/dev/null 2>&1; then echo "merges cleanly"
else FLAGS+=("conflicts with $BASE"); fi

echo
if [ "${#FLAGS[@]}" -eq 0 ]; then echo "No mechanical red flags. Now review the diff itself."
else printf 'RED FLAG: %s\n' "${FLAGS[@]}"; fi
