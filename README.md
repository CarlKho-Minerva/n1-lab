# n1-lab

A small self-hosted archive for single-subject (N=1) research, with phone-first review tasks
(tick the labels a model got wrong, read a dataset sample). Runs on an OpenHost zone behind the
zone's owner login. Python standard library only.

The repo holds code and sample data. Real write-ups, task items and answers live in the app's
data directory and are copied in over `oh app ssh` (see `tools/push.sh`); they are never committed.

    oh app deploy https://github.com/<you>/n1-lab --wait
    tools/push.sh push      # content in
    tools/push.sh pull      # answers out

Do not add any owner path to `public_paths`: the app has write endpoints. The one prefix that may be added
is the reviewer area, `/r/` (below).

## Reviewer area (`/r/`)

A read-only view for outside readers, behind one shared password, on the same site and in the same design.
It is off until a password has been set: without `reviewer_auth.json` in the data directory every `/r/` path
answers the same 404 as any unknown path.

- **What a reviewer sees.** Only entries in `reviewer/experiments.json` that carry `"reviewer": true`, and only
  attachments that carry `"reviewer": true` and whose bytes in `reviewer/files/` match the `sha256` recorded at
  build time. Everything else answers 404, the same as an entry that does not exist. Review tasks are never
  shown. One entry slug is refused in code whatever the content says (`REVIEW_NEVER` in `app.py`).
- **Read-only.** Under `/r/` there is no task, answer, export, reading-log or listing route, pages carry no
  script (`Content-Security-Policy: default-src 'none'` plus inline styles, same-site images and fonts), and a
  reviewer request writes nothing to disk. The only state it changes is the wrong-password counter, in memory.
- **Sign-in.** `reviewer_auth.py` keeps a salted scrypt hash of the password and a signing key in the data
  directory, never in the repo or the image. The session cookie is signed, lasts 30 days and is `HttpOnly`,
  `Secure`, `SameSite=Lax`, `Path=/r/`. Five wrong tries lock a client out for 15 minutes; 60 wrong tries from
  anywhere in 15 minutes lock sign-in for everyone until they age out. Setting a new password signs everyone out.
- **Content.** `tools/build_reviewer.py` makes the reviewer copies from a private rules file: exact edits, every
  script removed, root links pointed at `/r/`, then a leak check. A file with any hit is refused and reported.
  `tools/push.sh push-reviewer` builds and swaps the result onto the zone (the previous set stays as
  `reviewer.old`).

```sh
printf '%s' "$PASSWORD" | oh app ssh n1 'python3 /app/reviewer_auth.py set'   # on: password from stdin, never an argument
oh app ssh n1 'python3 /app/reviewer_auth.py status'
oh app ssh n1 'python3 /app/reviewer_auth.py off'                              # off: parks the file, area answers 404
tools/push.sh push-reviewer
```

To let readers reach it, add the prefix with its trailing slash: `public_paths = ["/healthz", "/r/"]`. Only
paths under `/r/` become public; link to `/r/`, not `/r`.

**Owner guard (optional second fence).** The zone's router marks requests it has authenticated as the owner's
with `X-OpenHost-Is-Owner: true` and strips that header from clients. With a file `owner_guard` containing
`enforce` in the data directory, every route except `/healthz` and `/r/` answers 403 without that mark. Remove
the file to turn it off.

    python3 -m unittest discover -s tests -p "test_reviewer.py" -v
    python3 -m unittest discover -s tests -p "test_build_reviewer.py" -v

## Reading log

Every file page (`/files/*.html`, `/files/*.md`) and experiment page (`/x/<slug>`) carries a small script that
measures, in the browser that is showing it: seconds visible, seconds active (a touch, scroll or key in the last
60 s), the deepest point reached, and which of 100 equal bands of the document were in the viewport for how many
seconds. Viewport width, height and pixel ratio are the real ones (an iPhone reports its own), so nothing about the
screen is assumed. Checkpoints go to `POST /api/read` every 30 s and when the page is hidden; each is cumulative per
view and the highest `seq` wins. Same-origin only, validated, append-only `reads/views.jsonl` in the app data dir,
fsynced; a failed write is a 500 and the page shows `reading log NOT SAVING` after two failures.

