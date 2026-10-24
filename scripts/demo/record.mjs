#!/usr/bin/env node
/**
 * 用真实浏览器跑一遍真实链路，并把这一遍录下来。
 *
 * 为什么不用「截几张图再拼」：README 上写「模型能自己跑代码」，最容易被怀疑的
 * 就是这句话。录屏里工具卡片展开后能看到 `/home/sandbox/workspace/*.py`、
 * 真实退出码、每一步的耗时——这些是拼不出来的。
 *
 * 三个刻意的选择：
 *
 * 1. **用系统装好的 Chrome**（`channel: 'chrome'`），不下载 Playwright 自带的
 *    Chromium：只为了录一段视频多 150MB 依赖不值。代价是需要本机装了 Chrome。
 * 2. **录的是页面，不是屏幕**：Playwright 的 `recordVideo` 只拍 viewport，
 *    不会被通知弹窗、鼠标轨迹、桌面壁纸污染，也不用申请 macOS 录屏权限。
 * 3. **静音**：录屏没有音轨（也没有人声脚本），README 里的 GIF/MP4 都是无声的；
 *    信息全部由画面承载。
 *
 * 坑（踩过）：`channel: 'chrome'` + `headless: true` 时，screencast 只拍到约
 * 810px 高，剩下 90px 被填成灰块——新 headless 的窗口仍带着一层不绘制的工具栏。
 * 所以默认**有头**录制（会弹出一个 Chrome 窗口，看得见它在打字），
 * `--headless` 只在真的没有显示器时才用，且要接受底部灰边。
 *
 * 输出：
 *   <out>/raw.webm        原始录屏（VP8，25fps，无音轨）
 *   <out>/timeline.json   各阶段的时间轴，给 make-video.py 剪掉等待用
 *
 * 用法见同目录 README.md。
 */

import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const require = createRequire(import.meta.url);

/**
 * 解析 Playwright：优先用本目录 `npm install` 装的那份，其次用环境变量指定的
 * 模块目录（适合「我已经全局装过，不想再装一份」）。
 */
async function loadPlaywright() {
  const candidates = [
    'playwright',
    process.env.PLAYWRIGHT_MODULE_DIR
      ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, 'index.mjs')
      : null,
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return await import(candidate.startsWith('/') ? candidate : candidate);
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error(
    '找不到 playwright。在 scripts/demo/ 下跑 `npm install`，或设 PLAYWRIGHT_MODULE_DIR ' +
      '指向已装好的 playwright 目录（例如 npx 缓存）。',
  );
}

function parseArgs(argv) {
  const args = {
    baseUrl: 'http://localhost:5173',
    email: process.env.DEMO_EMAIL,
    password: process.env.DEMO_PASSWORD,
    out: 'docs/demo',
    prompt:
      '这周一到周五每天的部署次数是 12、9、15、7、11。' +
      '请真的在你那台机器上跑一遍：算出平均值、中位数、标准差，' +
      '并用 # 号画一张按天的柱状图给我看（画在回答正文里，不要存成图片）。',
    state: '',
    headless: false,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    const take = () => {
      i += 1;
      return value;
    };
    switch (key) {
      case '--base-url': args.baseUrl = take(); break;
      case '--email': args.email = take(); break;
      case '--password': args.password = take(); break;
      case '--out': args.out = take(); break;
      case '--prompt': args.prompt = take(); break;
      case '--state': args.state = take(); break;
      case '--headless': args.headless = true; break;
      case '--help':
        console.log(
          'usage: node record.mjs [--base-url URL] [--email E] [--password P]\n' +
            '                       [--out DIR] [--prompt TEXT] [--state FILE] [--headless]',
        );
        process.exit(0);
        break;
      default:
        throw new Error(`未知参数：${key}`);
    }
  }
  return args;
}

const VIEWPORT = { width: 1440, height: 900 };

const sleep = (page, ms) => page.waitForTimeout(ms);

/**
 * 时间轴：每个 span 是 `[name, 开始时刻]`。取 span 的结束时刻 = 下一个 span 的开始
 * （最后一个用 total）。剪辑脚本据此决定「哪一段加速、哪一段保留 1×」。
 */
function createTimeline() {
  const t0 = Date.now();
  const marks = [{ name: 'blank', at: 0 }];
  return {
    mark(name) {
      marks.push({ name, at: Date.now() - t0 });
    },
    finish() {
      // 结束时刻必须是**脚本动作结束那一刻**，不是写文件那一刻：
      // 关 context 时 Playwright 还要把 webm 从内存里收尾，那几秒会被算进片子里，
      // 剪辑时就会多出一段「画面不动」的尾巴。
      return { startedAt: new Date(t0).toISOString(), marks };
    },
  };
}

