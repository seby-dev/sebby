#!/usr/bin/env bash
# A file barrier for the QA swarm's collision scenarios.
#
#   barrier.sh wait <dir> <name> <count> [timeout_seconds]
#   barrier.sh spread <dir>
#
# `wait` writes ready-<name> into <dir>, then blocks until <count> different names have
# arrived. Exactly one waiter decides the outcome, through `ln -s <name> go.lock`, a symlink
# that is the lock and names its owner in one atomic step, and writes either
# `go` (released) or `abandoned` (a timeout). Every waiter then returns: 0 if released, 75 if
# abandoned. The caller chains its action after `wait` with `&&`, so every participant acts
# within one poll (about 50 ms) of the last arrival. `spread` prints one JSON line with the
# outcome and how far apart the participants returned.
#
# Exit codes: 0 released, 75 abandoned, 64 usage error (nothing is created), 1 the folder
# can't be written.
#
# A waiter that a signal (TERM, INT, or HUP) stops, such as a tool timeout, abandons the
# barrier and exits 75: it takes `go.lock` and writes `abandoned` (reason=signal), or, if it
# already holds the lock with no outcome written (it was deciding), it writes `abandoned`
# then. The lock's owner is read back from the symlink, not taken from `ln`'s exit status, so
# a signal to the whole process group that kills `ln` after it made the link still leaves a
# lock its taker recognizes. If another participant holds the lock, it leaves the outcome to
# that one. If the outcome is `go`, a stopped waiter also removes its own `acted-<name>`,
# since its action never ran. A waiter whose parent is gone (the shell that would run the
# `&&` action was killed) does the same (reason=orphaned), on its next poll and again just
# before it writes `acted-<name>`; that last check also compares its parent pid, to catch a
# reused pid. A `kill -0` that fails with EPERM (a live parent of another user) isn't taken
# as a gone parent. This narrows, but can't close, the gap between exiting 0 and the action
# running: a waiter stopped after it exits 0 can't be told apart from one that acted. If the
# decider itself stalls holding `go.lock`, no `go` or `abandoned` file appears: every waiter
# times out with 75, and `spread` reports the outcome as `pending`. SIGKILL can't be trapped:
# a participant killed that way writes no `acted-` file, which `spread` shows as
# `released_count` exceeding the number of `acted` entries. An older version's lock, a
# `go.lock` directory with an `owner` file, is still honored as a lock.
#
# The outcome file and `spread` are authoritative, not one participant's exit code: a waiter
# that times out and loses the lock waits a grace period for the decider's file, and if the
# decider writes `go` only after that, the late waiter has already returned 75 while the
# others return 0.
#
# Portable to macOS /bin/bash 3.2 and GNU/Linux bash: no associative arrays, no mapfile, no
# case-changing expansions, and no dependence on EPOCHREALTIME (it's a bash 5 extra).
# QA_BARRIER_CLOCK=perl|python3|date|bash forces one clock, for tests: a forced clock that
# fails falls back to `date` only, never to another millisecond clock.
set -u
export LC_ALL=C

USAGE='usage: barrier.sh wait <dir> <name> <count 1-16> [timeout_seconds 1-600, default 300]
       barrier.sh spread <dir>'
NAME_RE='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
COUNT_RE='^[0-9]{1,2}$'
TIMEOUT_RE='^[0-9]{1,3}$'
MS_RE='^[1-9][0-9]{0,15}$' # no leading zero (octal) and no 64-bit overflow
RELEASED_RE='^[1-9][0-9]?$'
GRACE=5 # seconds a timed-out loser waits for the winner's file

die_usage() {
  printf 'barrier.sh: %s\n%s\n' "$1" "$USAGE" >&2
  exit 64
}

# Sets NOW_MS to the current time in milliseconds. Bash 5's EPOCHREALTIME needs no fork;
# otherwise perl, then python3, then whole seconds from date. A forced clock
# (QA_BARRIER_CLOCK) falls back only to date, so a test of it proves that clock ran.
now_ms() {
  NOW_MS=""
  case "${QA_BARRIER_CLOCK:-}" in
    perl) ms_perl ;;
    python3) ms_python ;;
    bash) ms_bash ;;
    date) ;;
    *)
      ms_bash
      [ -n "$NOW_MS" ] || ms_perl
      [ -n "$NOW_MS" ] || ms_python
      ;;
  esac
  [[ $NOW_MS =~ $MS_RE ]] || NOW_MS=""
  [ -n "$NOW_MS" ] || ms_date
}

ms_date() {
  NOW_MS="$(date +%s)000"
}

ms_bash() {
  local t="${EPOCHREALTIME:-}" frac
  [ -n "$t" ] || return 0
  t="${t/,/.}" # some locales print a decimal comma
  frac="${t#*.}000"
  NOW_MS="${t%.*}${frac:0:3}"
}

