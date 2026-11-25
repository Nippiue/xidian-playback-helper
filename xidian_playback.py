#!/usr/bin/env python3
"""西电在线课程播放助手。Python 3.10+，依赖 playwright。"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_URL = "https://dxonline.xidian.edu.cn/jjfz/lesson"
COURSE_HOST = "dxonline.xidian.edu.cn"
RESUME_TIMESTAMP = re.compile(r"该视频已播放至\s*\d{1,3}[:：]\d{2}(?:[:：]\d{2})?")

# 每次轮询重新找 video，因此支持换源、替换节点、iframe 和打开的 Shadow DOM。
# play() 不等待 Promise 返回，避免网络缓冲时阻塞 Python 的控制循环。
WATCH_VIDEO = r"""(options) => {
    const key = '__xidian_playback_helper_v1__';
    const state = window[key] || (window[key] = {
        entries: new WeakMap(), requested: null, panel: null
    });
    const enabled = state.requested === null ? options.enabled : state.requested;
    const requested = state.requested;
    state.requested = null;

    if (options.main && document.body) {
        if (!state.panel || !state.panel.host.isConnected) {
            const host = document.createElement('div');
            host.id = 'xidian-playback-helper';
            host.style.cssText = 'all:initial;position:fixed;top:12px;right:12px;z-index:2147483647;display:block';
            const root = host.attachShadow({mode: 'open'});
            root.innerHTML = `<style>
                :host {color-scheme:light;}
                .box {box-sizing:border-box;width:238px;padding:12px;background:#fff;
                    color:#172033;border:1px solid #cbd5e1;border-radius:10px;
                    box-shadow:0 3px 16px #0002;font:13px/1.5 system-ui,sans-serif;}
                strong {display:block;margin-bottom:7px;font-size:14px;}
                button {width:100%;padding:7px;border:0;border-radius:6px;
                    color:#fff;background:#166534;font:inherit;cursor:pointer;}
                button[aria-pressed=false] {background:#475569;}
                p {margin:8px 0 0;overflow-wrap:anywhere;}
                small {display:block;margin-top:5px;color:#64748b;}
            </style><div class="box"><strong>课程播放助手 v1.1</strong>
            <button type="button"></button><p role="status"></p>
            <small>手动暂停前，请先关闭自动恢复。</small></div>`;
            const button = root.querySelector('button');
            button.addEventListener('click', () => {
                state.requested = !(state.requested === null
                    ? button.getAttribute('aria-pressed') === 'true' : state.requested);
                button.setAttribute('aria-pressed', String(state.requested));
                button.textContent = '自动恢复：' + (state.requested ? '开' : '关');
            });
            document.body.appendChild(host);
            state.panel = {host, button, status: root.querySelector('[role=status]')};
        }
        state.panel.button.setAttribute('aria-pressed', String(enabled));
        state.panel.button.textContent = '自动恢复：' + (enabled ? '开' : '关');
        state.panel.status.textContent = options.summary || '正在检测播放器…';
    }

    const videos = [];
    function collect(root) {
        videos.push(...root.querySelectorAll('video'));
        for (const node of root.querySelectorAll('*')) {
            if (node.shadowRoot) collect(node.shadowRoot);
        }
    }
    collect(document);
    if (options.resumePrompt) {
        const now = performance.now();
        const retryPrompt = enabled && now - (state.promptLastTry ?? -Infinity) >= options.retryMs;
        if (retryPrompt) state.promptLastTry = now;
        return {requested, count: videos.length, status: enabled ? 'resume-prompt' : 'disabled', retryPrompt};
    }
    const visible = videos.map(video => {
        const rect = video.getBoundingClientRect();
        const style = getComputedStyle(video);
        return {video, area: rect.width * rect.height,
            visible: video.isConnected && rect.width >= 100 && rect.height >= 60
                && style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity) !== 0};
    }).filter(item => item.visible).sort((a, b) => b.area - a.area);
    const result = {requested, count: videos.length, status: 'no-video'};
    if (!visible.length) {
        result.status = videos.length ? 'hidden-video' : 'no-video';
        return result;
    }

    // 一帧内只处理占地面积最大的可见视频，避免播放隐藏的预加载视频。
    const video = visible[0].video;
    const source = [video.currentSrc, video.getAttribute('src'),
        ...Array.from(video.querySelectorAll('source'), item => item.src)].join('|');
    let entry = state.entries.get(video);
    if (!entry || entry.source !== source || entry.stream !== video.srcObject) {
        entry = {source, stream: video.srcObject, pending: false, lastTry: -Infinity,
            lastError: '', resumes: 0, lastTime: video.currentTime, progressAt: performance.now(), pauseSince: null};
        state.entries.set(video, entry);
    }
    const now = performance.now();
    if (!video.paused || !enabled) entry.pauseSince = null;
    else if (entry.pauseSince == null) entry.pauseSince = now;
    if (Math.abs(video.currentTime - entry.lastTime) > 0.05) {
        entry.lastTime = video.currentTime;
        entry.progressAt = now;
    }
    result.time = Number.isFinite(video.currentTime) ? video.currentTime : 0;
    result.duration = Number.isFinite(video.duration) ? video.duration : null;
    result.resumes = entry.resumes;
    if (!enabled) {
        result.status = 'disabled';
    } else if (video.ended) {
        result.status = 'ended';
    } else if (video.error) {
        result.status = 'media-error';
        result.error = String(video.error.code);
    } else if (!video.currentSrc && !video.getAttribute('src')
            && !video.querySelector('source[src]') && !video.srcObject) {
        result.status = 'no-source';
    } else if (!video.paused) {
        result.status = now - entry.progressAt > 30000 ? 'stalled'
            : entry.pending || video.readyState < 3 ? 'buffering' : 'playing';
        entry.lastError = '';
    } else {
        result.status = entry.lastError === 'NotAllowedError' ? 'click-required'
            : now - entry.pauseSince > 15000 ? 'paused-blocked'
            : entry.pending ? 'buffering' : 'resuming';
        result.error = entry.lastError;
        if (!entry.pending && now - entry.lastTry >= options.retryMs) {
            entry.lastTry = now;
            entry.pending = true;
            entry.progressAt = now;
            try {
                Promise.resolve(video.play()).then(() => {
                    entry.pending = false;
                    entry.lastError = '';
                    if (!video.paused) entry.resumes += 1;
                }, error => {
                    entry.pending = false;
                    entry.lastError = error && error.name || 'PlayError';
                });
            } catch (error) {
                entry.pending = false;
                entry.lastError = error && error.name || 'PlayError';
            }
        }
    }
    return result;
}"""

STATUS_TEXT = {
    "no-video": "等待你进入课程并打开视频",
    "hidden-video": "播放器暂未显示，请展开视频",
    "disabled": "自动恢复已关闭，可以手动暂停",
    "ended": "当前视频已结束，等待网站切换或手动选择下一段",
    "media-error": "视频资源报错，请检查网络或播放器提示",
    "no-source": "等待播放器加载视频地址",
    "playing": "正在播放",
    "stalled": "播放进度长时间未变化，请检查网络或页面提示",
    "click-required": "浏览器限制自动播放，请在播放器上手动点一次播放",
    "buffering": "正在等待视频缓冲",
    "resuming": "检测到暂停，正在恢复播放",
    "paused-blocked": "多次尝试仍未播放，请检查页面弹窗或手动点击播放",
    "resume-prompt": "发现历史进度，正在处理“继续播放”弹窗",
    "resume-selected": "已点击“继续播放”，等待视频开始播放",
    "resume-prompt-blocked": "续播弹窗尚未关闭，请在页面上手动点击“继续播放”",
}


def is_course_url(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.hostname == COURSE_HOST
            and (parts.path == "/jjfz" or parts.path.startswith("/jjfz/")))


def clock_text(seconds: float) -> str:
    value = max(0, int(seconds))
    return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"


def choose_result(results: list[dict]) -> dict:
    # 主页面可能没有 video，而播放器位于 iframe 中。
    candidates = [item for item in results if item.get("status") not in
                  {"no-video", "hidden-video"}]
    return (candidates or results or [{"status": "no-video"}])[0]


def describe(result: dict) -> str:
    status = result.get("status", "no-video")
    text = STATUS_TEXT.get(status, status)
    if status == "playing":
        text += " " + clock_text(result.get("time", 0))
        if result.get("duration") is not None:
            text += " / " + clock_text(result["duration"])
    if status == "media-error":
        text += f"（媒体错误码 {result.get('error', '?')}）"
    elif result.get("error") and status != "click-required":
        text += f"（{result['error']}）"
    return text


def read_commands(commands: queue.Queue) -> None:
    while True:
        try:
            commands.put(input().strip().lower())
        except (EOFError, OSError):
            return


async def watch_page(page, enabled: bool, summary: str, retry_ms: int) -> tuple[bool, dict]:
    from playwright.async_api import Error as PlaywrightError

    results = []
    # 先处理顶层按钮，再同步给所有 iframe；帧导航途中失败留待下次轮询。
    for frame in list(page.frames):
        try:
            if frame != page.main_frame:
                owner = await asyncio.wait_for(frame.frame_element(), timeout=3)
                try:
                    if not await owner.is_visible():
                        continue
                finally:
                    await owner.dispose()
            prompt = await find_resume_prompt(frame)
            result = await asyncio.wait_for(frame.evaluate(WATCH_VIDEO, {
                "enabled": enabled, "main": frame == page.main_frame,
                "summary": summary, "retryMs": retry_ms,
                "resumePrompt": prompt is not None,
            }), timeout=3)
            if frame == page.main_frame and result.get("requested") is not None:
                enabled = bool(result["requested"])
            if prompt is not None and enabled:
                if prompt["button"] is None:
                    result["status"] = "resume-prompt-blocked"
                elif result.get("retryPrompt"):
                    try:
                        # 使用网站按钮的正常点击事件，由网站恢复历史进度。
                        await prompt["button"].click(timeout=1200)
                        result["status"] = "resume-selected"
                    except PlaywrightError:
                        result["status"] = "resume-prompt-blocked"
            results.append(result)
        except Exception as error:
            # Playwright 在页面跳转或切片重建 iframe 时会抛异常。
            if page.is_closed():
                break
            if not isinstance(error, asyncio.TimeoutError):
                # 只吞掉浏览器操作异常；编程错误仍向上抛出。
                if not type(error).__module__.startswith("playwright"):
                    raise
    return enabled, choose_result(results)


async def find_resume_prompt(frame):
    """只识别截图所示的历史进度选择框，排除其他确认/签到弹窗。"""
    matches = []
    titles = frame.get_by_text("继续观看", exact=True).filter(visible=True)
    for title in (await titles.all())[:5]:
        # 最近的、含历史进度提示的祖先，避免把整个页面当作弹窗范围。
        container = title.locator(
            "xpath=ancestor::*[not(self::body or self::html) and "
            ".//*[contains(normalize-space(.), '该视频已播放至')]][1]"
        )
        if await container.count() != 1:
            continue
        text = await container.inner_text(timeout=500)
        if len(text) > 500 or not RESUME_TIMESTAMP.search(text):
            continue
        restart = container.get_by_text("从头播放", exact=True).filter(visible=True)
        if await restart.count() != 1:
            continue
        button = container.get_by_text("继续播放", exact=True).filter(visible=True)
        if await button.count() == 0:
            button = container.get_by_role("button", name="继续播放", exact=True).filter(visible=True)
        matches.append({"button": button if await button.count() == 1 else None})
    if len(matches) == 1:
        return matches[0]
    return {"button": None} if matches else None


async def launch(playwright, args):
    channels = {"edge": "msedge", "chrome": "chrome", "chromium": None}
    names = [args.browser] if args.browser != "auto" else ["edge", "chrome", "chromium"]
    errors = []
    for name in names:
        profile = args.profile / name
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), channel=channels[name],
                headless=False, no_viewport=True, accept_downloads=False,
            )
            print(f"已启动 {name}；独立登录目录：{profile}", flush=True)
            return context
        except Exception as error:
            if not type(error).__module__.startswith("playwright"):
                raise
            errors.append(f"{name}: {str(error).splitlines()[0]}")
    raise RuntimeError("无法启动浏览器。请安装 Edge/Chrome，或执行 python -m playwright install chromium。\n"
                       "如果已有脚本窗口，请先退出它，避免占用相同登录目录。\n" + "\n".join(errors))


async def run(args) -> None:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError as error:
        raise RuntimeError("缺少依赖，请先执行：python -m pip install playwright") from error
    commands: queue.Queue = queue.Queue()
    threading.Thread(target=read_commands, args=(commands,), daemon=True).start()
    async with async_playwright() as playwright:
        context = await launch(playwright, args)
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())
        enabled = True
        summary = "等待打开课程视频"
        last_status = None
        last_report = 0.0
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeout:
                print("页面加载较慢，将继续等待；也可以在浏览器中手动刷新。", flush=True)
            print("请在打开的浏览器中登录，进入课程，必要时手动点一次播放。", flush=True)
            print("登录后如未回到课程，在这里输入 o 并回车。", flush=True)
            print("命令：p 回车=开/关自动恢复；o 回车=打开原课程；q 回车=退出。", flush=True)
            while not closed.is_set():
                while not commands.empty():
                    command = commands.get_nowait()
                    if command == "q":
                        return
                    if command == "p":
                        enabled = not enabled
                        print("自动恢复：" + ("开" if enabled else "关"), flush=True)
                    elif command == "o":
                        pages = [item for item in context.pages if not item.is_closed()]
                        page = pages[-1] if pages else await context.new_page()
                        try:
                            await page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
                        except PlaywrightTimeout:
                            print("课程页面仍在加载。", flush=True)
                pages = [item for item in context.pages if not item.is_closed()]
                if not pages:
                    break
                course_pages = [item for item in pages if is_course_url(item.url)]
                if course_pages:
                    # 课程若在新窗口打开，跟随最新打开的课程页。
                    page = course_pages[-1]
                    enabled, result = await watch_page(page, enabled, summary, args.retry_ms)
                    summary = describe(result)
                    status = (result.get("status"), enabled, result.get("error"))
                else:
                    summary = "等待登录或进入课程页面；登录后可输入 o 回车打开原课程"
                    status = ("waiting-course", enabled, None)
                now = time.monotonic()
                if status != last_status or now - last_report >= 30:
                    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("[%H:%M:%S] ")
                    print(stamp + summary, flush=True)
                    last_status, last_report = status, now
                try:
                    await asyncio.wait_for(closed.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            with contextlib.suppress(Exception):
                await context.close()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="西电在线课程播放助手：恢复普通暂停并跟随播放器切片换源。")
    parser.add_argument("--url", default=DEFAULT_URL, help="课程页面地址")
    parser.add_argument("--browser", choices=["auto", "edge", "chrome", "chromium"], default="auto")
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parent / ".xidian_profile",
                        help="独立浏览器登录目录（同一目录同时只能运行一个实例）")
    parser.add_argument("--interval", type=float, default=1.0, help="检测间隔，默认 1 秒")
    parser.add_argument("--retry-ms", type=int, default=3000, help="播放失败后重试间隔，默认 3000 毫秒")
    args = parser.parse_args()
    if not is_course_url(args.url):
        parser.error("请输入 https://dxonline.xidian.edu.cn/jjfz/ 下的课程地址")
    if not 0.5 <= args.interval <= 60:
        parser.error("--interval 必须介于 0.5 和 60 秒")
    if args.retry_ms < 1000:
        parser.error("--retry-ms 至少为 1000 毫秒")
    args.profile = args.profile.resolve()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception as error:
        print(f"运行失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
