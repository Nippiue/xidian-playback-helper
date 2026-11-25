"""回归：历史进度弹窗必须先交给网站的“继续播放”按钮处理。"""
import asyncio
import os
import importlib.util
import json
import sys
import threading
import time
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
helper_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "xidian_playback.py"
spec = importlib.util.spec_from_file_location("helper", helper_path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
FIXTURES = ROOT / "work/resume_fixture"
FIXTURES.mkdir(parents=True, exist_ok=True)
with wave.open(str(FIXTURES / "clip.wav"), "wb") as media:
    media.setnchannels(1)
    media.setsampwidth(2)
    media.setframerate(8000)
    media.writeframes(b"\0\0" * 8000 * 30)
(FIXTURES / "resume.html").write_text("""<!doctype html><html><head><meta charset="utf-8"></head><body>
<video muted src="clip.wav" width="600" height="350" preload="auto" controls></video>
<button id="unrelated" onclick="window.unrelatedClicks++">继续播放</button>
<div id="mask" style="position:fixed;inset:400px 0 0;background:#0002">
 <div id="resume" style="background:white;padding:20px;width:400px">
  <div id="title">继续观看</div><p id="record">该视频已播放至 08:16</p>
  <div class="buttons"><div id="restart" onclick="window.chosen='restart'">从头播放</div>
  <div class="public_submit" onclick="window.continueVideo()">继续播放</div></div>
 </div>
</div>
<script>
window.unrelatedClicks = 0; window.resumeClicks = 0; window.chosen = '';
window.gate = true;
const video = document.querySelector('video');
video.addEventListener('play', () => { if (window.gate) video.pause(); });
window.continueVideo = () => {
  window.resumeClicks++; window.chosen='resume'; window.gate=false;
  document.querySelector('#mask').remove(); video.play();
};
</script></body></html>""", encoding="utf-8")
(FIXTURES / "outer.html").write_text("<html><body><h1>Iframe fixture</h1></body></html>", encoding="utf-8")


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def log_message(self, *_):
        pass


async def main():
    passed = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel=os.environ.get("PLAYBACK_BROWSER") or None, headless=True)
        page = await browser.new_page()

        async def fresh():
            await page.goto(base + "/resume.html")

        async def tick(enabled=True):
            return await helper.watch_page(page, enabled, "Regression test", 1000)

        async def drive(frame=None, seconds=5):
            end = time.monotonic() + seconds
            result = {}
            while time.monotonic() < end:
                _, result = await tick()
                if await (frame or page).evaluate("window.resumeClicks === 1 && !document.querySelector('video').paused && document.querySelector('video').currentTime > 0.1"):
                    return
                await asyncio.sleep(0.15)
            detail = await (frame or page).evaluate("({resumeClicks, chosen, paused:document.querySelector('video').paused, time:document.querySelector('video').currentTime, modal:!!document.querySelector('#resume')})")
            detail['helper'] = result
            prompt = await helper.find_resume_prompt((frame or page).main_frame if frame is None else frame) if hasattr(helper, 'find_resume_prompt') else None
            detail['matched'] = prompt is not None
            titles = (frame or page).get_by_text('继续观看', exact=True).filter(visible=True)
            detail['titles'] = await titles.count()
            if detail['titles']:
                container = titles.first.locator("xpath=ancestor::*[not(self::body or self::html) and .//*[contains(normalize-space(.), '该视频已播放至')]][1]")
                detail['containers'] = await container.count()
                if detail['containers']:
                    detail['containerText'] = await container.inner_text()
            await page.screenshot(path=str(ROOT / 'work/resume_failure.png'))
            raise AssertionError(f"Historical resume dialog blocked playback: {detail}")

        def ok(name):
            passed.append(name)
            print("PASS " + name, flush=True)

        try:
            await fresh()
            await drive()
            assert await page.evaluate("chosen === 'resume' && unrelatedClicks === 0")
            for _ in range(3):
                await tick()
            assert await page.evaluate("resumeClicks === 1")
            ok("clicks only the history resume action, then actual progress advances")

            await fresh()
            for _ in range(3):
                await tick(False)
            assert await page.evaluate("resumeClicks === 0 && document.querySelector('video').paused")
            ok("disabled recovery leaves the history choice untouched")
            await page.get_by_role('button', name='自动恢复：关', exact=True).click()
            enabled, _ = await tick(False)
            assert enabled
            await drive()
            ok("enabling the panel also resumes a pending history dialog")

            await fresh()
            await page.locator('#title').evaluate("node => node.textContent = '请完成签到'")
            for _ in range(3):
                await tick()
            assert await page.evaluate("resumeClicks === 0 && unrelatedClicks === 0")
            ok("does not click an unrelated confirmation prompt")

            await fresh()
            await page.locator('#record').evaluate("node => node.textContent = '请确认已完成学习任务'")
            await tick()
            assert await page.evaluate("resumeClicks === 0")
            ok("requires the historical playback timestamp")

            await fresh()
            await page.locator('#mask').evaluate("node => node.style.display = 'none'")
            await tick()
            assert await page.evaluate("resumeClicks === 0")
            ok("ignores a hidden history dialog")

            await page.goto(base + "/outer.html")
            await page.evaluate("""url => {
                const frame = document.createElement('iframe');
                frame.width='900'; frame.height='800'; frame.src=url;
                document.body.appendChild(frame);
            }""", f"http://localhost:{server.server_port}/resume.html")
            await page.frame_locator('iframe').locator('#resume').wait_for()
            await drive(frame=page.frames[-1])
            ok("handles a history resume dialog inside a cross-origin iframe")

            await fresh()
            await page.locator('.public_submit').evaluate("node => node.style.pointerEvents = 'none'")
            _, result = await tick()
            assert result['status'] == 'resume-prompt-blocked', result
            assert await page.evaluate("resumeClicks === 0 && document.querySelector('video').paused")
            ok("reports a blocked resume action instead of claiming playback")
        finally:
            await browser.close()
            server.shutdown()
    (ROOT / 'work/resume_verification.json').write_text(json.dumps({'passed': passed}, indent=2), encoding='utf-8')
    print(f"{len(passed)} resume regression checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
