#!/bin/bash
# launchd com.carl.n1-lab-pull, every 30 min: bring Carl's phone answers home, write a heartbeat, page once when a task completes.
# Beat rules (watchdog.sh): offline is `deferred` (short, self-resolving); a real failure is `fail` + page; never `ok` on failure.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"
cd "$(dirname "$0")/.." || exit 2
HB=~/.local/state/lifeos/heartbeats/n1-lab-pull.json
PAGE=~/CODELocalProjects/lifeos/page.sh
beat() { printf '{"job":"n1-lab-pull","ts":"%s","status":"%s","summary":%s,"progress":{"in":%s,"out":%s,"backlog":0,"last_output_ts":"%s"}}\n' \
  "$(date +%Y-%m-%dT%H:%M:%S%z | sed 's/\(..\)$/:\1/')" "$1" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$2")" "${3:-0}" "${3:-0}" "${4:-}" > "$HB.tmp" && mv "$HB.tmp" "$HB"; }
if ! curl -s -m 10 -o /dev/null https://n1.carl.selfhost.imbue.com/healthz; then
  beat deferred "zone unreachable from this Mac (offline or asleep network); will retry in 30 min"; exit 0; fi
before=$(cat private/responses/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
if ! out=$(tools/push.sh pull 2>&1); then
  beat fail "pull failed: $(echo "$out" | tail -1)"; "$PAGE" -s warn -c n1-lab "n1-lab answer pull FAILED: $(echo "$out" | tail -1)"; exit 1; fi
after=$(cat private/responses/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
last=$(ls -t private/responses/*.jsonl 2>/dev/null | head -1 | xargs -I{} date -r {} +%Y-%m-%dT%H:%M:%S%z 2>/dev/null)
summary=$(python3 - <<'PY'
import json,glob,os
out=[]
for f in sorted(glob.glob('private/responses/*.jsonl')):
    slug=os.path.basename(f)[:-6]; a={}
    for l in open(f):
        try: r=json.loads(l); a[r['id']]=r
        except Exception: pass
    try: n=len(json.load(open(f'private/content/tasks/{slug}.json'))['items'])
    except Exception: n='?'
    out.append(f"{slug}: {len(a)} of {n} answered, {sum(1 for r in a.values() if r.get('verdict')=='wrong')} marked wrong")
print('; '.join(out) or 'no answers yet')
PY
)
beat ok "$summary" "$((after - before))" "$last"
# Page once per task when it reaches 100%, so the next step (scoring agreement) is not waiting on anyone noticing.
for f in private/responses/*.jsonl; do [ -f "$f" ] || continue; slug=$(basename "$f" .jsonl); flag=private/responses/.$slug.done
  [ -f "$flag" ] && continue
  if echo "$summary" | grep -q "$slug: \([0-9]*\) of \1 answered"; then touch "$flag"; "$PAGE" -s info -c n1-lab "n1-lab: task '$slug' is complete ($summary). Answers are on the Mac in n1-lab/private/responses/."; fi
done
