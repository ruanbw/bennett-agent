# 演示物料的生成脚本

README 开头的两段视频都由这里的脚本产出：

| 物料 | 录什么 | 脚本 |
| --- | --- | --- |
| `docs/demo/demo.mp4` / `demo.gif` / `cover.png` | 用户端：对话 + 沙箱真跑代码 + 上下文预算 | `record.mjs` + `make-video.py` |
| `docs/demo-admin/demo.mp4` / `cover.png` | 管理端：提供商/模型、用户与角色；后端：`/docs`、探针、指标 | `record-admin.mjs` + `make-video.py` |

两组产物都由 `docs/index.html`（GitHub Pages 播放页）引用，README 的封面图链到那一页。

**这些不是应用的依赖**：只有想重做物料时才需要 `npm install`。

```
record.mjs        用户端录屏（Playwright + 系统 Chrome），产出 raw.webm + timeline.json
record-admin.mjs  管理端 + 后端录屏，同样的产出
make-video.py     按 timeline.json 逐段变速，剪成 mp4 / gif / 封面
screenshots.mjs   重拍界面截图（每个场景真的发一次请求）
```

## 前置

- 后端与两个前端都在跑：`uv run python main.py --reload`（:8000）、`cd frontend && npm run dev`（:5173）、`cd admin-frontend && npm run dev`（:5174）；
- 沙箱在跑（`docs/demo` 那段要真的执行代码）：`OPENSANDBOX_INSECURE_SERVER=YES uvx opensandbox-server --config docker/opensandbox.toml`；
- 本机装了 **Google Chrome**（脚本用 `channel: 'chrome'`，不下载 Playwright 自带的 Chromium）；
- `ffmpeg`（`brew install ffmpeg`）；
- 两个演示账号。录屏会**真的调用模型**并产生费用，别拿生产账号跑。

```bash
cd scripts/demo
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install   # 用系统 Chrome，不下载浏览器（省 150MB）
npx playwright install ffmpeg                    # 只有 1.3MB 的静态 ffmpeg，录屏编码要用
```

## 录用户端（README 的主视频）

```bash
DEMO_EMAIL=demo@example.com DEMO_PASSWORD='...' node record.mjs --out docs/demo
uv run python ../../scripts/demo/make-video.py
```

`record.mjs` 会：登录 → 打开模型选择器 → 键入提问 → 等这一轮跑完 → 展开工具轨迹 → 开上下文弹层 → 追问一句确认工作区还在。同时把每个阶段的时间戳写进 `timeline.json`。

`make-video.py` 按阶段变速（默认值在文件顶部的 `DEFAULT_SPEEDS`）：

| 阶段 | 含义 | 默认 |
| --- | --- | --- |
| `blank` | 页加载 + 登录 | 整段剪掉 |
| `ready` | 空态停留 | 1.0× |
| `models` | 打开模型选择器 | 1.0× |
| `typing` | 键入提问 | 1.1× |
| `submitted` | 等模型 / 建机器 | 2.5× |
| `tool` | 执行 + 流式回答 | 1.0× |
| `answered` | 结果停留 | 1.0× |
| `expanded` | 展开工具轨迹 | 1.0× |
| `usage` | 上下文用量弹层 | 1.0× |
| `typing2` / `submitted2` / `tool2` | 第二轮追问 | 1.5× / 3.0× / 1.4× |
| `wait2` | 第二轮**没**调工具时的纯等待 | 6.0× |
| `answered2` | 结尾停留 | 1.0× |

临时调参不用改代码：`make-video.py --speed submitted=4 --cut ready`。

`wait2` 是分支：第二轮模型有时会凭上一轮的上下文直接回答、不再进沙箱。录屏脚本按「过程组数量是否新增」判断，没有就落 `wait2` 这个 mark——**它必须和 `tool2` 分开**，否则那段十几秒的静止画面会被当成有内容的阶段原速播放。

## 录管理端 + 后端（README 的第二段）

```bash
ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD='...' node record-admin.mjs --out docs/demo-admin
uv run python ../../scripts/demo/make-video.py --dir docs/demo-admin \
    --cover-at users --skip-gif
```

顺序是：提供商列表 → 展开一行看模型清单 → 用户管理 → 按邮箱搜索 → 切到 `/docs` 展开 `/api/chat` → `/health/ready` → `/api/metrics`。

后两屏是浏览器原生渲染的纯文本，一屏只有一行小字，1080p 里也读不出来，所以脚本用 `addStyleTag` 把 `body` 放大 + 换等宽字体。**只改字号和字体，不改内容、不注入任何数据**。

这一段不出 GIF：管理端的画面是表格与表单，动起来信息增量很小，而 GIF 体积会翻三倍。README 里用封面图 + MP4 链接。

## 拍界面截图

```bash
DEMO_EMAIL=demo@example.com DEMO_PASSWORD='...' node screenshots.mjs          # 全部
node screenshots.mjs --only sandbox,context-usage                              # 只重拍几张
```

## 踩过的坑

**1. `channel: 'chrome'` 配 `headless: true` 会把画面裁掉 90px。**
新无头模式下 screencast 只拍到约 810px 高（1440×900 时），剩下 90px 被填成灰色，成片底部一条灰带（就是 IDE 工具栏那么高）。`--window-size` 之类的参数不解决。所以默认**有头**录制——会弹出一个 Chrome 窗口，能看见它在打字；真的没有显示器时再用 `--headless` 并接受灰边。

**2. 1080p 不是靠 `recordVideo.size` 放大的。**
`size` 只能缩放 viewport 画面，放大到 1920 不会长出细节。要真 1080p 就得让 viewport 本身就是 1920×1080（`record.mjs` 的默认值，`--width/--height` 可改）。`deviceScaleFactor` 只影响渲染精度，录像尺寸仍等于 viewport。

