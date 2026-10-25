#!/usr/bin/env node
/**
 * 重新拍 README 里 `docs/screenshots/*.png`。
 *
 * 截图会随着 UI 一起过期——旧图里的侧边栏标题、模型选择器、底部统计条都改过版，
 * 拿旧图配新功能说明等于对读者撒谎。所以截图也走脚本：能重跑，就不用靠记忆。
 *
 * 拍的都是**真实后端 + 真实模型跑出来的界面**，不是静态样板：
 * 每个场景都真的发一次请求，等这一轮结束再拍。
 *
 * 用法见同目录 README.md。默认输出到 docs/screenshots/。
 */

import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const require = createRequire(import.meta.url);

async function loadPlaywright() {
  const candidates = [
    'playwright',
    process.env.PLAYWRIGHT_MODULE_DIR ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, 'index.mjs') : null,
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

const args = new Map();
for (let i = 2; i < process.argv.length; i += 1) {
  const key = process.argv[i];
  if (!key.startsWith('--')) continue;
  const value = process.argv[i + 1]?.startsWith('--') ? '' : process.argv[++i];
  args.set(key.slice(2), value ?? '');
}

const BASE_URL = args.get('base-url') || 'http://localhost:5173';
const OUT = path.resolve(args.get('out') || 'docs/screenshots');
const EMAIL = args.get('email') || process.env.DEMO_EMAIL;
const PASSWORD = args.get('password') || process.env.DEMO_PASSWORD;
/** 录屏那条会话的标题：沙箱工具轨迹与上下文弹层的截图都取自它。 */
const SANDBOX_TITLE = args.get('sandbox-title') || '部署次数统计与柱状图';

const VIEWPORT = { width: 1440, height: 900 };
/** 只拍某几张（`--only sandbox,context-usage`）：改一张图不该重跑两轮真实模型。 */
const ONLY = new Set((args.get('only') || '').split(',').filter(Boolean));
const want = (name) => ONLY.size === 0 || ONLY.has(name);

async function waitForFirstScreen(page) {
  await page.waitForFunction(
    () =>
      document.querySelector('textarea[aria-label="输入消息"]') !== null ||
      document.querySelector('form input') !== null,
    undefined,
    { timeout: 30_000 },
  );
}

async function login(page) {
  await page.getByLabel('邮箱').fill(EMAIL);
  await page.getByLabel('密码', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: '登录' }).click();
  await page.getByLabel('输入消息').waitFor({ state: 'visible', timeout: 30_000 });
}

/** 等这一轮结束：停止按钮消失且正文 3 秒内不再变化。 */
async function waitForTurnEnd(page, timeoutMs = 300_000) {
  const deadline = Date.now() + timeoutMs;
  let last = '';
  let stableSince = Date.now();
  while (Date.now() < deadline) {
    await page.waitForTimeout(500);
    const text = await page.evaluate(() => document.body.innerText);
    if (text !== last) {
      last = text;
      stableSince = Date.now();
    }
    const running = (await page.getByLabel('停止生成').count()) > 0;
    if (!running && Date.now() - stableSince > 3000) return;
  }
  throw new Error('等这一轮结束超时');
}

async function ask(page, prompt) {
  const composer = page.getByLabel('输入消息');
  await composer.click();
  await composer.fill(prompt);
  await composer.press('Enter');
  await waitForTurnEnd(page);
  await page.waitForTimeout(800);
}

/** 侧边栏可能默认收起（收起态没有会话列表，只有顶栏的图标按钮）。 */
async function ensureSidebar(page) {
  const expand = page.getByLabel('展开侧边栏');
  if ((await expand.count()) > 0) {
    await expand.first().click();
    await page.waitForTimeout(600);
  }
}

/** 新建一条空会话。侧边栏展开时用胶囊按钮，收起时退回顶栏的加号。 */
async function newConversation(page) {
  const pill = page.getByRole('button', { name: '开启新对话' });
  if ((await pill.count()) > 0) {
    await pill.first().click();
  } else {
    await page.getByLabel('新建对话').click();
  }
  await page.waitForTimeout(900);
}

