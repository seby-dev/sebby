#!/usr/bin/env bash
# A file barrier for the QA swarm's collision scenarios.
#
#   barrier.sh wait <dir> <name> <count> [timeout_seconds]
#   barrier.sh spread <dir>
#
# `wait` writes ready-<name> into <dir>, then blocks until <count> different names have
# arrived. Exactly one waiter decides the outcome, through `mkdir go.lock`, and writes either
# `go` (released) or `abandoned` (a timeout). Every waiter then returns: 0 if released, 75 if
# abandoned. The caller chains its action after `wait` with `&&`, so every participant acts
# within one poll (about 50 ms) of the last arrival. `spread` prints one JSON line with the
# outcome and how far apart the participants returned.
#
# Exit codes: 0 released, 75 abandoned, 64 usage error (nothing is created), 1 the folder
# can't be written. A waiter that a signal (TERM, INT, or HUP) stops, such as a tool timeout,
# withdraws its arrival (removes its ready file) unless the decider has already started, and
# exits 75. If the decider itself stalls after `mkdir go.lock`, no `go` or `abandoned` file
# appears: every waiter times out with 75, and `spread` reports the outcome as `pending`.
#
# Portable to macOS /bin/bash 3.2 and GNU/Linux bash: no associative arrays, no mapfile, no
# case-changing expansions, and no dependence on EPOCHREALTIME (it's a bash 5 extra).
# QA_BARRIER_CLOCK=perl|python3|bash forces one clock, for tests.
set -u
export LC_ALL=C

USAGE='usage: barrier.sh wait <dir> <name> <count 1-16> [timeout_seconds 1-600, default 300]
       barrier.sh spread <dir>'
NAME_RE='^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
COUNT_RE='^[0-9]{1,2}$'
TIMEOUT_RE='^[0-9]{1,3}$'
MS_RE='^[0-9]+$'
GRACE=5 # seconds a timed-out loser waits for the winner's file

die_usage() {
  printf 'barrier.sh: %s\n%s\n' "$1" "$USAGE" >&2
  exit 64
}

# Sets NOW_MS to the current time in milliseconds. Bash 5's EPOCHREALTIME needs no fork;
# otherwise perl, then python3, then whole seconds from date.
now_ms() {
  NOW_MS=""
  case "${QA_BARRIER_CLOCK:-}" in
    perl) ms_perl ;;
    python3) ms_python ;;
  esac
  [ -n "$NOW_MS" ] || ms_bash
  [ -n "$NOW_MS" ] || ms_perl
  [ -n "$NOW_MS" ] || ms_python
  [ -n "$NOW_MS" ] || NOW_MS="$(date +%s)000"
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

# Tries to become the barrier's one decider. Exactly one mkdir succeeds.
claim() {
  mkdir "$DIR/go.lock" 2>/dev/null || return 1
  printf '%s\n' "$NAME" >"$DIR/go.lock/owner"
}

# Stops a waiter that a signal ends. It withdraws its arrival, so a stopped participant can't
# make the others believe it's still coming, unless the decider has already started.
on_signal() {
  [ -e "$DIR/go.lock" ] || rm -f "$DIR/ready-$NAME"
  exit 75
}

release() {
  trap - TERM INT HUP
  now_ms
  write_atomic "$DIR/acted-$NAME" "$NOW_MS" || fail_io
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
  now_ms
  write_atomic "$DIR/ready-$NAME" "$NOW_MS" || fail_io
  START=$SECONDS
  trap on_signal TERM INT HUP
  while :; do
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
        write_atomic "$DIR/abandoned" "abandoned_ms=$NOW_MS by=$NAME" || fail_io
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
  local outcome=pending f base name ms lo="" hi="" acted="" spread=null
  [ -e "$DIR/go" ] && outcome=go
  [ "$outcome" = pending ] && [ -e "$DIR/abandoned" ] && outcome=abandoned
  count_ready
  for f in "$DIR"/acted-*; do
    [ -e "$f" ] || continue
    base="${f##*/}"
    name="${base#acted-}"
    ms=""
    IFS= read -r ms <"$f"
    [[ $ms =~ $MS_RE ]] || continue
    acted="$acted${acted:+, }\"$name\": $ms"
    { [ -z "$lo" ] || [ "$ms" -lt "$lo" ]; } && lo=$ms
    { [ -z "$hi" ] || [ "$ms" -gt "$hi" ]; } && hi=$ms
  done
  [ -n "$lo" ] && spread=$((hi - lo))
  printf '{"outcome": "%s", "count": %s, "acted": {%s}, "spread_ms": %s}\n' \
    "$outcome" "$READY" "$acted" "$spread"
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
