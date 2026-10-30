#!/usr/bin/env node
// 文档质量检查：frontmatter、相对链接、标题层级、占位符、代码围栏。
// 只用 node: 内置模块，零依赖：npm ci 之前也能跑，也不依赖 vitepress 构建。
// 文件清单从 docs/ 实际内容遍历得出，新页面（如 docs/reference/）没落盘时不参与检查，不需要改脚本。
// 每条问题输出为「文件:行号  说明」，有问题时退出码为 1。
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const DOCS_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
// .vitepress 是构建配置与主题，public 是原样拷贝的静态资源，都不是正文页面。
const SKIP_DIRS = new Set(['.vitepress', 'node_modules', 'public']);
const PLACEHOLDER_PATTERNS = [
  { pattern: /\bTODO\b/, name: 'TODO' },
  { pattern: /\bTBD\b/, name: 'TBD' },
  { pattern: /\bFIXME\b/, name: 'FIXME' },
  { pattern: /待补充/, name: '待补充' },
];
const DESCRIPTION_MIN = 10;
const DESCRIPTION_MAX = 60;
const LINK_RE = /\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"[^"]*")?\s*\)/g;

const problems = [];

function report(file, line, message) {
  problems.push({ file, line, message });
}

function collectMarkdown(dir) {
  const files = [];
  const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      files.push(...collectMarkdown(full));
    } else if (entry.isFile() && entry.name.endsWith('.md')) {
      files.push(full);
    }
  }
  return files;
}

function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

// 只解析 frontmatter 顶层的 key: value；hero: 这类嵌套块缩进行跳过。
function parseFrontmatter(lines, file) {
  if (lines[0]?.trim() !== '---') return null;
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  if (end === -1) {
    report(file, 1, 'frontmatter 未闭合：缺少结束的 ---');
    return null;
  }
  const data = {};
  for (let i = 1; i < end; i += 1) {
    const match = /^([A-Za-z][\w-]*):\s*(.*)$/.exec(lines[i]);
    if (!match) continue;
    data[match[1]] = match[2].trim().replace(/^['"]|['"]$/g, '');
  }
  return { data, bodyStart: end + 1 };
}

// 返回逐行是否位于代码围栏内（含围栏行本身），顺带报未闭合的围栏。
function scanCodeFences(lines, bodyStart, file) {
  const inFence = new Array(lines.length).fill(false);
  let open = null;
  for (let i = bodyStart; i < lines.length; i += 1) {
    const fence = /^\s{0,3}(`{3,}|~{3,})/.exec(lines[i]);
    if (!fence) {
      if (open) inFence[i] = true;
      continue;
    }
    inFence[i] = true;
    if (!open) {
      open = { line: i + 1, marker: fence[1][0] };
    } else if (fence[1][0] === open.marker) {
      open = null;
    }
  }
  if (open) {
    report(file, open.line, `代码围栏未闭合：缺少收尾的 ${open.marker.repeat(3)}`);
  }
  return inFence;
}

function checkHeadings(lines, inFence, bodyStart, file) {
  let previousLevel = 0;
  for (let i = bodyStart; i < lines.length; i += 1) {
    if (inFence[i]) continue;
    const heading = /^(#{1,6})\s+\S/.exec(lines[i]);
    if (!heading) continue;
    const level = heading[1].length;
    if (previousLevel > 0 && level > previousLevel + 1) {
      report(file, i + 1, `标题层级跳跃：H${previousLevel} 之后直接出现 H${level}`);
    }
    previousLevel = level;
  }
}

function checkLinks(lines, inFence, bodyStart, file) {
  const dir = dirname(file);
  for (let i = bodyStart; i < lines.length; i += 1) {
    if (inFence[i]) continue;
    // 行内代码里的示例链接不算链接。
    const text = lines[i].replace(/`[^`]*`/g, '');
    for (const match of text.matchAll(LINK_RE)) {
      let target = match[1];
      if (target.startsWith('<') && target.endsWith('>')) target = target.slice(1, -1);
      if (!target || target.startsWith('#')) continue;
      if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(target) || target.startsWith('//')) continue;
      const pathPart = target.split('#')[0];
      // 只静态校验指向 .md 的链接；锚点和无扩展名的站点路径交给 vitepress build。
      if (!pathPart.endsWith('.md')) continue;
      let decoded = pathPart;
      try {
        decoded = decodeURI(pathPart);
      } catch {
        // 非法转义按原文处理，路径不存在时同样会报出来。
      }
      const absolute = decoded.startsWith('/') ? join(DOCS_ROOT, decoded) : resolve(dir, decoded);
      if (!isFile(absolute)) {
        report(file, i + 1, `链接目标不存在：${target}`);
      }
    }
  }
}

function checkPlaceholders(lines, inFence, bodyStart, file) {
  for (let i = bodyStart; i < lines.length; i += 1) {
    if (inFence[i]) continue;
    for (const { pattern, name } of PLACEHOLDER_PATTERNS) {
      if (pattern.test(lines[i])) {
        report(file, i + 1, `出现占位内容：${name}`);
      }
    }
  }
}

function checkFrontmatter(frontmatter, file) {
  if (!frontmatter) {
    report(file, 1, '缺少 YAML frontmatter');
    return 0;
  }
  const { data, bodyStart } = frontmatter;
  if (!data.title) report(file, 1, 'frontmatter 缺少 title');
  if (data.layout === 'home') return bodyStart;
  const description = data.description;
  if (!description) {
    report(file, 1, 'frontmatter 缺少 description');
    return bodyStart;
  }
  // 中文字符按 1 个算，空白不计入长度。
  const length = [...description].filter((char) => !/\s/.test(char)).length;
  if (length < DESCRIPTION_MIN || length > DESCRIPTION_MAX) {
    report(file, 1, `description 长度 ${length} 字，超出 ${DESCRIPTION_MIN}-${DESCRIPTION_MAX} 字`);
  }
  return bodyStart;
}

function checkFile(file) {
  const lines = readFileSync(file, 'utf8').replace(/^\uFEFF/, '').split(/\r?\n/);
  const frontmatter = parseFrontmatter(lines, file);
  const bodyStart = checkFrontmatter(frontmatter, file);
  const inFence = scanCodeFences(lines, bodyStart, file);
  checkHeadings(lines, inFence, bodyStart, file);
  checkLinks(lines, inFence, bodyStart, file);
  checkPlaceholders(lines, inFence, bodyStart, file);
}

const files = collectMarkdown(DOCS_ROOT);
for (const file of files) checkFile(file);

const prefix = process.cwd() + '/';
for (const { file, line, message } of problems) {
  const path = file.startsWith(prefix) ? relative(process.cwd(), file) : file;
  console.log(`${path}:${line}  ${message}`);
}
if (problems.length > 0) {
  console.log(`\n扫描 ${files.length} 个 Markdown 文件，发现 ${problems.length} 个问题。`);
  process.exitCode = 1;
} else {
  console.log(`扫描 ${files.length} 个 Markdown 文件，未发现问题。`);
}
