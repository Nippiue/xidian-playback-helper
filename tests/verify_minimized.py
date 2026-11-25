"""真正最小化有界面的 Edge，验证媒体时间在隐藏窗口中持续增加。"""
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
helper_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'xidian_playback.py'
spec = importlib.util.spec_from_file_location('helper', helper_path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
FIXTURES = ROOT / 'work/minimized_fixture'
FIXTURES.mkdir(parents=True, exist_ok=True)
with wave.open(str(FIXTURES / 'clip.wav'), 'wb') as media:
    media.setnchannels(1)
    media.setsampwidth(2)
    media.setframerate(8000)
    media.writeframes(b'\0\0' * 8000 * 90)
(FIXTURES / 'index.html').write_text('''<!doctype html><html><head><meta charset="utf-8"></head><body>
<h1>Minimized window integration test</h1>
<video controls muted src="clip.wav" preload="auto" width="640" height="360"></video>
<div id="holder"></div>
<script>
window.mode='pause'; window.gate=false; window.resumeClicks=0; window.hiddenCount=0;
const video=document.querySelector('video');
video.addEventListener('play',()=>{ if(window.gate) video.pause(); });
document.addEventListener('visibilitychange',()=>{
 if(!document.hidden) return;
 window.simulateSitePause();
});
window.simulateSitePause=()=>{
 window.hiddenCount++;
 video.pause();
 if(window.mode==='prompt') {
  window.gate=true;
  document.querySelector('#holder').innerHTML=`<div style="position:fixed;inset:420px 0 0;background:#fff">
    <h2>继续观看</h2><p>该视频已播放至 08:16</p>
    <button onclick="window.restartClicked=true">从头播放</button>
    <button onclick="window.resumeClicks++;window.gate=false;document.querySelector('#holder').replaceChildren();video.play()">继续播放</button>
  </div>`;
 }
};
</script></body></html>''', encoding='utf-8')


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(FIXTURES),**kwargs)

    def log_message(self,*_):
        pass


async def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report={'checks':[],'samples':[]}
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(channel=os.environ.get("PLAYBACK_BROWSER") or None,headless=False)
        context=await browser.new_context(no_viewport=True)
        page=await context.new_page()
        cdp=await context.new_cdp_session(page)
        window_id=(await cdp.send('Browser.getWindowForTarget'))['windowId']

        async def bounds(state):
            await cdp.send('Browser.setWindowBounds',{'windowId':window_id,'bounds':{'windowState':state}})

        async def sample():
            data=await page.evaluate('''() => ({time:document.querySelector('video').currentTime,
                paused:document.querySelector('video').paused, hidden:document.hidden,
                focused:document.hasFocus(),
                resumeClicks, hiddenCount, gate, modal:!!document.querySelector('#holder h2')})''')
            data['windowState']=(await cdp.send('Browser.getWindowBounds',{'windowId':window_id}))['bounds']['windowState']
            return data

        async def warmup(mode):
            await bounds('normal')
            await page.goto(f'http://127.0.0.1:{server.server_port}/index.html')
            await page.bring_to_front()
            await page.evaluate('mode=>window.mode=mode',mode)
            for _ in range(30):
                await helper.watch_page(page,True,'窗口最小化测试',1000)
                if await page.evaluate("document.querySelector('video').currentTime>0.2"):
                    return
                await asyncio.sleep(0.1)
            raise AssertionError('foreground video failed to start')

        async def measure(mode,seconds):
            await warmup(mode)
            before=await sample()
            await bounds('minimized')
            # 助手的 Playwright 默认保持内容焦点；窗口仍是真正最小化。
            # 在已最小化的窗口中触发与网站相同的暂停/续播提示，验证恢复不唤起窗口。
            await page.evaluate('window.simulateSitePause()')
            start=time.monotonic()
            while time.monotonic()-start<seconds:
                _,result=await helper.watch_page(page,True,'窗口最小化测试',1000)
                point=await sample()
                point.update(mode=mode,status=result['status'],elapsed=round(time.monotonic()-start,2))
                report['samples'].append(point)
                if len(report['samples'])%4==1:
                    print(json.dumps(point,ensure_ascii=False),flush=True)
                await asyncio.sleep(0.5)
            after=await sample()
            assert after['windowState']=='minimized',after
            assert all(point['windowState']=='minimized' for point in report['samples'] if point['mode']==mode), 'watchdog restored the minimized window'
            assert after['time']-before['time']>=seconds*0.6,{'before':before,'after':after}
            assert not after['paused'],after
            if mode=='prompt':
                assert after['resumeClicks']==1 and not after['modal'],after
            report['checks'].append({'mode':mode,'elapsed':round(time.monotonic()-start,2),
                'advanced':round(after['time']-before['time'],3),'windowState':after['windowState']})
            print('PASS actual minimized window: '+mode,flush=True)

        try:
            await measure('pause',10)
            await measure('prompt',10)
            await bounds('normal')
            await page.screenshot(path=str(ROOT/'work/minimized_verified.png'))
        finally:
            try:
                await bounds('normal')
            except Exception:
                pass
            await browser.close()
            server.shutdown()
            (ROOT/'work/minimized_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('2 actual minimized-window scenarios passed.')


if __name__ == "__main__":
    asyncio.run(main())