ms_perl() {
  command -v perl >/dev/null 2>&1 || return 0
  NOW_MS="$(perl -MTime::HiRes=time -e 'printf "%d\n", time() * 1000' 2>/dev/null)"
}

ms_python() {
  command -v python3 >/dev/null 2>&1 || return 0
  NOW_MS="$(python3 -c 'import time; print(int(time.time() * 1000))' 2>/dev/null)"
}

# write_atomic <file> <text>: a dot-prefixed temp file in the same folder, then a rename, so
# a reader never sees half a file and a temp file never matches ready-* or acted-*.
write_atomic() {
  local dir="${1%/*}" base="${1##*/}"
  local tmp="$dir/.$base.tmp.$$"
  printf '%s\n' "$2" >"$tmp" && mv -f "$tmp" "$1"
}

nap() {
  sleep 0.05 2>/dev/null || sleep 1
}

# Sets READY to the number of ready-* files.
count_ready() {
  local f
  READY=0
  for f in "$DIR"/ready-*; do
    [ -e "$f" ] && READY=$((READY + 1))
  done
}

# take_lock: tries to create go.lock, a symlink to this waiter's name. Exactly one `ln -s`
# succeeds, and the link and its owner appear together. `-n` keeps ln from following an
# existing link. ln's exit status is ignored: whether this waiter won is read back by
# lock_owner, so an ln killed after it made the link still counts.
take_lock() {
  [ -e "$DIR/go.lock" ] || [ -L "$DIR/go.lock" ] || ln -sn "$NAME" "$DIR/go.lock" 2>/dev/null
  # An older version's go.lock directory, made in the moment before ln ran, gets the link
  # inside it instead: remove that stray, since the directory is the lock.
  if [ -d "$DIR/go.lock" ] && [ ! -L "$DIR/go.lock" ] && [ -L "$DIR/go.lock/$NAME" ]; then
    rm -f "$DIR/go.lock/$NAME"
  fi
}

# Sets OWNER to go.lock's owner: the symlink's target, or an older directory lock's owner
# file. Empty when there's no lock or its owner can't be read.
lock_owner() {
  OWNER=""
  if [ -L "$DIR/go.lock" ]; then
    OWNER="$(readlink "$DIR/go.lock" 2>/dev/null)"
  elif [ -d "$DIR/go.lock" ]; then
    IFS= read -r OWNER 2>/dev/null <"$DIR/go.lock/owner"
  fi
}

# Tries to become the barrier's one decider: 0 if this waiter holds go.lock.
claim() {
  take_lock
  lock_owner
  [ "$OWNER" = "$NAME" ]
}

on_signal() {
  stop_waiter signal
}

# Exits 75 if the parent that would run the `&&` action is gone. `kill -0` also fails with
# EPERM for a live process of another user, so a failure is checked once more with ps.
check_parent() {
  kill -0 "$PARENT" 2>/dev/null && return 0
  ps -p "$PARENT" >/dev/null 2>&1 && return 0
  stop_waiter orphaned
}

# stop_waiter <reason>: abandons the barrier and exits 75. It writes `abandoned` if it wins
# go.lock, or if it already holds the lock and no outcome is written yet (it was deciding).
# A lock held by another participant is left to that one.
# If the outcome is `go`, it removes its own acted- file (and any temp file for it): a waiter
# stopped while it wrote that file never returned 0, so its action never ran.
stop_waiter() {
  trap '' TERM INT HUP
  take_lock
  lock_owner
  if [ "$OWNER" = "$NAME" ] && [ ! -e "$DIR/go" ] && [ ! -e "$DIR/abandoned" ]; then
    write_abandoned "$1"
  fi
  if [ -e "$DIR/go" ]; then
    rm -f "$DIR/acted-$NAME" "$DIR/.acted-$NAME.tmp.$$"
  fi
  printf 'barrier.sh: abandoned (%s): stopped (%s)\n' "$DIR" "$1" >&2
  exit 75
}

write_abandoned() {
  now_ms
  write_atomic "$DIR/abandoned" "abandoned_ms=$NOW_MS by=$NAME reason=$1"
}

release() {
  local ppid
  check_parent
  # The parent pid may have been reused since this waiter started: compare the real one. An
  # empty answer (no ps) leaves the kill -0 check above as the only one.
  ppid="$(ps -o ppid= -p $$ 2>/dev/null)"
  ppid="${ppid//[!0-9]/}"
  [ -z "$ppid" ] || [ "$ppid" = "$PARENT" ] || stop_waiter orphaned
  now_ms
  write_atomic "$DIR/acted-$NAME" "$NOW_MS" || fail_io
  # The trap stays set until the script exits: a signal now still drops acted- and exits 75,
  # because the caller's action hasn't run yet.
  exit 0
}

