"""用真实 Edge 媒体元素验证播放恢复，测试网页仅在本地提供。"""
import asyncio
import os
import contextlib
import importlib.util
import json
import sys
import threading
import time
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("helper", ROOT / "xidian_playback.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
from playwright.async_api import async_playwright

FIXTURES = ROOT / "work/media_fixture"
FIXTURES.mkdir(parents=True, exist_ok=True)
with wave.open(str(FIXTURES / "clip.wav"), "wb") as media:
    media.setnchannels(1)
    media.setsampwidth(2)
    media.setframerate(8000)
    media.writeframes(b"\0\0" * 8000 * 45)
with wave.open(str(FIXTURES / "short.wav"), "wb") as media:
    media.setnchannels(1)
    media.setsampwidth(2)
    media.setframerate(8000)
    media.writeframes(b"\0\0" * 8000)
(FIXTURES / "index.html").write_text("""<!doctype html><html><body>
<h1>Local playback integration fixture</h1>
<video id="v" src="/clip.wav" muted controls preload="auto" width="480" height="270"></video>
</body></html>""", encoding="utf-8")


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def log_message(self, *_):
        pass


async def main():
    checks = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    url = f"http://127.0.0.1:{port}/index.html"

    def passed(name):
        checks.append(name)
        print("PASS " + name, flush=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel=os.environ.get("PLAYBACK_BROWSER") or None, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def tick(enabled=True):
            return await helper.watch_page(page, enabled, "本地测试", 1000)

        async def drive(condition, enabled=True, frame=None, seconds=6):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                await tick(enabled)
                if await (frame or page).evaluate(condition):
                    return
                await asyncio.sleep(0.15)
            raise AssertionError(condition)

        try:
            assert helper.is_course_url(helper.DEFAULT_URL)
            assert not helper.is_course_url("https://dxonline.xidian.edu.cn.evil.test/jjfz/play")
            assert not helper.is_course_url("https://dxonline.xidian.edu.cn/user/login")
            passed("course URL scope")

            await page.goto(url)
            await drive("!document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1")
            passed("starts actual HTML5 media")

            await page.evaluate("document.querySelector('video').pause()")
            await drive("!document.querySelector('video').paused")
            passed("resumes ordinary pause")

            await page.evaluate("""() => {
                const old = document.querySelector('video');
                const video = old.cloneNode(true);
                video.muted = true;
                old.replaceWith(video);
            }""")
            await drive("!document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1")
            passed("follows replaced player node")

            await page.evaluate("document.querySelector('video').src = '/clip.wav?slice=2'")
            await drive("!document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1")
            passed("follows source change on same player")

            await page.evaluate("document.querySelector('video').pause()")
            for _ in range(9):
                await tick(False)
                await asyncio.sleep(0.15)
            assert await page.evaluate("document.querySelector('video').paused")
            passed("disabled switch preserves manual pause")
            await page.get_by_role("button", name="自动恢复：关", exact=True).click()
            enabled, _ = await tick(False)
            assert enabled is True
            await drive("!document.querySelector('video').paused")
            passed("in-page switch turns recovery back on")

            await page.evaluate("document.querySelector('video').src = '/short.wav'")
            await drive("!document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1")
            await page.wait_for_function("document.querySelector('video').ended", timeout=8000)
            for _ in range(8):
                await tick()
                await asyncio.sleep(0.15)
            assert await page.evaluate("document.querySelector('video').ended && document.querySelector('video').paused")
            passed("natural ending is not replayed")

            await page.evaluate("""url => {
                document.querySelector('video').remove();
                const frame = document.createElement('iframe');
                frame.src = url;
                frame.width = '700'; frame.height = '400';
                document.body.appendChild(frame);
            }""", f"http://localhost:{port}/index.html")
            await page.frame_locator('iframe').locator('video').wait_for()
            child = page.frames[-1]
            await drive("!document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1", frame=child)
            passed("starts video inside cross-origin iframe")

            await page.locator('iframe').evaluate("node => node.style.display = 'none'")
            await child.evaluate("document.querySelector('video').pause()")
            for _ in range(8):
                await tick()
                await asyncio.sleep(0.15)
            assert await child.evaluate("document.querySelector('video').paused")
            passed("does not restart a hidden iframe player")
            await page.locator('iframe').evaluate("node => node.style.display = 'block'")

            await child.evaluate("document.querySelector('video').pause()")
            await page.bring_to_front()
            background_tab = await context.new_page()
            await background_tab.goto("about:blank")
            await drive("!document.querySelector('video').paused", frame=child)
            await background_tab.close()
            passed("recovery while another browser tab is open")

            await page.reload()
            await drive("!document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1")
            await tick()
            await tick()
            assert await page.locator('#xidian-playback-helper').count() == 1
            passed("navigation reattaches helper without duplicate panels")

            await page.evaluate("""() => {
                document.querySelector('video').remove();
                const node = document.createElement('div');
                node.attachShadow({mode:'open'}).innerHTML = '<video muted src="/clip.wav" width="480" height="270"></video>';
                document.body.appendChild(node);
            }""")
            await drive("!document.querySelector('div:not(#xidian-playback-helper)').shadowRoot.querySelector('video').paused")
            passed("detects video in open shadow DOM")

            blocked_context = await browser.new_context()
            blocked_page = await blocked_context.new_page()
            await blocked_page.goto(url)
            # 本机 Edge 允许该本地页自动播放；注入标准 API 拒绝来验证限制提示。
            await blocked_page.evaluate("""() => {
                const video = document.querySelector('video');
                video.play = () => Promise.reject(new DOMException('Simulated policy', 'NotAllowedError'));
            }""")
            await helper.watch_page(blocked_page, True, "test", 1000)
            await asyncio.sleep(0.3)
            _, blocked = await helper.watch_page(blocked_page, True, "test", 1000)
            assert blocked['status'] == 'click-required', blocked
            passed("simulated autoplay denial gives click-required status")
            await blocked_context.close()
        finally:
            await browser.close()
            server.shutdown()
    (ROOT / "work/verification.json").write_text(json.dumps({"passed": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(checks)} integration checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
