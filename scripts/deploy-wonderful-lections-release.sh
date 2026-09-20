#!/usr/bin/env bash
set -Eeuo pipefail

TARGET=/home/dev/.local/opt/wonderful-lections-runtime-source
EXPECTED_OLD="${WONDERFUL_EXPECTED_OLD_SHA:?missing WONDERFUL_EXPECTED_OLD_SHA}"
RELEASE_SHA="${WONDERFUL_RELEASE_SHA:?missing WONDERFUL_RELEASE_SHA}"
RELEASE_BRANCH="${WONDERFUL_RELEASE_BRANCH:?missing WONDERFUL_RELEASE_BRANCH}"

die(){ echo "ERROR: $*" >&2; exit 1; }
is_sha(){ [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }
is_sha "$EXPECTED_OLD" || die "invalid expected old SHA"
is_sha "$RELEASE_SHA" || die "invalid release SHA"
[[ "$RELEASE_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid release branch"
test -d "$TARGET" || die "runtime source worktree missing"

git_cmd(){
  timeout 120 /usr/bin/git --no-optional-locks     -c core.fsmonitor=false -c core.hooksPath=/dev/null -c diff.external=     -C "$TARGET" "$@"
}

git_cmd rev-parse --is-inside-work-tree | grep -qx true || die "target is not a Git worktree"
git_cmd diff --quiet || die "runtime source has unstaged tracked changes"
git_cmd diff --cached --quiet || die "runtime source has staged changes"
if [ -n "$(git_cmd ls-files --others --exclude-standard | head -n 1)" ]; then
  die "runtime source has untracked files"
fi

OLD_SHA="$(git_cmd rev-parse HEAD)"
case "$OLD_SHA" in
  "$EXPECTED_OLD"|"$RELEASE_SHA") ;;
  *) die "runtime HEAD $OLD_SHA is neither expected old nor release SHA" ;;
esac

git_cmd fetch --no-tags origin "refs/heads/$RELEASE_BRANCH"
REMOTE_SHA="$(git_cmd rev-parse FETCH_HEAD)"
[ "$REMOTE_SHA" = "$RELEASE_SHA" ] || die "remote branch moved: $REMOTE_SHA"

if [ "$OLD_SHA" = "$EXPECTED_OLD" ]; then
  git_cmd merge-base --is-ancestor "$EXPECTED_OLD" "$RELEASE_SHA"     || die "release is not a descendant of expected production SHA"
  git_cmd switch --detach "$RELEASE_SHA"
fi

[ "$(git_cmd rev-parse HEAD)" = "$RELEASE_SHA" ] || die "failed to materialize exact release SHA"

/usr/bin/env npm --prefix "$TARGET" ci
/usr/bin/env npm --prefix "$TARGET" run build

git_cmd diff --quiet || die "build left unstaged tracked changes"
git_cmd diff --cached --quiet || die "build left staged changes"
if [ -n "$(git_cmd ls-files --others --exclude-standard | head -n 1)" ]; then
  die "build left untracked files"
fi

for file in review.css review.js speaker-notes.js; do
  test -f "$TARGET/site/dist/review/$file" || die "missing built review asset: $file"
done

COMPACT_CSS="$(tr -d '[:space:]' < "$TARGET/site/dist/review/review.css")"
grep -Fq 'width:fit-content' <<<"$COMPACT_CSS" || die "built CSS lacks compact width rule"
grep -Fq 'max-width:min(520px,42%)' <<<"$COMPACT_CSS" || die "built CSS lacks 42 percent max-width rule"

echo "runtime_old_sha=$OLD_SHA"
echo "runtime_release_sha=$RELEASE_SHA"
echo "runtime_branch=$RELEASE_BRANCH"
echo "runtime_clean=PASS"
echo "static_build=PASS"
echo "compact_overlay_css=PASS"
