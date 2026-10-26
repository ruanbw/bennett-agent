#!/usr/bin/env node
/**
 * 录管理后台 + 后端接口面的一段短演示（README 里的第二段视频）。
 *
 * 与 `record.mjs` 的分工：那段讲「用户端能做什么」（对话、沙箱真跑代码、上下文预算），
 * 这段讲「谁在管它」（提供商与模型、用户与角色）以及「接口长什么样」（OpenAPI 文档、
 * 就绪探针）。两段合起来才是这个仓库的三块：用户端 / 管理端 / 后端。
 *
 * 与 record.mjs 相同的三条约定：系统 Chrome、只录页面、静音。
 *
 * 输出：
 *   <out>/raw.webm        原始录屏
 *   <out>/timeline.json   时间轴（剪辑用）
 *
 * 用法：
 *   ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD='...' node record-admin.mjs --out docs/demo-admin
 */

import path from 'node:path';
import process from 'node:process';
import { mkdir, writeFile, rename, copyFile } from 'node:fs/promises';

async function loadPlaywright() {
  const candidates = [
    'playwright',
    process.env.PLAYWRIGHT_MODULE_DIR
      ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, 'index.mjs')
      : null,
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return await import(candidate);
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error('找不到 playwright：在 scripts/demo/ 下 npm install，或设 PLAYWRIGHT_MODULE_DIR');
}

function parseArgs(argv) {
  const args = {
    baseUrl: 'http://localhost:5174',
    apiUrl: 'http://127.0.0.1:8000',
    email: process.env.ADMIN_EMAIL || process.env.DEMO_EMAIL,
    password: process.env.ADMIN_PASSWORD || process.env.DEMO_PASSWORD,
    out: 'docs/demo-admin',
    state: '',
    headless: false,
    width: 1920,
    height: 1080,
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
      case '--api-url': args.apiUrl = take(); break;
      case '--email': args.email = take(); break;
      case '--password': args.password = take(); break;
      case '--out': args.out = take(); break;
      case '--state': args.state = take(); break;
      case '--width': args.width = Number(take()); break;
      case '--height': args.height = Number(take()); break;
      case '--headless': args.headless = true; break;
      case '--help':
        console.log(
          'usage: node record-admin.mjs [--base-url URL] [--api-url URL] [--email E]\n' +
            '                             [--password P] [--out DIR] [--state FILE]\n' +
            '                             [--width 1920] [--height 1080] [--headless]',
        );
        process.exit(0);
        break;
      default:
        throw new Error(`未知参数：${key}`);
    }
  }
  args.viewport = { width: args.width, height: args.height };
  return args;
}

const sleep = (page, ms) => page.waitForTimeout(ms);

