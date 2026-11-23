#!/usr/bin/env bash
# Push private content to the zone and pull Carl's answers home. Content never goes through git or HTTP upload.
#   tools/push.sh push     build private/content, copy it into /data/app_data/n1 (responses/ is never touched)
#   tools/push.sh pull     fetch responses/*.jsonl into private/responses/ (append-only logs, safe to re-run)
#   tools/push.sh push-reviewer   build the reviewer copies (tools/build_reviewer.py, private/reviewer_rules.json) from
#                          private/content as it stands and swap them into /data/app_data/n1/reviewer; the previous
#                          set is kept beside it as reviewer.old. Touches nothing else on the zone.
set -euo pipefail
cd "$(dirname "$0")/.."
OH=${OH:-$HOME/.local/bin/oh}
case "${1:-}" in
push)
  python3 private/build_content.py
  tar czf - -C private/content experiments.json tasks files | "$OH" app ssh n1 'mkdir -p /data/app_data/n1 && tar xzf - -C /data/app_data/n1 && ls /data/app_data/n1'
  ;;
push-reviewer)
  # The build refuses any file with a leak-check hit and exits non-zero (leaving the last good build) on a bad rule.
  python3 tools/build_reviewer.py --rules private/reviewer_rules.json --content private/content --out private/reviewer_build
  [ -f private/reviewer_build/reviewer/experiments.json ] || { echo "FAIL: no reviewer build to push" >&2; exit 1; }
  # Unpack beside the live folder, check it arrived whole, then swap. A failed copy leaves the live folder untouched.
  tar czf - -C private/reviewer_build reviewer | "$OH" app ssh n1 'set -e; cd /data/app_data/n1; rm -rf reviewer.new; mkdir reviewer.new; tar xzf - -C reviewer.new; test -f reviewer.new/reviewer/experiments.json; rm -rf reviewer.old; if [ -d reviewer ]; then mv reviewer reviewer.old; fi; mv reviewer.new/reviewer reviewer; rmdir reviewer.new; echo "reviewer files on the zone: $(ls reviewer/files | wc -l)"' \
    || { echo "FAIL: the reviewer copy did not reach the zone; what was live before is still live" >&2; exit 1; }
  ;;
pull)
  mkdir -p private/responses
  # The remote side always answers with one explicit word first, so "nothing yet" and "it broke" cannot look alike.
  state=$("$OH" app ssh n1 'cd /data/app_data/n1 2>/dev/null && { [ -d responses ] && ls responses/*.jsonl >/dev/null 2>&1 && echo HAVE || echo EMPTY; } || echo NODATA') \
    || { echo "FAIL: could not reach the n1 app over oh app ssh" >&2; exit 1; }
  case "$state" in
    *HAVE*) "$OH" app ssh n1 'cd /data/app_data/n1 && tar czf - responses' | tar xzf - -C private/ \
              || { echo "FAIL: responses exist on the zone but the copy failed" >&2; exit 1; }
            wc -l private/responses/*.jsonl ;;
    *EMPTY*) echo "no responses yet (checked: the app answered, responses/ is empty)" ;;
    *) echo "FAIL: unexpected state from the zone: '$state' (is content pushed? is the data path right?)" >&2; exit 1 ;;
  esac
  # The reading log (time on screen and scroll coverage per page view) comes home the same way.
  rstate=$("$OH" app ssh n1 '[ -f /data/app_data/n1/reads/views.jsonl ] && echo HAVE || echo EMPTY') \
    || { echo "FAIL: could not reach the n1 app over oh app ssh for the reading log" >&2; exit 1; }
  case "$rstate" in
    *HAVE*) "$OH" app ssh n1 'cd /data/app_data/n1 && tar czf - reads' | tar xzf - -C private/ \
              || { echo "FAIL: the reading log exists on the zone but the copy failed" >&2; exit 1; }
            echo "reads: $(wc -l < private/reads/views.jsonl | tr -d ' ') checkpoints" ;;
    *EMPTY*) echo "reads: none logged yet" ;;
    *) echo "FAIL: unexpected reading-log state from the zone: '$rstate'" >&2; exit 1 ;;
  esac
  ;;
*) echo "usage: tools/push.sh push|pull|push-reviewer" >&2; exit 2 ;;
esac
