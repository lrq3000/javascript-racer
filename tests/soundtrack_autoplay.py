"""Run with `python tests/soundtrack_autoplay.py` (requires Playwright and Chromium).

Set PLAYWRIGHT_CHANNEL=msedge or chrome to use an installed browser instead.
"""

import os
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


class GameFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Reloading or closing a page can cancel an in-progress asset download.


class SoundtrackAutoplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(GameFiles, directory=root))
        cls.addClassCleanup(server.server_close)
        Thread(target=server.serve_forever, daemon=True).start()
        cls.addClassCleanup(server.shutdown)
        cls.origin = f"http://127.0.0.1:{server.server_port}"
        cls.v5 = cls.origin + "/v5.game.html"
        cls.v4 = cls.origin + "/v4.final.html"

        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.chromium = playwright.chromium
        cls.channel = os.environ.get("PLAYWRIGHT_CHANNEL")
        cls.browser = playwright.chromium.launch(
            channel=cls.channel,
            args=["--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies"],
        )
        cls.addClassCleanup(cls.browser.close)

    @contextmanager
    def game_page(self, browser=None, **options):
        with (browser or self.browser).new_context(**options) as context:
            context.add_init_script("""
                window.playbackErrors = [];
                addEventListener('unhandledrejection', event => playbackErrors.push(event.reason.name));
            """)
            page = context.new_page()
            page.set_default_timeout(5000)
            yield page, context.new_cdp_session(page)

    def wait_for_audio(self, cdp, condition):
        # Playwright evaluation runs with a user gesture by default. Observe without unlocking autoplay.
        result = cdp.send("Runtime.evaluate", {
            "expression": f"""new Promise(resolve => {{
                const music = document.getElementById('music');
                const deadline = performance.now() + 4000;
                function check() {{
                    if ({condition})
                        resolve(true);
                    else if (performance.now() >= deadline)
                        resolve({{paused: music.paused, muted: music.muted, time: music.currentTime,
                                  source: music.currentSrc, errors: playbackErrors}});
                    else
                        requestAnimationFrame(check);
                }}
                check();
            }})""",
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": False,
        })
        self.assertIs(result["result"].get("value"), True, f"Expected {condition}: {result}")

    def assert_music_playing(self, cdp):
        self.wait_for_audio(cdp, "!music.paused && !music.muted && music.currentTime > 0")
        self.wait_for_audio(cdp, "playbackErrors.length === 0")

    def test_fresh_load_starts_music_on_first_driving_key(self):
        with self.game_page() as (page, cdp):
            page.goto(self.v5, wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.paused")
            page.keyboard.press("ArrowUp")
            self.assert_music_playing(cdp)

    def test_reload_starts_music_on_first_canvas_click(self):
        with self.game_page() as (page, cdp):
            page.goto(self.v5, wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.paused")
            page.reload(wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.paused")
            page.locator("#canvas").click()
            self.assert_music_playing(cdp)

    def test_holding_onscreen_accelerator_starts_music_before_release(self):
        with self.game_page() as (page, cdp):
            page.goto(self.v5, wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.paused")
            accelerator = page.locator("#gamepad-up")
            accelerator.scroll_into_view_if_needed()
            box = accelerator.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            try:
                self.wait_for_audio(cdp, "Number(document.getElementById('speed_value').textContent) > 0")
                self.assert_music_playing(cdp)
            finally:
                page.mouse.up()

    def test_touch_gamepad_unlocks_music(self):
        with self.game_page(has_touch=True) as (page, cdp):
            page.goto(self.v5, wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.paused")
            page.locator("#gamepad-up").tap()
            self.assert_music_playing(cdp)

    def test_v5_link_keeps_autoplay_when_allowed(self):
        with self.game_page() as (page, cdp):
            page.goto(self.v4, wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2")
            page.get_by_role("link", name="out of time").click()
            page.wait_for_url(self.v5, wait_until="domcontentloaded")
            self.assert_music_playing(cdp)

    def test_allowed_autoplay_respects_mute_on_load_and_reload(self):
        browser = self.chromium.launch(channel=self.channel, args=["--autoplay-policy=no-user-gesture-required"])
        try:
            for muted in (False, True):
                with self.subTest(muted=muted), self.game_page(browser) as (page, cdp):
                    if muted:
                        page.add_init_script("localStorage.muted = 'true';")
                    # Prevent the first frame from rendering so playback cannot depend on loaded sprites.
                    blocked_sprites = []

                    def block_sprites(route):
                        blocked_sprites.append(route.request.url)
                        route.abort()

                    page.route("**/images/sprites.png", block_sprites)
                    page.goto(self.v5, wait_until="domcontentloaded")
                    for entry in ("direct", "reload"):
                        with self.subTest(entry=entry):
                            if entry == "reload":
                                page.reload(wait_until="domcontentloaded")
                            if muted:
                                self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.muted && music.paused")
                            else:
                                self.assert_music_playing(cdp)
                    self.assertEqual(len(blocked_sprites), 2)
        finally:
            browser.close()

    def test_mute_is_saved_and_unmuting_starts_playback(self):
        with self.game_page() as (page, cdp):
            page.goto(self.v5, wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.readyState >= 2 && music.paused")
            page.locator("#mute").click()
            self.wait_for_audio(cdp, "music.muted && music.paused && localStorage.muted === 'true'")
            page.reload(wait_until="domcontentloaded")
            self.wait_for_audio(cdp, "music.loop && music.muted")
            page.keyboard.press("ArrowUp")
            self.wait_for_audio(cdp, "music.muted && music.paused")
            page.locator("#mute").click()
            self.assert_music_playing(cdp)
            self.wait_for_audio(cdp, "localStorage.muted === 'false'")

    def test_mp3_plays_if_ogg_is_unavailable(self):
        with self.game_page() as (page, cdp):
            page.route("**/*.ogg", lambda route: route.abort())
            page.goto(self.v5, wait_until="domcontentloaded")
            page.keyboard.press("ArrowUp")
            self.assert_music_playing(cdp)
            self.wait_for_audio(cdp, "new URL(music.currentSrc).pathname === '/music/jsracer_soundtrack.mp3'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
