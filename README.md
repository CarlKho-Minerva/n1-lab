# n1-lab

A small self-hosted archive for single-subject (N=1) research, with phone-first review tasks
(tick the labels a model got wrong, read a dataset sample). Runs on an OpenHost zone behind the
zone's owner login. Python standard library only.

The repo holds code and sample data. Real write-ups, task items and answers live in the app's
data directory and are copied in over `oh app ssh` (see `tools/push.sh`); they are never committed.

    oh app deploy https://github.com/<you>/n1-lab --wait
    tools/push.sh push      # content in
    tools/push.sh pull      # answers out

Do not add anything to `public_paths` beyond `/healthz`: the app has a write endpoint.

## Private voice lab

`/voice` records explicitly labelled samples. Start the microphone, capture five seconds (or a
manual marker with five seconds before and after), replay, then Save or Discard. Audio stays in
browser RAM until Save. Stop/page-hide cancels active capture and outstanding microphone setup.
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

The real-backend browser regression covers a lost upload response followed by idempotent retry,
and a permission prompt resolving after page-hide. The separate 65-check fake-microphone browser
suite covers waveform DSP, context capture, consent, label freeze, playback/discard, mobile width,
explicit-save upload, and error states. Final external `vet-hc` review was attempted but blocked by
the account's weekly limit; passing executable tests are not a substitute for that review.