**3. 时间轴要量到「脚本动作结束」，不是「文件写完」。**
`context.close()` 之后 Playwright 还要把 webm 收尾，那段时间也会被算进片子，表现是末尾多出十几秒画面不动的尾巴。所以 `end` 这个 mark 在脚本动作结束时打。

**4. `recordVideo` 是「每个 page 一段视频」。**
在同一个 context 里 `newPage()` 新开标签页，录到的是另一个文件，而 `page.video()` 只拿得到第一个 page 的那段——后面那些秒会录成「画面停在上一个页面不动」。所以 `record-admin.mjs` 在 `/docs` → `/health/ready` → `/api/metrics` 之间是**同一个标签页里跳转**。

**5. GIF 必须和 MP4 分开出。**
GitHub 的 Markdown 不支持 `<video>`，只有 GIF 能内联自动播放；而 60 秒的 GIF 会到十几 MB。做法是同一份时间轴出两个取景：GIF 从「沙箱开始跑」截到「上下文弹层结束」，整体再压一档速度，880 宽 9fps，落到 2 MB 以内。

**6. GIF 要裁图，不能整帧缩。**
整帧 1920 缩到 880 时正文只有 7px 高，字全糊成灰带。`GIF_CROP = "920:1080:600:0"` 把会话栏原生裁出来再轻微缩放，正文才有 10px 左右。

**7. GIF 的调色板要单独生成。**
这一屏几乎全是白底 + 灰边，用 ffmpeg 默认的通用 256 色表会出现明显色带和麻点。脚本走两遍：`palettegen`（`stats_mode=diff`）再 `paletteuse`（bayer 抖动），并且只取 128 色——浅色 UI 用不到那么多颜色，少一半颜色体积也小一截。

## 为什么不直接屏幕录制

`recordVideo` 录的是**页面**，不是桌面：不会被通知弹窗、鼠标轨迹、壁纸污染，也不用申请 macOS 的录屏权限；分辨率恒等于 viewport，重新跑一次结果一致。代价是它只拍浏览器内容区——够用，演示的重点本来也不在桌面上。

## 素材是否入库

- `docs/demo/demo.mp4`、`demo.gif`、`cover.png`、`docs/demo-admin/demo.mp4`、`cover.png` 入库（README 要引用）；
- `raw.webm`、`.raw/`、`timeline.json`、`scripts/demo/node_modules/` 不入库（见根目录 `.gitignore`）——它们随时能重录，且只对当次录屏有意义。

## 播放页（GitHub Pages）

README 的封面图链到 **https://ruanbw.github.io/bennett-agent/**，那一页是 `docs/index.html`：
零依赖单页，两段视频都是原生 `<video>` + 章节按钮，浅色/深色自适应。

仓库设置：Pages 走 `master` 分支的 `/docs` 目录（`gh api -X POST repos/<owner>/<repo>/pages`;
换成本仓库时记得改）。**不需要构建**：`docs/` 目录里就是要发布的静态文件，仓库里的
mp4 与封面同时也是 Pages 的资源，一份文件两处用。

### 为什么不能直接链仓库里的 mp4

实测（2026-10，本仓库 `master`）：

| 地址 | 浏览器行为 |
| --- | --- |
| `github.com/…/blob/master/docs/demo/demo.mp4` | 只有「View raw」，**不播放** |
| `raw.githubusercontent.com/…/demo.mp4` | `content-type: application/octet-stream` + `nosniff`，`<video>` 解不出来 |
| `github.com/…/raw/refs/heads/master/docs/demo/demo.mp4` | 302 到上面那条，同样 octet-stream |
| `ruanbw.github.io/bennett-agent/demo/demo.mp4` | `content-type: video/mp4`，**能播** |

另外 GitHub 的 README 会过滤用户写的 `<video>` 标签，所以「写个 video 标签指 raw 链接」
这条路两头都堵死。唯一能内联出播放器的是 **user-attachments**（把文件拖进编辑器上传，
README 里裸链接即可），但它需要网页登录态——`gh` 2.89 还没有 `--attach`。

### 章节秒数从哪来

`make-video.py` 结尾会打印「成片章节（秒）」，`docs/index.html` 里按钮上的
`data-seek` 就照它抄。变速档位一改，这些秒数就变，而手算「几段各多少秒相加」
必然错一格——所以让脚本自己报，别自己算。

## 要不要另传一个视频平台

当前方案（仓库 mp4 + GIF 内联 + Pages 播放页）已经覆盖「先扫一眼」与「看原画」两种意图。
其余选项的实际边界：

| 方式 | 能否页面内播放 | 代价 |
| --- | --- | --- |
| **Pages 播放页（当前）** | **是**（原生 `<video>`） | 站点公开；需在仓库设置里启用 Pages |
| 仓库内 GIF | 是（README 内联、自动循环） | 只有 880 宽、9fps，2 MB |
| 仓库内 MP4 直链 | 否（blob 页只给 View raw） | — |
| user-attachments | 是（README 内联播放器） | 要网页登录上传；免费账号单文件 10 MB |
| Release 附件 | 否（点击下载） | 同为外链，但没有任何播放收益 |
| jsDelivr 镜仓 | 是（返回 `video/mp4`） | 依赖第三方 CDN，且仍然是「跳出去播」 |
| B 站 | 否（只能放链接/封面） | 要账号与过审；国内访问最快、可切清晰度 |

结论：**Pages 播放页 + GIF 内联 + 仓库 mp4 原件**三件套足够，不需要第四个平台。
B 站只在「国内访问速度」成为问题时才值得加，届时把两段 mp4 传上去、在 README 补两个链接即可。
