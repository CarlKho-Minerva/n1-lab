#!/usr/bin/env python3
"""Browser tests for voice.html against the in-memory stub (tests/voice_stub_server.py).

Runs headless Chromium with Chrome's fake microphone, so the full path is exercised: getUserMedia,
Web Audio tap, 5 s recording, resampling to 16 kHz, Save with an injected failure and retry, context
capture with marker, discard, and the no-background-capture rule. Also asserts that the page never
contacts any host but its own.

    ~/.minds/.venv/bin/python tests/voice_frontend_check.py

Exit code is non-zero on any failure (design law 3: silence is the bug).
"""
import json
import math
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_stub_server as stub  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("FAIL: playwright not importable. Use ~/.minds/.venv/bin/python (it has playwright + chromium).", file=sys.stderr)
    sys.exit(2)

PORT = int(os.environ.get("VOICE_TEST_PORT", "8766"))
BASE = f"http://127.0.0.1:{PORT}"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


def post(path):
    urllib.request.urlopen(urllib.request.Request(BASE + path, data=b"{}", method="POST")).read()


def status():
    return json.loads(urllib.request.urlopen(BASE + "/api/voice/status").read())


def wait_for(page, expr, timeout=12.0, poll=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if page.evaluate(expr):
            return True
        time.sleep(poll)
    return False


def main():
    srv = stub.serve(PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    external, console_errors, page_errors = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 375, "height": 760}, permissions=["microphone"], device_scale_factor=2)
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" and '500' not in m.text else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        def on_request(req):
            if not req.url.startswith((BASE, 'blob:' + BASE)):
                external.append(req.url)
        page.on("request", on_request)

        page.goto(BASE + "/voice")
        page.click('#advancedToggle')
        check("page loads with policy pill from /api/voice/status",
              wait_for(page, "document.getElementById('policy').textContent==='policy: collection_only'"))
        check("supported browser: unsupported banner hidden", page.evaluate("document.getElementById('unsupported').hidden"))
        check("no horizontal overflow at 375px", page.evaluate("document.scrollingElement.scrollWidth<=window.innerWidth+1"),
              str(page.evaluate("[document.scrollingElement.scrollWidth, window.innerWidth]")))
        check("every button has an accessible name", page.evaluate(
            "[...document.querySelectorAll('button')].every(b=>(b.textContent.trim()||b.getAttribute('aria-label')))"))
        check("every canvas has an aria-label or is aria-hidden", page.evaluate(
            "[...document.querySelectorAll('canvas')].every(c=>c.getAttribute('aria-label')||c.getAttribute('aria-hidden'))"))
        check("Draft prompts header present and attributed to the agent", page.evaluate(
            "[...document.querySelectorAll('h4')].some(h=>h.textContent==='Draft prompts') && document.body.textContent.includes(\"not Carl's own statements\")"))
        check("consent text names upload to OpenHost, not local-only", page.evaluate(
            "document.body.textContent.includes('Save uploads this audio') && document.body.textContent.includes('not local-only')"))
        check("no deploy / train controls exist", page.evaluate(
            "![...document.querySelectorAll('button,input,select')].some(e=>/deploy|train/i.test(e.textContent+(e.getAttribute('aria-label')||'')))"))

        # ---- DSP unit checks through the exposed pure functions
        dsp = page.evaluate("""() => {
          const V=window.N1Voice; const sr=48000, n=sr; const mk=(f,a)=>{const x=new Float32Array(n);for(let i=0;i<n;i++)x[i]=a*Math.sin(2*Math.PI*f*i/sr);return x};
          const low=V.resample(mk(1000,0.5),sr,16000), hi=V.resample(mk(20000,0.5),sr,16000), dc=V.resample(new Float32Array(n).fill(0.3),sr,16000);
          const s16=V.toS16(new Float32Array([1.5,-1.5,0,0.5])); const bytes=new Uint8Array(s16.buffer);
          const same=V.resample(mk(440,0.2),16000,16000);
          return {lowLen:low.length, lowRms:V.stats(low).rms, hiRms:V.stats(hi).rms, dcMid:dc[8000], s16:[...s16], b64:V.b64(bytes), sameLen:same.length,
                  low44:V.resample(mk(1000,0.5),44100,16000).length};
        }""")
        check("resample 48k->16k length", dsp["lowLen"] == 16000, str(dsp["lowLen"]))
        check("resample preserves 1 kHz amplitude (rms 0.3536)", abs(dsp["lowRms"] - 0.5 / math.sqrt(2)) < 0.01, str(dsp["lowRms"]))
        check("resample kills 20 kHz alias (rms < 0.005)", dsp["hiRms"] < 0.005, str(dsp["hiRms"]))
        check("resample DC gain is 1", abs(dsp["dcMid"] - 0.3) < 1e-3, str(dsp["dcMid"]))
        check("resample 44.1k->16k length", dsp["low44"] == math.floor(48000 * 16000 / 44100), str(dsp["low44"]))
        check("toS16 clamps and scales", dsp["s16"] == [32767, -32768, 0, 16383], str(dsp["s16"]))
        check("b64 of s16 bytes", dsp["b64"] == "/38AgAAA/z8=", dsp["b64"])
        check("same-rate resample is a copy", dsp["sameLen"] == 48000)

        # ---- microphone
        check("capture buttons disabled before mic starts", page.evaluate("document.getElementById('rec').disabled && document.getElementById('ctx').disabled"))
        page.click("#start")
        check("mic running with live tracks", wait_for(page, "N1Voice.state().running && N1Voice.state().tracks.every(t=>t==='live')"))
        check("actual settings shown (echoCancellation row present)", page.evaluate("document.getElementById('settings').textContent.includes('echoCancellation')"))
        check("record enabled, context disabled until prebuffer", page.evaluate("!document.getElementById('rec').disabled && document.getElementById('ctx').disabled"))
        check("RMS meter updates from audio", wait_for(page, "document.getElementById('rms').textContent!=='RMS - dBFS'", 5))
        check("context unlocks after 5 s prebuffer", wait_for(page, "!document.getElementById('ctx').disabled", 9))

        # ---- record 5 s, label frozen, save with injected failure, retry
        page.check("input[name=split][value=test]")
        page.fill("#note", "test note")
        page.click("#rec")
        check("label/split frozen while recording", page.evaluate("[...document.querySelectorAll('#labels input,#splits input')].every(i=>i.disabled) && !document.getElementById('frozen').hidden"))
        check("countdown runs from 5.0", wait_for(page, "parseFloat(document.getElementById('countdown').firstChild.nodeValue)<4.5", 3))
        check("pending clip appears after 5 s", wait_for(page, "N1Voice.state().pending!==null", 8))
        pend = page.evaluate("N1Voice.state().pending")
        check("pending is ~5.0 s at 16 kHz (160000 bytes)", pend and abs(pend["duration_s"] - 5.0) < 0.02 and pend["bytes"] == 160000, str(pend))
        check("pending carries label/split chosen before capture", pend and pend["label"] == "carl_live" and pend["split"] == "test", str(pend))
        check("still frozen while pending", page.evaluate("[...document.querySelectorAll('#labels input,#splits input')].every(i=>i.disabled)"))
        check("nothing uploaded by recording alone", len(status()["samples"]) == 0)
        check("save state reads 'not saved'", page.evaluate("document.getElementById('savestate').textContent==='not saved'"))
        post("/__test/fail_next?n=1")
        page.click("#save")
        check("NOT SAVED shown on server failure", wait_for(page, "document.getElementById('savestate').textContent.startsWith('NOT SAVED')", 6),
              page.evaluate("document.getElementById('savestate').textContent"))
        pend2 = page.evaluate("N1Voice.state().pending")
        check("clip retained in memory with the same id", pend2 and pend2["id"] == pend["id"])
        check("retry button offered", page.evaluate("document.getElementById('save').textContent==='Retry save' && !document.getElementById('save').disabled"))
        page.click("#save")
        check("retry saves", wait_for(page, "N1Voice.state().pending===null", 6), page.evaluate("document.getElementById('savestate').textContent"))
        st = status()
        check("server holds exactly one sample with that id", len(st["samples"]) == 1 and st["samples"][0]["id"] == pend["id"], json.dumps(st)[:200])
        check("server-side duration ~5.0 s", abs(st["samples"][0]["duration_s"] - 5.0) < 0.02)
        check("note and split reached the server", st["samples"][0]["note"] == "test note" and st["samples"][0]["split"] == "test")
        check("session id is a uuid and matches page", st["samples"][0]["session"] == page.evaluate("N1Voice.state().session"))
        check("label/split unfrozen after save", page.evaluate("[...document.querySelectorAll('#labels input,#splits input')].every(i=>!i.disabled) && document.getElementById('frozen').hidden"))
        check("saved list shows the clip with a playback element", wait_for(page, "document.querySelectorAll('#list audio').length===1", 4))
        check("saved clip audio is preload=none (no autoplay)", page.evaluate("document.querySelector('#list audio').getAttribute('preload')==='none' && !document.querySelector('#list audio').autoplay"))
        check("counts line reflects 1 carl_live test", page.evaluate("document.getElementById('counts').textContent.includes('carl_live 0 fit / 1 test')"),
              page.evaluate("document.getElementById('counts').textContent"))
        check("step strip marks one test step", page.evaluate("document.querySelectorAll('#steps i.test').length===1"))
        check("recommended next step is fit (1 done of 7)", page.evaluate("document.getElementById('stepstext').textContent.includes('step 2 of 10') && document.getElementById('stepstext').textContent.includes('fit')"))

        # ---- context capture, marker, discard
        check("context still enabled (prebuffer kept across the clip)", wait_for(page, "!document.getElementById('ctx').disabled", 8))
        page.click("#ctx")
        check("pending context clip appears", wait_for(page, "N1Voice.state().pending!==null", 8))
        pc = page.evaluate("N1Voice.state().pending")
        check("context clip is ~10 s with marker at 5.0", pc and abs(pc["duration_s"] - 10.0) < 0.02 and pc["marker_s"] == 5 and pc["bytes"] == 320000, str(pc))
        check("jump-to-marker visible", page.evaluate("!document.getElementById('tomarker').hidden"))
        page.click("#tomarker")
        page.click("#play")
        check("local playback runs", wait_for(page, "document.getElementById('play').textContent==='Stop playback'", 3))
        page.click("#play")
        page.click("#discard")
        check("discard clears pending without upload", page.evaluate("N1Voice.state().pending===null") and len(status()["samples"]) == 1)

        # ---- context save carries context_marker_s
        check("context enabled again", wait_for(page, "!document.getElementById('ctx').disabled", 8))
        page.check("input[name=label][value=mention]")
        page.click("#ctx")
        check("second context clip ready", wait_for(page, "N1Voice.state().pending!==null", 8))
        page.click("#save")
        check("context clip saved", wait_for(page, "N1Voice.state().pending===null", 6))
        st = status()
        ctxs = [s for s in st["samples"] if s["label"] == "mention"]
        check("server received context_marker_s=5 and label mention", len(ctxs) == 1 and ctxs[0]["context_marker_s"] == 5 and abs(ctxs[0]["duration_s"] - 10) < 0.02, json.dumps(ctxs)[:200])

        # ---- new session changes id; hidden page stops all tracks
        s1 = page.evaluate("N1Voice.state().session")
        page.click("#newsession")
        check("new session yields a different uuid", page.evaluate("N1Voice.state().session") != s1)
        page.click("#rec")
        time.sleep(0.5)
        page.evaluate("Object.defineProperty(document,'hidden',{get:()=>true,configurable:true});document.dispatchEvent(new Event('visibilitychange'))")
        check("hidden page: mic stopped, all tracks ended, recording aborted", wait_for(page,
              "!N1Voice.state().running && N1Voice.state().tracks.length===0 && !N1Voice.state().recording && N1Voice.state().pending===null", 3),
              str(page.evaluate("N1Voice.state()")))
        check("hidden page: capture buttons disabled, label unfrozen", page.evaluate("document.getElementById('rec').disabled && document.getElementById('ctx').disabled && document.getElementById('frozen').hidden"))
        check("stop message is loud, not silent", page.evaluate("document.getElementById('live').textContent.includes('page hidden')"))

        # ---- reload gives a fresh session
        page.evaluate("Object.defineProperty(document,'hidden',{get:()=>false,configurable:true})")
        page.reload()
        page.click('#advancedToggle')
        wait_for(page, "document.getElementById('policy').textContent==='policy: collection_only'")
        check("fresh page gets a fresh session uuid", page.evaluate("N1Voice.state().session") not in (s1,))
        check("saved samples from before still listed (2)", wait_for(page, "document.querySelectorAll('#list .item').length===2", 4))
        check("status tolerates missing optional fields (list still renders)", page.evaluate("document.querySelectorAll('#list .item').length===2"))

        # ---- status failure is shown, not swallowed
        page.route("**/api/voice/status", lambda r: r.fulfill(status=500, body='{"ok":false,"error":"boom"}'))
        page.click("#refresh")
        check("status error surfaces", wait_for(page, "!document.getElementById('statuserr').hidden && document.getElementById('policy').textContent==='policy: unreachable'", 4))
        page.unroute("**/api/voice/status")

        browser.close()
    srv.shutdown()
    check("no external requests", not external, "; ".join(external[:5]))
    check("no uncaught page errors", not page_errors, "; ".join(page_errors[:3]))
    check("no console errors", not console_errors, "; ".join(console_errors[:3]))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
