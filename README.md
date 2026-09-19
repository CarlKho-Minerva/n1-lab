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
