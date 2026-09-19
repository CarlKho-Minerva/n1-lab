#!/usr/bin/env bash
# Push private content to the zone and pull Carl's answers home. Content never goes through git or HTTP upload.
#   tools/push.sh push     build private/content, copy it into /data/app_data/n1 (responses/ is never touched)
#   tools/push.sh pull     fetch responses/*.jsonl into private/responses/ (append-only logs, safe to re-run)
set -euo pipefail
cd "$(dirname "$0")/.."
OH=${OH:-$HOME/.local/bin/oh}
case "${1:-}" in
push)
  python3 private/build_content.py
  tar czf - -C private/content experiments.json tasks files | "$OH" app ssh n1 'mkdir -p /data/app_data/n1 && tar xzf - -C /data/app_data/n1 && ls /data/app_data/n1'
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
  ;;
*) echo "usage: tools/push.sh push|pull" >&2; exit 2 ;;
esac
