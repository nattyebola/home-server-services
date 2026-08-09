#!/bin/sh
# Install this repo's cron jobs into the host crontab WITHOUT dropping any line
# the user added themselves. Reads the already-substituted content of
# scripts/crontab on stdin (make cron-install does the placeholder expansion,
# since only make reads .env.shared) and takes the checkout's absolute path as
# its single argument.
#
# Why this exists: `make cron-install` used to pipe scripts/crontab straight
# into `crontab -`, which replaces the *whole* crontab — every job the user had
# added by hand disappeared silently at the next install, with no diff and no
# warning (hit on 2026-08-03 with an unrelated personal job).
#
# The repo's jobs are wrapped in two marker comments and re-emitted between them
# on every install; anything outside the markers is carried over verbatim. The
# managed block is appended LAST on purpose: a `MAILTO=""` in a crontab only
# applies to the lines that follow it, so keeping the block at the end scopes
# that silencing to the repo's own jobs instead of swallowing the mail the
# user's jobs would otherwise produce.
set -eu

BEGIN='# >>> managed by `make cron-install` — edit server/scripts/crontab, not here >>>'
END='# <<< managed by `make cron-install` — end of managed block <<<'

REPO_ROOT="${1:?usage: install-crontab.sh <repo-root> < substituted-crontab}"

managed=$(cat)
test -n "$managed" || { echo "install-crontab.sh: empty input on stdin" >&2; exit 1; }

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT HUP INT TERM

# `crontab -l` exits non-zero when the user has no crontab yet (first install)
# — but ALSO on any other failure (permissions, cron daemon missing, SELinux).
# The old `2>/dev/null || true` conflated the two: on a transient error $current
# came back empty, the script took the migration branch with nothing to keep,
# and wrote out the managed block alone. Every personal job vanished, with no
# message — and MAILTO="" even suppresses the mail cron would have sent. That is
# precisely the accident this script exists to prevent, so an empty crontab is
# now only accepted when cron actually says there is none.
set +e
crontab -l >"$workdir/current" 2>"$workdir/err"
crontab_rc=$?
set -e
if [ "$crontab_rc" -ne 0 ]; then
	if grep -qi 'no crontab for' "$workdir/err"; then
		: >"$workdir/current"
	else
		echo "install-crontab.sh: 'crontab -l' failed (exit $crontab_rc) — nothing was installed:" >&2
		sed 's/^/  /' "$workdir/err" >&2
		exit 1
	fi
fi
current=$(cat "$workdir/current")

# A BEGIN without its END means the block's closing marker was lost (a stray
# `crontab -e`). The awk below never clears `skip` in that case, so it would
# swallow every line after BEGIN — silently dropping any personal job placed
# after the block, and without even triggering the "unmarked lines" report.
# Refuse rather than guess: the user's own jobs are at stake.
begin_count=$(grep -cxF "$BEGIN" "$workdir/current" || true)
end_count=$(grep -cxF "$END" "$workdir/current" || true)
if [ "$begin_count" -ne "$end_count" ]; then
	echo "install-crontab.sh: unbalanced markers in the current crontab" >&2
	echo "  $begin_count begin marker(s), $end_count end marker(s)" >&2
	echo "  Refusing to install: fix the markers with 'crontab -e' (or delete the" >&2
	echo "  managed block entirely), then re-run. Nothing was changed." >&2
	exit 1
fi

if printf '%s\n' "$current" | grep -qxF "$BEGIN"; then
	# Normal case: drop the previous managed block, keep everything else.
	kept=$(printf '%s\n' "$current" | awk -v b="$BEGIN" -v e="$END" '
		$0 == b { skip = 1; next }
		$0 == e { skip = 0; next }
		!skip   { print }
	')
else
	# Migration from the pre-marker versions of this target, where the repo's
	# jobs sat unmarked in the crontab. Two rules, in order of precision:
	# drop any line that appears verbatim in the block about to be installed
	# (catches the job lines, MAILTO and the whole comment header, which would
	# otherwise be left behind as orphan clutter in the user section), then
	# drop any remaining line mentioning the checkout path (a job whose text
	# changed between two versions of scripts/crontab, so no longer verbatim).
	# Only reached once per host. A user line that happens to mention the
	# checkout path is dropped here too, hence the report below.
	# The block is handed to awk as a file, not via -v: an assignment goes
	# through awk's escape processing, which turns the `date +\%s` of the cron
	# lines into `date +%s` and makes them stop matching (same `%` trap as in
	# scripts/crontab, one layer up).
	# Same $workdir as above, cleaned by the single trap set at the top: a
	# second `trap ... EXIT` here would REPLACE that one and leak the first
	# temp dir.
	tmp="$workdir/managed"
	printf '%s\n' "$managed" > "$tmp"
	kept=$(printf '%s\n' "$current" | awk '
		NR == FNR { seen[$0] = 1; next }
		!($0 in seen)
	' "$tmp" - | grep -vF "$REPO_ROOT" || true)
	dropped=$(printf '%s\n' "$current" | grep -F "$REPO_ROOT" || true)
	if [ -n "$dropped" ]; then
		echo "replacing these unmarked lines from a previous install:"
		printf '%s\n' "$dropped" | sed 's/^/  - /'
	fi
fi

# Drop the blank lines the removal leaves at the top, so repeated installs
# don't grow a gap at the seam (trailing ones are already eaten by $(...)).
# Interior blank lines are the user's own spacing and stay untouched.
kept=$(printf '%s\n' "$kept" | sed '/./,$!d')

{
	if [ -n "$kept" ]; then
		printf '%s\n\n' "$kept"
	fi
	printf '%s\n%s\n%s\n' "$BEGIN" "$managed" "$END"
} | crontab -