function createTimeline() {
  const t0 = Date.now();
  const marks = [{ name: 'blank', at: 0 }];
  return {
    mark(name) {
      marks.push({ name, at: Date.now() - t0 });
    },
    finish() {
      return { startedAt: new Date(t0).toISOString(), marks };
    },
  };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.email || !args.password) {
    throw new Error('缺少账号：用 --email/--password 或环境变量 ADMIN_EMAIL/ADMIN_PASSWORD');
  }
  const outDir = path.resolve(args.out);
  await mkdir(outDir, { recursive: true });

  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: 'chrome', headless: args.headless });
  const context = await browser.newContext({
    viewport: args.viewport,
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    recordVideo: { dir: path.join(outDir, '.raw'), size: args.viewport },
    ...(args.state ? { storageState: args.state } : {}),
  });

  const timeline = createTimeline();
  const page = await context.newPage();
  page.setDefaultTimeout(60_000);

  try {
    // ── 管理后台 ────────────────────────────────────────────────
    await page.goto(args.baseUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    if (await page.getByLabel('邮箱').count()) {
      await page.getByLabel('邮箱').fill(args.email);
      await page.getByLabel('密码', { exact: true }).fill(args.password);
      await page.getByRole('button', { name: '登录' }).click();
    }
    await page.getByRole('heading', { name: '模型提供商' }).waitFor({ timeout: 30_000 });
    await sleep(page, 2200);
    timeline.mark('providers');

    // 展开一行：模型清单、启用状态、默认模型都在折叠区里
    const expand = page.getByRole('button', { name: /展开/ }).first();
    if (await expand.count()) {
      await expand.click();
      await sleep(page, 2600);
      timeline.mark('models');
      await sleep(page, 2600);
    }

    // 用户管理：角色、启用状态、按邮箱搜索
    await page.getByRole('link', { name: '用户管理' }).click();
    await sleep(page, 2400);
    timeline.mark('users');
    await page.getByLabel('按邮箱搜索用户').fill('demo@');
    await page.getByRole('button', { name: '搜索' }).click();
    await sleep(page, 2600);
    timeline.mark('users-search');
    await sleep(page, 2200);

    // ── 后端 ────────────────────────────────────────────────────
    //
    // 必须**在同一个标签页里**跳转：Playwright 的 recordVideo 是「每个 page 一段视频」，
    // 新开标签页录到的是另一个文件，`page.video()` 只拿得到第一个 page 的那段——
    // 后面那 20 秒会录成「画面停在管理后台不动」。
    await page.goto(`${args.apiUrl}/docs`, { waitUntil: 'domcontentloaded' });
    // Swagger UI 靠自己的脚本渲染，等它真的把接口列表填出来再打点
    await page
      .waitForFunction(() => document.querySelectorAll('.opblock').length > 0, undefined, {
        timeout: 60_000,
      })
      .catch(() => {});
    await sleep(page, 2600);
    timeline.mark('docs');

    // 展开核心那条：`/api/chat`（SSE 流式对话）的请求体 schema 最能说明这个后端在做什么
    const chatBlock = page
      .locator('.opblock')
      // 精确匹配：`hasText: '/api/chat'` 会先命中 `/api/chat/title`（子串匹配）
      .filter({ has: page.locator('.opblock-summary-path', { hasText: /^\/api\/chat$/ }) })
      .first();
    const block = (await chatBlock.count()) ? chatBlock : page.locator('.opblock').first();
    await block.locator('.opblock-summary').click();
    await sleep(page, 2800);
    timeline.mark('docs-open');
    await sleep(page, 2600);

    // 最后是两屏「后端自己报的状态」：
    //   /health/ready  —— 三个真实依赖（库 / Redis / 对象存储）逐项 ok
    //   /api/metrics   —— Prometheus 文本格式的对话与 HTTP 指标
    // 这两个页面是浏览器原生渲染的纯文本，只有一行小字；录屏里看不清，所以用 CSS
    // 把它放大（不改内容、不注入数据，改的只是字号与等宽字体）。
    await page.goto(`${args.apiUrl}/health/ready`, { waitUntil: 'domcontentloaded' });
    await page.addStyleTag({
      content:
        'body{zoom:2.2;padding:36px 56px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;' +
        'line-height:1.6;white-space:pre-wrap}',
    });
    await sleep(page, 2800);
    timeline.mark('health');
    await sleep(page, 3400);

    await page.goto(`${args.apiUrl}/api/metrics`, { waitUntil: 'domcontentloaded' });
    await page.addStyleTag({
      content:
        'body{zoom:1.55;padding:24px 48px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;' +
        'line-height:1.45;white-space:pre-wrap}',
    });
    await sleep(page, 2600);
    // 滚到直方图那一段：`chat_first_event_seconds` 与 `chat_streams_total`
    // 是本仓自己加的指标，比通用的 HTTP 耗时更能说明问题
    await page.evaluate(() => {
      const line = [...document.body.innerText.split('\n')].findIndex((row) =>
        row.startsWith('chat_first_event_seconds'),
      );
      if (line > 0) window.scrollTo(0, Math.max(0, line * 22 - 120));
    });
    await sleep(page, 3000);
    timeline.mark('metrics');
    await sleep(page, 3600);
    timeline.mark('end');
  } finally {
    const video = page.video();
    await context.close();
    await browser.close();

    if (video) {
      const raw = await video.path();
      await rename(raw, path.join(outDir, 'raw.webm')).catch(async () => {
        await copyFile(raw, path.join(outDir, 'raw.webm'));
      });
    }
    const result = timeline.finish();
    await writeFile(
      path.join(outDir, 'timeline.json'),
      `${JSON.stringify({ ...result, viewport: args.viewport }, null, 2)}\n`,
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
