import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SettingsPanel } from './SettingsDialog';

/**
 * 设置面板的渲染契约。
 *
 * 只渲染 `SettingsPanel`：`SettingsDialog` 用的是 base-ui 的 Portal 弹层，node 环境里
 * 没有 document。面板本身是同步渲染的纯视图（两个 hook 只在 effect 里读数据），
 * 因此在 SSR 下可以直接断言它输出的结构。
 */
const render = (props: Partial<Parameters<typeof SettingsPanel>[0]> = {}) =>
  renderToStaticMarkup(
    createElement(SettingsPanel, { userEmail: 'user@example.com', ...props })
  );

describe('SettingsPanel', () => {
  it('区块标题用 h3，紧跟对话框标题（h2）之后，读屏的标题导航不出现断层', () => {
    const html = render();

    expect(html).toContain('<h3');
    expect(html).not.toContain('<h4');
  });

  it('五个区块都在：账号、外观、修改密码、本机数据、关于系统', () => {
    const html = render();

    for (const heading of ['当前账号', '外观与主题', '修改密码', '本机数据', '关于系统']) {
      expect(html).toContain(heading);
    }
  });

  it('能力清单拿不到时显示「未知」，而不是谎报「未启用」', () => {
    const html = render({ capabilities: null });

    expect(html).toContain('未知');
  });

  it('拿到能力快照时显示真实状态', () => {
    const html = render({
      capabilities: { web_search: true, tools: ['ask_user_clarification', 'tavily_search'] },
    });

    expect(html).toContain('已启用');
    expect(html).toContain('2 个');
  });

  it('角色徽标按中文名显示，未知角色不猜', () => {
    expect(render({ userRole: 'admin' })).toContain('管理员');
    // 后端将来新增一档时只显示邮箱，不把内部字符串甩给用户
    expect(render({ userRole: 'future-role' })).not.toContain('future-role');
  });

  it('没有邮箱时显示占位而不是空白', () => {
    const html = render({ userEmail: null });

    expect(html).toContain('未登录');
  });
});

describe('运行时能力：未知时的「重新探测」入口', () => {
  it('能力探测失败时说明「未知不代表没配」，并给一个就地重探的按钮', () => {
    const onRefresh = vi.fn();
    const html = renderToStaticMarkup(
      createElement(SettingsPanel, {
        userEmail: 'user@example.com',
        capabilities: null,
        capabilitiesFailed: true,
        onRefreshCapabilities: onRefresh,
      }),
    );

    expect(html).toContain('能力探测失败');
    expect(html).toContain('不代表服务端没配');
    expect(html).toContain('重新探测');
  });

  it('拿到能力清单后不再显示这句提示', () => {
    const html = renderToStaticMarkup(
      createElement(SettingsPanel, {
        userEmail: 'user@example.com',
        capabilities: { web_search: true, tools: ['tavily_search'] },
        onRefreshCapabilities: vi.fn(),
      }),
    );

    expect(html).toContain('已启用');
    expect(html).not.toContain('重新探测');
  });

  it('调用方没提供重探回调时不渲染点不动的按钮', () => {
    const html = renderToStaticMarkup(
      createElement(SettingsPanel, { userEmail: 'user@example.com', capabilities: null, capabilitiesFailed: true }),
    );

    // 没有可点的动作就不留一句干瞪眼的提示
    expect(html).not.toContain('重新探测');
    expect(html).not.toContain('还没有拿到能力清单');
  });
});

describe('工作步骤展示与收起时机', () => {
  /** 只截「工作步骤展示」那一组卡片：改密表单也有 aria-pressed 按钮，不能全局数。 */
  const detailGroup = (html: string) =>
    html.slice(html.indexOf('aria-label="工作步骤展示"'), html.indexOf('aria-label="工作步骤收起时机"'));

  /**
   * 终点取紧随其后的「性能与用量」区块，而不是「修改密码」：后者之间还夹着一组
   * 同样用 aria-pressed 的两张卡片，截到最末会把它们一起数进来。
   */
  const timingGroup = (html: string) => {
    const start = html.indexOf('aria-label="工作步骤收起时机"');
    const end = html.indexOf('aria-label="性能与用量"', start);
    return html.slice(start, end === -1 ? undefined : end);
  };

  /** 「性能与用量」两档：终点取「修改密码」（那一块也有 aria-pressed 的显隐按钮）。 */
  const performanceGroup = (html: string) => {
    const start = html.indexOf('aria-label="性能与用量"');
    const end = html.indexOf('修改密码', start);
    return html.slice(start, end === -1 ? undefined : end);
  };

  it('四档都在，且默认选中「标准」', () => {
    const group = detailGroup(render());

    for (const label of ['简洁', '标准', '详细', '完全展开']) {
      expect(group).toContain(label);
    }
    // 四张卡片，恰好一个被选中
    expect(group.match(/aria-pressed="true"/g)).toHaveLength(1);
    expect(group.match(/aria-pressed="false"/g)).toHaveLength(3);
  });

  it('收起时机两档独立成组，默认「回答结束后」', () => {
    const group = timingGroup(render());

    expect(group).toContain('回答结束后');
    expect(group).toContain('下次有新消息时');
    expect(group.match(/aria-pressed="true"/g)).toHaveLength(1);
    expect(group.match(/aria-pressed="false"/g)).toHaveLength(1);
  });

  it('性能与用量两档独立成组，默认「详细」', () => {
    const group = performanceGroup(render());

    expect(group).toContain('简洁');
    expect(group).toContain('详细');
    // 两档，恰好一个被选中
    expect(group.match(/aria-pressed="true"/g)).toHaveLength(1);
    expect(group.match(/aria-pressed="false"/g)).toHaveLength(1);
  });

  it('性能与用量说明它不影响记账与上下文圆环', () => {
    const group = performanceGroup(render());
    expect(group).toContain('上下文占用环始终显示');
  });

  it('两块分组各自带 2×2 / 1×2 的网格，不是一个 6 项的列表', () => {
    const html = render();
    // 组头标题各自独立：读屏按标题导航时能分清「露多少」与「何时收」
    expect(html).toContain('工作步骤展示');
    expect(html).toContain('工作步骤收起时机');
  });

  it('注释说明改的是默认值而不是能力：四档都能点开看细节', () => {
    const html = render();
    expect(html).toContain('只决定默认露多少');
  });
});