`/reads` lists files with no visit logged and the 60 most recent sessions; experiment pages show a line and a
coverage bar under each file. `tools/push.sh pull` (and so the 30-minute Mac job) brings the log home to
`private/reads/`. On screen is not the same as read. Nothing was logged before 2026-10-17.

    python3 -m unittest discover -s tests -p test_reads.py -v

## Private voice lab

`/voice` opens a one-screen guided recorder: one drafted sentence and Record, then Listen /
Save & next / Redo. Record starts the microphone; it stops automatically after each five-second
clip. The next sentence appears only after Save. No labels, split choices, charts or instructions
compete with the current action. Settings & data opens the original advanced controls, including
manual context capture (five seconds before and after a marker). Audio stays in browser RAM until
Save. Stop/page-hide cancels active capture and outstanding microphone setup.

A link with `?study=full&mic=rode&batch=<UUID>` appends a separate, reload-resumable study:
10 new Carl clips (7 fit, 3 exploratory test), then 3 other-live, 3 background and 3 mention
test clips. Each stage has its own deterministic session ID within that batch. The other-person
stage requires an explicit permission/handoff acknowledgement; mention returns the mic to Carl.
The RØDE input name is visible, `default`/`communications` aliases are excluded, capture requests
the exact device ID, and a mismatched device is rejected. “Find RØDE” may briefly request mic
permission to reveal device labels, but never creates a clip and always releases that stream.
A new batch does not alter earlier saved samples. Completion means collection only, not validated
voice activation; replay/liveness and intent recognition remain unproven. The local evaluator
builds a fit-only speaker reference, not new neural model weights.
Labels/splits are frozen before capture; the upload payload stays identical across manual retries.
The guided sequence selects seven `carl_live` fit clips, then three test clips. Same-session tests
are **exploratory**; collect separate-session live-other examples and later live-Carl tests too.

Saved WAV/JSON pairs live in `$OPENHOST_APP_DATA_DIR/voice/samples/` (directories 0700,
files 0600), never git. OpenHost owner authentication protects the page, status, uploads and audio.
Anyone whose voice is recorded should agree before capture. This is private cloud storage, not
local-only storage. No recordings go to a remote model provider.

The existing Mac pull job calls `tools/voice_sync.py` every 30 minutes. It copies immutable pairs
to `~/.local/state/hey/voice/lab/samples`, runs the adjacent Hey repo's `voice_lab_eval.py` locally,
and publishes an advisory report to the private app. Failures exit nonzero and set a failed
heartbeat; offline runs defer. The UI polls for reports while visible. At least three usable
fit-only Carl clips construct a provisional reference; the fixed 0.60 threshold is uncalibrated.
Seven fit clips and three independent live-other test clips are a readiness minimum, not a claim
of accuracy. Replay identity matches do not establish liveness, and mentions are not impersonators.
No report changes the runtime reference or enables Jarvis: Hey remains shadow-only.

Validation commands (all synthetic; no live microphone or real voice labels):

```sh
python3 -m unittest discover -s tests -p test_voice_store.py -v
python3 -m unittest discover -s tests -p test_voice_sync.py -v
/Users/carl/.minds/.venv/bin/python -m unittest discover -s tests -p test_voice_integration.py -v
/Users/carl/.minds/.venv/bin/python tests/voice_frontend_check.py
/Users/carl/.local/venvs/carl-spk/bin/python -m unittest discover -s ../hey/tests -v
```

The real-backend browser regressions cover the one-screen sequence at 320/375/390px phone widths
and desktop size, save-to-next, redo-without-advancing, microphone shutdown after capture, a lost
upload response followed by idempotent retry, and permission resolving after page-hide. The separate 65-check fake-microphone browser
suite covers waveform DSP, context capture, consent, label freeze, playback/discard, mobile width,
explicit-save upload, and error states. Final external `vet-hc` review was attempted but blocked by
the account's weekly limit; passing executable tests are not a substitute for that review.
