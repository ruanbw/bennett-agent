# 演示物料的生成脚本

README 里的 `docs/demo/demo.mp4` / `demo.gif` / `cover.png` 与 `docs/screenshots/*.png`
都由这里的脚本产出。**这些不是应用的依赖**：只有想重做物料时才需要 `npm install`。

```
record.mjs        真实链路录屏（Playwright + 系统 Chrome），产出 raw.webm + timeline.json
make-video.py     按 timeline.json 逐段变速，剪成 mp4 / gif / 封面
screenshots.mjs   重拍界面截图（每个场景真的发一次请求）
```

## 前置

- 后端与前端都在跑（`uv run python main.py` + `cd frontend && npm run dev`）；
- 本机装了 **Google Chrome**（脚本用 `channel: 'chrome'`，不下载 Playwright 自带的 Chromium）；
- `ffmpeg`（`brew install ffmpeg`）；
- 一个演示账号。录屏与截图都会**真的调用模型**并产生费用，别拿生产账号跑。

```bash
cd scripts/demo
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install   # 用系统 Chrome，不下载浏览器（省 150MB）
npx playwright install ffmpeg                    # 只有 1.3MB 的静态 ffmpeg，录屏编码要用
```

## 录一段演示

```bash
DEMO_EMAIL=demo@example.com DEMO_PASSWORD='...' node record.mjs --out docs/demo
uv run python ../../scripts/demo/make-video.py
```

`record.mjs` 会：登录 → 新建会话 → 逐字键入提问 → 等这一轮跑完 → 展开工具轨迹 → 收工。
同时把每个阶段的时间戳写进 `timeline.json`。

`make-video.py` 按阶段变速（默认值在文件顶部的 `DEFAULT_SPEEDS`）：

| 阶段 | 含义 | 默认 |
| --- | --- | --- |
| `blank` | 页加载 + 登录 | 整段剪掉 |
| `ready` | 空态停留 | 1.0× |
| `typing` | 键入提问 | 1.3× |
| `submitted` | 等模型 / 建机器 | 4.0× |
| `tool` | 执行 + 流式回答 | 1.0× |
| `answered` | 展开工具轨迹 | 1.0× |
| `expanded` | 结尾停留 | 1.0× |

临时调参不用改代码：`make-video.py --speed submitted=6 --cut ready`。

## 拍界面截图

```bash
DEMO_EMAIL=demo@example.com DEMO_PASSWORD='...' node screenshots.mjs          # 全部
node screenshots.mjs --only sandbox,context-usage                              # 只重拍几张
```

`sandbox` 与 `context-usage` 取景自 `--sandbox-title`（默认「部署次数统计与柱状图」）那条会话，
也就是录屏用的那条；没有这条会话时会跳过并打印提示。

## 四个踩过的坑

**1. `channel: 'chrome'` 配 `headless: true` 会把画面裁掉 90px。**
新无头模式下 screencast 只拍到约 810px 高，剩下 90px 被填成灰色，成片底部一条灰带
（就是 IDE 工具栏那么高）。`--window-size` 之类的参数不解决。所以默认**有头**录制——
会弹出一个 Chrome 窗口，能看见它在打字；真的没有显示器时再用 `--headless` 并接受灰边。

**2. 时间轴要量到「脚本动作结束」，不是「文件写完」。**
`context.close()` 之后 Playwright 还要把 webm 收尾，那段时间也会被算进片子，
表现是末尾多出十几秒画面不动的尾巴。所以 `end` 这个 mark 在脚本动作结束时打。

**3. GIF 必须和 MP4 分开出。**
GitHub 的 Markdown 不支持 `<video>`，只有 GIF 能内联自动播放；而 60 秒的 GIF 会到
十几 MB。做法是同一份时间轴出两个取景：GIF 从「刚发出」截到结尾、整体再压一档速度、
900 宽 10fps，落到 1.5MB 左右。

**4. GIF 的调色板要单独生成。**
这一屏几乎全是白底 + 灰边，用 ffmpeg 默认的通用 256 色表会出现明显色带和麻点。
脚本走两遍：`palettegen`（`stats_mode=diff`）再 `paletteuse`（bayer 抖动）。

## 为什么不直接屏幕录制

`recordVideo` 录的是**页面**，不是桌面：不会被通知弹窗、鼠标轨迹、壁纸污染，
也不用申请 macOS 的录屏权限；分辨率恒等于 viewport，重新跑一次结果一致。
代价是它只拍浏览器内容区——够用，演示的重点本来也不在桌面上。

## 素材是否入库

- `docs/demo/demo.mp4`、`demo.gif`、`cover.png`、`docs/screenshots/*.png` 入库（README 要引用）；
- `docs/demo/raw.webm`、`docs/demo/.raw/`、本目录的 `node_modules/` 不入库（`.gitignore`）。
