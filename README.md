# 西电在线课程播放助手

**针对党课观看时“切换页面、最小化窗口就自动暂停”的问题，持续检测视频状态并尝试自动恢复播放。**

当前适配西电在线课程平台，使用 Python + Playwright 监测 HTML5 视频，并跟随视频切片换源、播放器替换和 iframe 重建。

## 解决的问题

- 观看党课时，切换到其他标签页或应用窗口后，视频自动暂停。
- 将课程浏览器窗口最小化后，视频播放被中断。
- 视频切片更换或出现历史进度“继续观看”弹窗后，停留在暂停状态。

使用时需保留课程页面、保持脚本运行，并开启页面右上角的“自动恢复”。首次播放受浏览器限制时，请手动点击一次播放；此后脚本会继续检测并恢复意外暂停。

## 功能

- 默认每秒检查播放状态，播放失败后间隔至少 3 秒重试。
- 识别“继续观看 / 该视频已播放至 … / 从头播放 / 继续播放”的历史进度弹窗，选择其中的“继续播放”，由网站恢复已有进度。
- 支持 iframe 和打开的 Shadow DOM；每个 frame 内处理显示面积最大的可见视频。
- 页面右上角提供“自动恢复”开关，需要手动暂停时先关闭开关。
- 视频自然结束后停留，等待网站切换或手动选择下一段。
- 使用独立的浏览器配置目录保存登录状态。

## Windows 快速开始

准备 Python 3.10 或以上版本，以及 Microsoft Edge 或 Google Chrome。

1. 下载仓库 ZIP 并解压，或克隆本仓库。
2. 双击 `start.bat`。首次运行会创建 `.venv` 并安装依赖。
3. 在打开的独立浏览器窗口中正常登录，进入自己的课程并打开视频。
4. 如提示自动播放受限，在播放器上手动点一次播放。

页面右上角显示 **课程播放助手 v1.1**。保持脚本运行且“自动恢复”开启，即可持续监测播放。电脑进入睡眠后无法继续监测。

## 命令行运行

```console
python -m pip install -r requirements.txt
python xidian_playback.py
```

默认打开课程列表。也可以指定自己的课程地址，链接中的 `&` 需要放在双引号内：

```console
python xidian_playback.py --url "https://dxonline.xidian.edu.cn/jjfz/lesson"
python xidian_playback.py --browser edge
python xidian_playback.py --browser chrome
```

没有安装 Edge / Chrome 时，可使用 Playwright Chromium：

```console
python -m playwright install chromium
python xidian_playback.py --browser chromium
```

命令窗口中输入以下字母并按回车：

| 命令 | 操作 |
| --- | --- |
| `p` | 开启或关闭自动恢复 |
| `o` | 打开启动时指定的课程地址 |
| `q` | 退出助手并关闭其浏览器窗口 |

关闭开关只停止自动恢复；需要暂停时，再点击播放器的暂停按钮。

## 常见情况

- **仍停在续播弹窗**：确认已退出旧进程，并重新启动 v1.1；按钮被其他元素遮挡时会提示手动点击。
- **提示需要点击播放**：浏览器的自动播放限制可能要求一次手动操作。
- **进度长时间不变或媒体报错**：检查网络及播放器提示。脚本会报告状态，不会自动刷新或跳动进度条。
- **新章节没有自动开始**：如果网站尚未切换到下一段，请手动选择目录。
- **登录目录被占用**：同一个 `.xidian_profile/<浏览器>` 目录同时只能运行一个实例。

需要签到、答题、验证码或身份确认时，按页面要求手动处理。助手只处理普通播放恢复与上述历史进度续播选择。

## 登录数据

账号密码在网站中输入即可。`.xidian_profile/` 包含浏览器登录数据，已加入 `.gitignore`，不要分享该目录。发布包与自动发布流程均只打包明确列出的源码和文档。

## 测试

自动化测试使用本地 HTTP 服务与生成的 WAV 媒体，不需要真实课程账号。

```console
python -m playwright install chromium
python tests/verify_playback.py
python tests/verify_resume_prompt.py
```

Windows 上使用已安装的 Edge 测试：

```powershell
$env:PLAYBACK_BROWSER = "msedge"
python tests/verify_playback.py
python tests/verify_resume_prompt.py
python tests/verify_minimized.py
```

前两个测试文件覆盖 14 项播放行为和 8 项历史续播弹窗检查。第三个测试会实际打开并最小化 Edge，验证普通暂停、续播弹窗出现后的播放恢复，以及窗口始终保持最小化。

2026-11-25 曾在 Windows + Edge 的真实课程页面中连续最小化 60.312 秒，视频前进 60.358 秒，中途人为暂停一次后自动恢复。该结果仅代表当时环境与课程的实测；平台更新可能需要调整适配。仓库不包含该次课程截图、个人课程参数或浏览器配置。

GitHub Actions 对每次推送和拉取请求运行两个无界面测试文件；推送 `v*` 标签且测试通过后，自动创建仅包含公开文件的发行包。

## 许可证

[MIT](LICENSE)