gave_up() {
  trap - TERM INT HUP
  count_ready
  printf 'barrier.sh: abandoned (%s: %s of %s arrived): %s\n' "$DIR" "$READY" "$COUNT" "$1" >&2
  exit 75
}

fail_io() {
  printf 'barrier.sh: can'"'"'t write in %s\n' "$DIR" >&2
  exit 1
}

# After losing the lock at a timeout, the winner is writing `go` or `abandoned` right now.
await_outcome() {
  local since=$SECONDS
  while [ $((SECONDS - since)) -le "$GRACE" ]; do
    check_parent
    [ -e "$DIR/go" ] && release
    [ -e "$DIR/abandoned" ] && break
    nap
  done
  gave_up "the barrier timed out"
}

cmd_wait() {
  DIR=$1
  NAME=$2
  local count=$3 timeout=$4
  [ -d "$DIR" ] || die_usage "'$DIR' isn't an existing directory"
  [[ $NAME =~ $NAME_RE ]] || die_usage "the name must match $NAME_RE"
  [[ $count =~ $COUNT_RE ]] || die_usage "the count must be an integer from 1 to 16"
  COUNT=$((10#$count))
  { [ "$COUNT" -ge 1 ] && [ "$COUNT" -le 16 ]; } || die_usage "the count must be from 1 to 16"
  [[ $timeout =~ $TIMEOUT_RE ]] || die_usage "the timeout must be an integer from 1 to 600"
  TIMEOUT=$((10#$timeout))
  { [ "$TIMEOUT" -ge 1 ] && [ "$TIMEOUT" -le 600 ]; } || die_usage "the timeout must be from 1 to 600"

  [ -e "$DIR/abandoned" ] && gave_up "this barrier was already abandoned"
  PARENT=$PPID
  trap on_signal TERM INT HUP # before the arrival, so no arrival is left without its trap
  now_ms
  write_atomic "$DIR/ready-$NAME" "$NOW_MS" || fail_io
  START=$SECONDS
  while :; do
    check_parent
    [ -e "$DIR/go" ] && release
    [ -e "$DIR/abandoned" ] && gave_up "another participant timed out"
    count_ready
    if [ "$READY" -ge "$COUNT" ] && claim; then
      now_ms
      write_atomic "$DIR/go" "released_ms=$NOW_MS by=$NAME count=$READY" || fail_io
      release
    elif [ $((SECONDS - START)) -gt "$TIMEOUT" ]; then
      if claim; then
        now_ms
        write_atomic "$DIR/abandoned" "abandoned_ms=$NOW_MS by=$NAME reason=timeout" || fail_io
        gave_up "the barrier timed out"
      fi
      await_outcome
    fi
    nap
  done
}

cmd_spread() {
  DIR=$1
  [ -d "$DIR" ] || die_usage "'$DIR' isn't an existing directory"
  local outcome=pending f base name ms lo="" hi="" acted="" spread=null released=null
  local line="" word
  [ -e "$DIR/go" ] && outcome=go
  [ "$outcome" = pending ] && [ -e "$DIR/abandoned" ] && outcome=abandoned
  if [ "$outcome" = go ]; then
    IFS= read -r line 2>/dev/null <"$DIR/go"
    set -f # the words are data, never globs
    for word in $line; do
      case "$word" in
        count=*) [[ ${word#count=} =~ $RELEASED_RE ]] && released=${word#count=} ;;
      esac
    done
    set +f
  fi
  count_ready
  for f in "$DIR"/acted-*; do
    [ -f "$f" ] || continue
    base="${f##*/}"
    name="${base#acted-}"
    [[ $name =~ $NAME_RE ]] || continue # a stray name could break the JSON
    ms=""
    IFS= read -r ms 2>/dev/null <"$f"
    [[ $ms =~ $MS_RE ]] || continue # validated before any arithmetic
    acted="$acted${acted:+, }\"$name\": $ms"
    { [ -z "$lo" ] || [ "$ms" -lt "$lo" ]; } && lo=$ms
    { [ -z "$hi" ] || [ "$ms" -gt "$hi" ]; } && hi=$ms
  done
  [ -n "$lo" ] && spread=$((hi - lo))
  printf '{"outcome": "%s", "count": %s, "released_count": %s, "acted": {%s}, "spread_ms": %s}\n' \
    "$outcome" "$READY" "$released" "$acted" "$spread"
}

main() {
  [ "$#" -ge 1 ] || die_usage "missing command"
  case "$1" in
    wait)
      { [ "$#" -ge 4 ] && [ "$#" -le 5 ]; } || die_usage "wait takes <dir> <name> <count> [timeout_seconds]"
      cmd_wait "$2" "$3" "$4" "${5:-300}"
      ;;
    spread)
      [ "$#" -eq 2 ] || die_usage "spread takes <dir>"
      cmd_spread "$2"
      ;;
    *) die_usage "unknown command '$1'" ;;
  esac
}

main "$@"
