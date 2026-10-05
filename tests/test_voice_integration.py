"""Browser + real backend integration, synthetic Chromium microphone only.
Run with /Users/carl/.minds/.venv/bin/python -m unittest discover -s tests -p test_voice_integration.py.
Other interpreters skip cleanly when Playwright is unavailable.
"""
import base64
import json
import uuid
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


@unittest.skipIf(sync_playwright is None, 'Playwright venv required')
class BrowserIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_data, app.DATA = app.DATA, cls.tmp.name
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/voice'
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=[
            '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()
        app.DATA = cls.old_data
        cls.tmp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def test_record_actual_backend_and_retry_lost_response(self):
        page = self.page
        page.goto(self.url)
        page.click('#advancedToggle')
        self.assertFalse(page.locator('#pending').is_visible())
        self.assertFalse(page.locator('#abort').is_visible())
        self.assertLessEqual(page.locator('#start').bounding_box()['height'], 64)
        page.click('#start')
        page.wait_for_function('N1Voice.state().running')
        page.fill('#note', 'synthetic microphone integration test')
        page.click('#rec')
        page.wait_for_function('N1Voice.state().pending !== null', timeout=15000)
        sid = page.evaluate('N1Voice.state().pending.id')
        attempts = []
        def lose_first_response(route):
            attempts.append(route.request.post_data)
            if len(attempts) == 1:
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort('failed')
            else:
                route.continue_()
        page.route('**/api/voice/sample', lose_first_response)
        page.click('#save')
        page.wait_for_function('N1Voice.state().pending && N1Voice.state().pending.error')
        self.assertTrue(page.locator('#note').is_disabled())
        self.assertEqual(page.evaluate('N1Voice.state().pending.id'), sid)
        page.click('#save')
        page.wait_for_function('N1Voice.state().pending === null')
        self.assertEqual(attempts[0], attempts[1])
        stored = json.loads((Path(self.tmp.name) / 'voice/samples' / (sid + '.json')).read_text())
        self.assertEqual(stored['duration_s'], 5)
        self.assertEqual(stored['label'], 'carl_live')
        self.assertEqual(page.locator('.spark').count(), page.locator('.list audio').count())
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        self.assertFalse(page.evaluate('N1Voice.state().running'))
        self.assertEqual(page.evaluate('N1Voice.state().tracks'), [])
        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_one_screen_guided_flow(self):
        page = self.page
        page.goto(self.url)
        self.assertFalse(page.locator('#advanced').is_visible())
        self.assertTrue(page.locator('#guideRecord').is_visible())
        self.assertIn('calendar', page.locator('#guidePrompt').inner_text())
        for width, height in [(390, 844), (375, 667), (320, 568), (1280, 800)]:
            page.set_viewport_size({'width': width, 'height': height})
            self.assertTrue(page.evaluate('document.documentElement.scrollHeight <= innerHeight'), (width, height))
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path='/tmp/n1-guide-prompt.png')
        page.click('#guideRecord')
        page.wait_for_function('N1Voice.state().recording')
        self.assertTrue(page.locator('#guideCancel').is_visible())
        page.wait_for_function('N1Voice.state().pending !== null', timeout=15000)
        self.assertFalse(page.evaluate('N1Voice.state().running'))
        self.assertEqual(page.evaluate('N1Voice.state().tracks'), [])
        page.wait_for_function('!document.getElementById("guideReview").hidden')
        self.assertTrue(page.evaluate('document.documentElement.scrollHeight <= innerHeight'))
        page.screenshot(path='/tmp/n1-guide-review.png')
        page.click('#guideListen')
        page.wait_for_function('document.getElementById("guideListen").textContent === "Stop playback"')
        page.click('#guideListen')
        sid = page.evaluate('N1Voice.state().pending.id')
        page.click('#guideSave')
        page.wait_for_function('document.getElementById("guideProgress").textContent === "Clip 2 of 10"')
        self.assertIn('weather', page.locator('#guidePrompt').inner_text())
        saved = json.loads((Path(self.tmp.name) / 'voice/samples' / (sid + '.json')).read_text())
        self.assertEqual(saved['label'], 'carl_live')
        self.assertEqual(saved['split'], 'fit')
        page.click('#guideRecord')
        page.wait_for_function('N1Voice.state().pending !== null', timeout=15000)
        page.click('#guideRedo')
        page.wait_for_function('N1Voice.state().pending === null')
        self.assertEqual(page.locator('#guideProgress').text_content(), 'Clip 2 of 10')
        self.assertFalse(page.evaluate('N1Voice.state().running'))

    def test_rode_full_study_handoffs_and_resume(self):
        page = self.page
        run = str(uuid.uuid4())
        session = lambda i: run[:-1] + format(int(run[-1], 16) ^ i, 'x')
        labels = ['carl_live', 'other_live', 'background', 'mention']
        # Real backend fixtures, isolated temporary storage; leave one clip per stage to record.
        for phase, label in enumerate(labels):
            for n in range(9 if phase == 0 else 2):
                response = page.request.post(self.url.replace('/voice', '/api/voice/sample'), data={
                    'id':str(uuid.uuid4()), 'session':session(phase), 'label':label,
                    'split':'fit' if phase == 0 and n < 7 else 'test',
                    'device':'synthetic fixture', 'note':'test only',
                    'pcm_b64':base64.b64encode(bytes(32000)).decode()})
                self.assertEqual(response.status, 200)
        page.add_init_script('''(() => {
          const enumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
          navigator.mediaDevices.enumerateDevices = async () => (await enumerate()).map(d => ({
            deviceId:d.deviceId, kind:d.kind, groupId:d.groupId,
            label:d.kind === 'audioinput' ? 'Wireless MICRO' : d.label}));
          const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          window.inputRequests = [];
          navigator.mediaDevices.getUserMedia = async args => {
            window.inputRequests.push(args); return await get(args);
          };
        })()''')
        url = self.url + '?study=full&mic=rode&batch=' + run
        page.goto(url)
        page.wait_for_function('document.getElementById("guideProgress").textContent.includes("10 / 10")')
        self.assertIn('Wireless MICRO', page.locator('#guideMic').inner_text())
        page.reload()
        page.wait_for_function('document.getElementById("guideProgress").textContent.includes("10 / 10")')
        for phase, label in enumerate(labels):
            if phase:
                page.wait_for_function('!document.getElementById("guideReady").hidden')
                self.assertFalse(page.locator('#guideRecord').is_visible())
                if phase == 1:
                    self.assertIn('permission', page.locator('#guidePrompt').inner_text())
                    page.screenshot(path='/tmp/n1-rode-other-handoff.png')
                page.click('#guideReady')
            page.click('#guideRecord')
            page.wait_for_function('N1Voice.state().pending !== null', timeout=15000)
            pending = page.evaluate('N1Voice.state().pending')
            self.assertEqual(pending['label'], label)
            self.assertEqual(pending['split'], 'test')
            self.assertEqual(page.evaluate('N1Voice.state().session'), session(phase))
            self.assertEqual(page.evaluate('N1Voice.state().tracks'), [])
            device = page.evaluate('inputRequests.at(-1).audio.deviceId.exact')
            self.assertNotIn(device, ['', 'default', 'communications'])
            self.assertTrue(page.evaluate('document.documentElement.scrollHeight <= innerHeight'))
            if phase == 0:
                page.screenshot(path='/tmp/n1-rode-review.png')
            page.click('#guideSave')
            page.wait_for_function('N1Voice.state().pending === null')
        page.wait_for_function('document.getElementById("guidePrompt").textContent === "19 new clips saved."')
        self.assertIn('Replay safety is still untested', page.locator('#guideStatus').inner_text())
        page.screenshot(path='/tmp/n1-rode-complete.png')
        page.reload()
        page.wait_for_function('document.getElementById("guidePrompt").textContent === "19 new clips saved."')
        self.assertFalse(page.locator('#guideRecord').is_visible())

    def test_rode_missing_never_records_default(self):
        page = self.page
        page.add_init_script('''(() => {
          const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async args => {
            window.probe = await get(args); return window.probe;
          };
        })()''')
        page.goto(self.url + '?study=full&mic=rode&batch=' + str(uuid.uuid4()))
        page.wait_for_function('!document.getElementById("guideConnect").hidden')
        self.assertFalse(page.locator('#guideRecord').is_visible())
        page.click('#guideConnect')
        page.wait_for_function('document.getElementById("guideStatus").textContent.includes("RØDE not found")')
        self.assertTrue(page.evaluate('probe.getTracks().every(t => t.readyState === "ended")'))
        self.assertFalse(page.evaluate('N1Voice.state().running'))
        self.assertIsNone(page.evaluate('N1Voice.state().pending'))

    def test_permission_resolving_after_pagehide_releases_tracks(self):
        self.page.add_init_script('''(() => {
          const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async args => {
            const stream = await original(args); window.heldStream = stream;
            return new Promise(resolve => { window.finishPermission = () => resolve(stream); });
          };
        })()''')
        self.page.goto(self.url)
        self.page.click('#advancedToggle')
        self.page.click('#start')
        self.page.wait_for_function('typeof finishPermission === "function"')
        self.page.evaluate("window.dispatchEvent(new Event('pagehide')); finishPermission()")
        self.page.wait_for_function('heldStream.getTracks().every(t => t.readyState === "ended")', timeout=3000)
        self.assertFalse(self.page.evaluate('N1Voice.state().running'))


if __name__ == '__main__':
    unittest.main()
