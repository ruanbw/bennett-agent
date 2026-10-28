/**
 * 「我的电脑」展示模型与面板结构的契约。
 *
 * 这里锁的三类东西都是「改样式/改分支时最容易丢、但直接决定用户能不能看懂」的：
 *
 * 1. **状态优先级**：探测失败不能沿用上一次的成功状态（否则用户会去等一个不存在的机器）；
 * 2. **未知状态与「没有机器」必须区分**：后端新增一档时，一台真实存在的机器不能被显示
 *    成「从未创建」；
 * 3. **产物必须可下载、且过期链接不能点**：预签名 URL 有时效，点开才发现 403 是最差的
 *    体验。
 *
 * 面板内容单独测 `SandboxPanelContent`：它挂在 Base UI 的 Portal 上，而
 * `renderToStaticMarkup` 不渲染 portal（仓库跑在 node 环境、没有 jsdom）。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SandboxPanelContent, SandboxStatus } from './SandboxStatus';
import { mergeSandboxProbe } from './use-sandbox-status';
import {
  SANDBOX_LABEL,
  artifactExpired,
  artifactsExpired,
  describeArtifactCount,
  describeArtifactExpiry,
  describeSandboxStatus,
} from './view';
import type { SandboxArtifact } from '../../lib/sandbox';

const NOW = Date.parse('2026-10-28T10:00:00Z');

const artifact = (overrides: Partial<SandboxArtifact> = {}): SandboxArtifact => ({
  name: 'workspace.tar.gz',
  size_bytes: 12_345,
  download_url: 'https://storage.example/sbx/workspace.tar.gz?sig=x',
  expires_at: new Date(NOW + 10 * 60_000).toISOString(),
  ...overrides,
});

const status = (state: string) => ({ state, expires_at: null, artifact_uri: null });

/** 渲染面板内容（弹层里的那一块）。 */
function renderPanel({
  view = describeSandboxStatus(status('ready')),
  artifacts = [] as SandboxArtifact[],
  loading = false,
  onRefresh,
}: {
  view?: ReturnType<typeof describeSandboxStatus>;
  artifacts?: SandboxArtifact[];
  loading?: boolean;
  onRefresh?: () => void;
} = {}) {
  return renderToStaticMarkup(
    createElement(SandboxPanelContent, { view, artifacts, now: NOW, loading, onRefresh })
  );
}

describe('describeSandboxStatus：七态 + 未知', () => {
  it('已就绪 / 正在启动 / 已暂停 / 正在回收 / 已回收 / 启动失败 / 回收失败都有中文', () => {
    expect(describeSandboxStatus(status('ready')).stateLabel).toBe('已就绪');
    expect(describeSandboxStatus(status('provisioning')).stateLabel).toBe('正在启动');
    expect(describeSandboxStatus(status('paused')).stateLabel).toBe('已暂停');
    expect(describeSandboxStatus(status('destroying')).stateLabel).toBe('正在回收');
    expect(describeSandboxStatus(status('destroyed')).stateLabel).toBe('已回收');
    expect(describeSandboxStatus(status('failed')).stateLabel).toBe('启动失败');
    expect(describeSandboxStatus(status('destroy_failed')).stateLabel).toBe('回收失败');
  });

  it('过渡态才呼吸（启动中、回收中），其余静止', () => {
    expect(describeSandboxStatus(status('provisioning')).pulsing).toBe(true);
    expect(describeSandboxStatus(status('destroying')).pulsing).toBe(true);
    expect(describeSandboxStatus(status('ready')).pulsing).toBe(false);
    expect(describeSandboxStatus(status('destroyed')).pulsing).toBe(false);
  });

  it('色调区分正常 / 注意 / 出错', () => {
    expect(describeSandboxStatus(status('ready')).tone).toBe('ok');
    expect(describeSandboxStatus(status('provisioning')).tone).toBe('busy');
    expect(describeSandboxStatus(status('paused')).tone).toBe('warning');
    expect(describeSandboxStatus(status('destroy_failed')).tone).toBe('warning');
    expect(describeSandboxStatus(status('failed')).tone).toBe('error');
    expect(describeSandboxStatus(status('destroyed')).tone).toBe('muted');
  });

  it('未知状态如实带出原始值，绝不显示成「从未创建」', () => {
    const view = describeSandboxStatus(status('migrating'));

    expect(view.stateLabel).toContain('migrating');
    expect(view.stateLabel).toContain('状态未知');
    // 「还没有启动」是「没有机器」的说法，不能拿来描述一台存在的机器
    expect(view.stateLabel).not.toBe('还没有启动');
  });

  it('null（404 或还没建）说「还没有启动」，不说「已回收」', () => {
    const view = describeSandboxStatus(null);

    expect(view.stateLabel).toBe('还没有启动');
    expect(view.stateLabel).not.toBe('已回收');
    expect(view.description).toContain('自动创建');
  });

  it('探测失败优先于任何状态：不能挂着上一轮的「已就绪」骗人', () => {
    const view = describeSandboxStatus(status('ready'), true);

    expect(view.stateLabel).toBe('暂时查不到');
    expect(view.tone).toBe('warning');
    expect(view.stateLabel).not.toBe('已就绪');
  });

  it('名字统一叫「我的电脑」，不是「本会话的电脑」（机器跨会话共用）', () => {
    expect(SANDBOX_LABEL).toBe('我的电脑');
    expect(describeSandboxStatus(status('ready')).label).toBe('我的电脑');
    expect(describeSandboxStatus(null).label).toBe('我的电脑');
    expect(describeSandboxStatus(null).description).not.toContain('本会话');
  });

  it('标题带上状态短语，用户不必点开才知道机器怎么样', () => {
    expect(describeSandboxStatus(status('ready')).title).toBe('我的电脑 · 已就绪');
    expect(describeSandboxStatus(status('failed')).title).toBe('我的电脑 · 启动失败');
  });
});