/**
 * 等首屏稳定：登录表单和输入框只会出现其中一个。
 *
 * 不能写成「有登录框就登录，否则等输入框」——探测登录态的请求回来之前两者都没有，
 * 那一瞬间的 `count()===0` 会让脚本误以为「已登录」，然后在登录页上等输入框等到超时。
 */
async function waitForFirstScreen(page) {
  await page.waitForFunction(
    () =>
      document.querySelector('textarea[aria-label="输入消息"]') !== null ||
      document.querySelector('form input') !== null,
    undefined,
    { timeout: 30_000 },
  );
}

async function login(page, args) {
  await page.getByLabel('邮箱').fill(args.email);
  await page.getByLabel('密码', { exact: true }).fill(args.password);
  await page.getByRole('button', { name: '登录' }).click();
  await page.getByLabel('输入消息').waitFor({ state: 'visible', timeout: 30_000 });
}

/** 等助手这一轮结束：停止按钮消失 + 正文连续 3 秒没再变长。 */
async function waitForTurnEnd(page, { timeoutMs = 300_000 } = {}) {
  const started = Date.now();
  const composer = page.getByLabel('输入消息');
  let lastText = '';
  let stableSince = Date.now();
  while (Date.now() - started < timeoutMs) {
    await page.waitForTimeout(500);
    const stopVisible = (await page.getByLabel('停止生成').count()) > 0;
    const text = await page.evaluate(() => document.body.innerText);
    if (text !== lastText) {
      lastText = text;
      stableSince = Date.now();
    }
    if (!stopVisible && Date.now() - stableSince > 3000) {
      await composer.waitFor({ state: 'visible' });
      return;
    }
  }
  throw new Error('等这一轮结束超时');
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.email || !args.password) {
    throw new Error('缺少账号：用 --email/--password 或环境变量 DEMO_EMAIL/DEMO_PASSWORD');
  }
  const outDir = path.resolve(args.out);
  await mkdir(outDir, { recursive: true });

  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: 'chrome', headless: args.headless });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    // 时间轴从 context 创建开始量：录屏的第一帧也差不多在这一刻
    recordVideo: { dir: path.join(outDir, '.raw'), size: VIEWPORT },
    ...(args.state ? { storageState: args.state } : {}),
  });

  const timeline = createTimeline();
  const page = await context.newPage();
  page.setDefaultTimeout(60_000);

  try {
    await page.goto(args.baseUrl, { waitUntil: 'domcontentloaded' });
    await waitForFirstScreen(page);
    if (await page.getByLabel('邮箱').count()) {
      await login(page, args);
    }
    const composer = page.getByLabel('输入消息');
    await composer.waitFor({ state: 'visible' });
    // 就绪之后的停留才开始「属于演示」：之前那段是加载与登录，剪辑时整段剪掉
    timeline.mark('ready');

    // 空态建议条会自己淡入，再等一拍让首帧稳定
    await sleep(page, 2200);
    timeline.mark('typing');

    await composer.click();
    await composer.pressSequentially(args.prompt, { delay: 28 });
    await sleep(page, 700);
    timeline.mark('submitted');
    await composer.press('Enter');

    // 「已在我的电脑上操作」= 这一轮第一次真的动了那台机器
    const traceToggle = page.getByRole('button', { name: /已在我的电脑上操作|正在我的电脑上/ });
    await traceToggle.first().waitFor({ state: 'visible', timeout: 240_000 });
    timeline.mark('tool');
    await sleep(page, 900);

    await waitForTurnEnd(page);
    timeline.mark('answered');
    await sleep(page, 1200);

    // 展开工具轨迹：这是「真的跑了」最硬的证据（脚本路径、退出码、每步耗时）
    const expand = page.getByRole('button', { name: /已在我的电脑上操作/ });
    if (await expand.count()) {
      await expand.first().click();
      await sleep(page, 900);
      timeline.mark('expanded');
    }
    await sleep(page, 2600);
    timeline.mark('end');
  } finally {
    const video = page.video();
    await context.close(); // 关 context 才会把 webm 落盘
    await browser.close();

    if (video) {
      const raw = await video.path();
      await rename(raw, path.join(outDir, 'raw.webm')).catch(async () => {
        const { copyFile } = await import('node:fs/promises');
        await copyFile(raw, path.join(outDir, 'raw.webm'));
      });
    }
    const result = timeline.finish();
    await writeFile(
      path.join(outDir, 'timeline.json'),
      `${JSON.stringify({ ...result, viewport: VIEWPORT, prompt: args.prompt }, null, 2)}\n`,
      'utf8',
    );
    console.log(`录制完成 → ${outDir}/raw.webm`);
    for (const mark of result.marks) console.log(`  ${(mark.at / 1000).toFixed(1)}s ${mark.name}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