async function shot(page, name) {
  const target = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: target });
  console.log(`  ✓ ${path.relative(process.cwd(), target)}`);
}

async function main() {
  if (!EMAIL || !PASSWORD) {
    throw new Error('缺少账号：用 --email/--password 或环境变量 DEMO_EMAIL/DEMO_PASSWORD');
  }
  await mkdir(OUT, { recursive: true });

  const { chromium } = await loadPlaywright();
  // 与录屏同一个理由：`channel: 'chrome'` + headless 时 screencast 会少 90px，
  // 截图虽然不受影响，但保持同一条路径，出问题时不用怀疑两处配置。
  const browser = await chromium.launch({ channel: 'chrome', headless: false });
  const context = await browser.newContext({ viewport: VIEWPORT, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
  const page = await context.newPage();
  page.setDefaultTimeout(60_000);

  try {
    await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
    await waitForFirstScreen(page);
    if (await page.getByLabel('邮箱').count()) await login(page);
    await ensureSidebar(page);
    await page.waitForTimeout(1500); // 等一次同步：侧边栏的会话列表来自服务端

    // ---- 1. 工具轨迹 + 结果：录屏那条会话，展开后拍「它真的跑了」 ----
    const sandboxConv = page.getByRole('button', { name: SANDBOX_TITLE }).first();
    if (await sandboxConv.count()) {
      await sandboxConv.click();
      await page.waitForTimeout(1500);
      const expand = page.getByRole('button', { name: /已在我的电脑上操作/ });
      if (await expand.count()) {
        await expand.first().click();
        await page.waitForTimeout(900);
      }
      if (want('sandbox')) await shot(page, 'sandbox');
    } else {
      console.warn(`  跳过 sandbox：侧边栏没有「${SANDBOX_TITLE}」这条会话`);
    }

    // ---- 2. 普通问答：表格与结构化输出是它最常被用到的一面 ----
    if (want('chat') || want('context-usage')) {
      await newConversation(page);
      await ask(page, '请对比 SSE 与 WebSocket 在服务端单向推送场景下的取舍，给出一张表。');
      if (want('chat')) await shot(page, 'chat');
    }

    // ---- 3. 上下文用量 ----
    // 用量事件在一轮结束时才发，所以要再问一轮、等它结束，弹层里才有「最新一轮」的数字。
    if (want('context-usage')) {
      await ask(page, '这张表里最容易被忽略的是哪一条？用三句话说完。');
      // 这个按钮的 aria-label 是动态的（`上下文用量：…`），弹层本身才叫「上下文用量详情」
      const usage = page.getByRole('button', { name: /^上下文用量/ }).first();
      await usage.click();
      await page.waitForTimeout(700);
      await shot(page, 'context-usage');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(400);
    }

    // ---- 4. 工具审批：只有 SAFE_WRITE/DANGEROUS 才会出现，`sandbox_reset` 是其中之一 ----
    const composer = page.getByLabel('输入消息');
    if (want('approval')) {
      await newConversation(page);
      await composer.click();
      await composer.fill('把我这台执行环境重置一下，清空 workspace 里的东西。');
      await composer.press('Enter');
    }
    const approval = page.getByText('需要你批准才能继续');
    try {
      if (!want('approval')) throw new Error('skipped');
      await approval.waitFor({ state: 'visible', timeout: 120_000 });
      await page.waitForTimeout(700);
      await shot(page, 'approval');
      // 拍完不批准：这个调用会真的销毁机器，截图不该顺手把环境拆了
      await page.getByLabel('暂不处理这些调用').click();
    } catch {
      console.warn('  跳过 approval：这一轮没有产生审批卡片（模型可能选择不调用重置）');
    }
    await page.waitForTimeout(500);

    // ---- 5. 设置页 ----
    if (want('settings')) {
      await page.getByLabel('用户账户与设置菜单').first().click();
      await page.waitForTimeout(500);
      await page.getByText('设置', { exact: true }).click();
      await page.waitForTimeout(1200);
      await shot(page, 'settings');
    }
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