describe('describeArtifactCount', () => {
  it('有产物报数量，没有说「暂无」', () => {
    expect(describeArtifactCount([artifact(), artifact()])).toBe('2 个文件');
    expect(describeArtifactCount([])).toBe('暂无文件');
  });
});

describe('describeArtifactExpiry', () => {
  it('按剩余时间说清还有多久失效', () => {
    expect(describeArtifactExpiry(new Date(NOW + 5 * 60_000).toISOString(), NOW)).toBe(
      '下载链接 5 分钟后失效'
    );
    expect(describeArtifactExpiry(new Date(NOW + 90 * 60_000).toISOString(), NOW)).toBe(
      '下载链接 1 小时后失效'
    );
    expect(describeArtifactExpiry(new Date(NOW + 30_000).toISOString(), NOW)).toBe(
      '下载链接 1 分钟内失效'
    );
  });

  it('已过期时直接说「失效，请刷新」，而不是显示负数分钟', () => {
    const text = describeArtifactExpiry(new Date(NOW - 60_000).toISOString(), NOW);

    expect(text).toContain('已失效');
    expect(text).toContain('刷新');
  });

  it('没有时刻 / 时刻解析不了时不编一句话出来', () => {
    expect(describeArtifactExpiry('', NOW)).toBe('');
    expect(describeArtifactExpiry('not-a-date', NOW)).toBe('');
  });
});

describe('artifactExpired / artifactsExpired', () => {
  it('单个链接按时刻判定', () => {
    expect(artifactExpired(new Date(NOW + 1000).toISOString(), NOW)).toBe(false);
    expect(artifactExpired(new Date(NOW - 1000).toISOString(), NOW)).toBe(true);
    expect(artifactExpired('', NOW)).toBe(false);
  });

  it('全部过期才算整块过期：还有能下的就不该说全失效', () => {
    const expired = artifact({ expires_at: new Date(NOW - 60_000).toISOString() });
    const live = artifact({ name: 'b.tar', expires_at: new Date(NOW + 60_000).toISOString() });

    expect(artifactsExpired([expired, live], NOW)).toBe(false);
    expect(artifactsExpired([expired], NOW)).toBe(true);
    // 空列表没有可点的东西，谈不上过期
    expect(artifactsExpired([], NOW)).toBe(false);
  });
});

describe('SandboxStatus 触发器', () => {
  it('有可读名称（不是只有一个图标），且说明当前状态', () => {
    const html = renderToStaticMarkup(
      createElement(SandboxStatus, {
        status: status('ready'),
        artifacts: [],
        failed: false,
      })
    );

    expect(html).toContain('aria-label=');
    expect(html).toContain('我的电脑');
    expect(html).toContain('已就绪');
  });

  it('探测失败时触发器说「暂时查不到」', () => {
    const html = renderToStaticMarkup(
      createElement(SandboxStatus, { status: status('ready'), artifacts: [], failed: true })
    );

    expect(html).toContain('暂时查不到');
  });
});

describe('SandboxPanelContent 面板结构', () => {
  it('面板标题写明状态，并列出产物与大小', () => {
    const html = renderPanel({ artifacts: [artifact()] });

    expect(html).toContain('我的电脑 · 已就绪');
    expect(html).toContain('workspace.tar.gz');
    // 12_345 字节 = 12.1 KB
    expect(html).toContain('12.1 KB');
    expect(html).toContain('1 个文件');
  });

  it('每个产物都有下载链接，指向上游预签名 URL（前端不拼路径）', () => {
    const html = renderPanel({ artifacts: [artifact()] });

    // 直接指给浏览器，不在前端做二次拼装
    expect(html).toContain('https://storage.example/sbx/workspace.tar.gz?sig=x');
    expect(html).toContain('download=');
    expect(html).toContain('aria-label="下载 workspace.tar.gz"');
  });

  it('外链带 rel=noopener noreferrer（目标域不是本站）', () => {
    expect(renderPanel({ artifacts: [artifact()] })).toContain('rel="noopener noreferrer"');
  });

  it('没有产物时说清「还没有」，而不是给一张空表', () => {
    const html = renderPanel({ artifacts: [] });

    expect(html).toContain('暂无文件');
    expect(html).toContain('还没有产出文件');
  });

  it('过期链接不让点（aria-disabled + 指针事件关闭），并明说已失效', () => {
    const html = renderPanel({
      artifacts: [artifact({ expires_at: new Date(NOW - 60_000).toISOString() })],
    });

    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain('pointer-events-none');
    expect(html).toContain('链接已失效');
  });

  it('探测失败时面板说「暂时查不到」，不显示「已就绪」', () => {
    const html = renderPanel({ view: describeSandboxStatus(status('ready'), true) });

    expect(html).toContain('暂时查不到');
    expect(html).not.toContain('我的电脑 · 已就绪');
  });

  it('有一个可点的刷新入口（产物链接过期后的唯一恢复方式）', () => {
    expect(renderPanel({ artifacts: [artifact()], onRefresh: vi.fn() })).toContain('刷新');
  });

  it('说清机器跨会话共用、闲置会回收（这是用户最需要知道的两条）', () => {
    const html = renderPanel();

    expect(html).toContain('所有对话之间共用');
    expect(html).toContain('自动回收');
  });

  it('多次产物时数量与列表条数一致', () => {
    const html = renderPanel({
      artifacts: [
        artifact(),
        artifact({ name: 'report.csv', download_url: 'https://x/report.csv', size_bytes: 2048 }),
      ],
    });

    expect(html).toContain('2 个文件');
    expect(html.match(/aria-label="下载 /g)).toHaveLength(2);
  });
});

describe('mergeSandboxProbe：两条请求互相降级', () => {
  const ready = { state: 'ready', expires_at: null, artifact_uri: null };
  const artifactList = [artifact()];

  it('两条都成功：各用各的', () => {
    const merged = mergeSandboxProbe(
      { ok: true, value: ready },
      { ok: true, value: { state: 'ready', artifacts: artifactList } }
    );

    expect(merged.status).toEqual(ready);
    expect(merged.artifacts?.artifacts).toHaveLength(1);
    expect(merged.failed).toBe(false);
  });

  it('/status 挂了但 /artifacts 成功：用后者的 state 兜底，不假装「还没有启动」', () => {
    const merged = mergeSandboxProbe(
      { ok: false },
      { ok: true, value: { state: 'destroyed', artifacts: artifactList } }
    );

    // 机器已回收、文件还能下载——退化成 null 会显示成「从没建过」，用户以为全没了
    expect(merged.status?.state).toBe('destroyed');
    expect(merged.artifacts?.artifacts).toHaveLength(1);
    // 一条成功就不算探测失败
    expect(merged.failed).toBe(false);
  });

  it('/artifacts 挂了但 /status 成功：机器状态照常显示，产物空', () => {
    const merged = mergeSandboxProbe({ ok: true, value: ready }, { ok: false });

    expect(merged.status).toEqual(ready);
    expect(merged.artifacts).toBeNull();
    expect(merged.failed).toBe(false);
  });

  it('两条都失败才算 failed', () => {
    const merged = mergeSandboxProbe({ ok: false }, { ok: false });

    expect(merged.status).toBeNull();
    expect(merged.failed).toBe(true);
  });

  it('/artifacts 成功但没有 state（从来没有过机器）时不编一个状态出来', () => {
    const merged = mergeSandboxProbe(
      { ok: false },
      { ok: true, value: { state: null, artifacts: [] } }
    );

    expect(merged.status).toBeNull();
    expect(merged.failed).toBe(false);
  });

  it('产物列表只来自 /artifacts（/status 的 artifact_uri 是对象键，不是可下载地址）', () => {
    const withUri = { state: 'destroyed', expires_at: null, artifact_uri: 'sbx_abc/workspace.tar.gz' };
    const merged = mergeSandboxProbe({ ok: true, value: withUri }, { ok: true, value: { state: 'destroyed', artifacts: [] } });

    // 不能拿 artifact_uri 拼下载地址：预签名必须由服务端签
    expect(merged.artifacts?.artifacts).toEqual([]);
  });
});
