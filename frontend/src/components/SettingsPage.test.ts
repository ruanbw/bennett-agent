import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  AccountSection,
  AppearanceSection,
  ConversationSection,
  SettingsPage,
} from './SettingsPage';
import { SETTINGS_SECTIONS, type SettingsSectionId } from '../lib/settings-sections';

/**
 * 设置页（原「设置弹窗」）的渲染契约。
 *
 * 只做服务端渲染：页面是同步视图，几个偏好 hook 的读盘都在 effect 里，node 环境
 * 下可以直接断言结构。交互（点分区、←↑↓ 换项、焦点归还）由浏览器侧覆盖。
 */
const renderPage = (props: Partial<Parameters<typeof SettingsPage>[0]> = {}) =>
  renderToStaticMarkup(
    createElement(SettingsPage, { userEmail: 'user@example.com', onClose: vi.fn(), ...props })
  );

/** 取某个分区的面板开始标签（用来断言它是否被 `hidden` 收起）。 */
const panelOpenTag = (html: string, id: SettingsSectionId): string => {
  const from = html.indexOf(`id="settings-panel-${id}"`);
  return html.slice(from, html.indexOf('>', from) + 1);
};

describe('设置页布局：居中一列，左侧分区菜单 + 右侧内容', () => {
  it('左侧是分区 tablist，纵向排列', () => {
    const html = renderPage();

    expect(html).toContain('role="tablist" aria-label="设置分区" aria-orientation="vertical"');
    expect(html.match(/role="tab"/g)).toHaveLength(SETTINGS_SECTIONS.length);
  });

  it('每个分区在菜单里都有入口，且同一时刻只有一个被选中', () => {
    const html = renderPage();

    for (const section of SETTINGS_SECTIONS) {
      expect(html).toContain(section.label);
    }
    // 默认停在第一个分区（账号与安全）：按分区 id 定位那一颗按钮
    const firstTab = html.slice(html.indexOf('id="settings-tab-account"'));
    expect(firstTab.slice(0, firstTab.indexOf('</button>'))).toContain('aria-selected="true"');
    expect(firstTab).toContain('账号与安全');
    expect(html.match(/aria-selected="true"/g)).toHaveLength(1);
  });

  it('右栏标题跟随分区，层级是 h1 页面 / h2 分区 / h3 区块', () => {
    const html = renderPage();

    expect(html).toMatch(/<h1[^>]*>设置<\/h1>/);
    expect(html).toMatch(/<h2[^>]*>账号与安全<\/h2>/);
    expect(html).toContain('<h3');
    // 不出现跳级的 h4：读屏按标题导航时层级要连续
    expect(html).not.toContain('<h4');
  });

  it('每个分区都有面板，非当前分区的只收起、不卸载（切回来草稿还在）', () => {
    const html = renderPage();

    expect(html.match(/role="tabpanel"/g)).toHaveLength(SETTINGS_SECTIONS.length);
    for (const section of SETTINGS_SECTIONS) {
      const tag = panelOpenTag(html, section.id);
      // 面板挂在 tab 上，读屏能说清「这块内容属于哪个分区」
      expect(tag).toContain(`aria-labelledby="settings-tab-${section.id}"`);
      expect(tag.includes('hidden=""')).toBe(section.id !== 'account');
    }
  });

  it('可以从指定分区直接进入', () => {
    const html = renderPage({ defaultSection: 'appearance' });

    expect(html).toMatch(/<h2[^>]*>外观与主题<\/h2>/);
    expect(panelOpenTag(html, 'appearance')).not.toContain('hidden=""');
    expect(panelOpenTag(html, 'account')).toContain('hidden=""');
  });

  it('设置里只有真正能调的三组：知识库 / 本机数据 / 关于系统不在这一页', () => {
    const html = renderPage();

    // 这三块是信息展示，不是设置项；混进来会把这一页撑成大表
    expect(SETTINGS_SECTIONS.map((section) => section.id)).toEqual([
      'account',
      'appearance',
      'conversation',
    ]);
    expect(html).not.toContain('知识库');
    expect(html).not.toContain('本机数据');
    expect(html).not.toContain('关于系统');
  });

  it('页头只有标题与一个关闭图标，不再有文字版「返回对话」', () => {
    const html = renderPage();

    expect(html).toMatch(/<h1[^>]*>设置<\/h1>/);
    expect(html).toContain('aria-label="关闭设置"');
    // 文字按钮去掉后，页头只剩「设置」四个字
    expect(html).not.toContain('返回对话');
  });
});

describe('账号与安全分区', () => {
  it('展示昵称、完整邮箱与角色徽标', () => {
    const html = renderToStaticMarkup(
      createElement(AccountSection, { userEmail: 'alice@example.com', userRole: 'admin' })
    );

    expect(html).toContain('登录身份');
    expect(html).toContain('alice@example.com');
    // 角色徽标按中文名显示，未知角色不猜
    expect(html).toContain('管理员');
    expect(
      renderToStaticMarkup(createElement(AccountSection, { userEmail: 'a@example.com', userRole: 'owner' }))
    ).not.toContain('owner');
  });

  it('没有邮箱时显示占位而不是空白', () => {
    const html = renderToStaticMarkup(createElement(AccountSection, { userEmail: null }));

    expect(html).toContain('未登录');
    expect(html).toContain('未');
  });

  it('改密表单就在这一分区，并说清会登出所有设备', () => {
    const html = renderToStaticMarkup(createElement(AccountSection, { userEmail: 'a@example.com' }));

    expect(html).toContain('修改密码');
    // 会登出所有设备这件事由表单自己说（原来页面里还平白重复了一遍）
    expect(html).toContain('所有设备都需要用新密码重新登录');
  });
});

describe('外观与主题分区', () => {
  it('三态主题是一个 radiogroup，恰好一项被选中', () => {
    const html = renderToStaticMarkup(createElement(AppearanceSection));

    for (const label of ['浅色', '深色', '跟随系统']) {
      expect(html).toContain(label);
    }
    const group = html.slice(html.indexOf('role="radiogroup" aria-label="外观主题"'));
    expect(group).toContain('role="radiogroup"');
    expect(group.match(/role="radio"/g)).toHaveLength(3);
    expect(group.match(/aria-checked="true"/g)).toHaveLength(1);
    expect(group.match(/aria-checked="false"/g)).toHaveLength(2);
  });

  it('组内只有选中项进 Tab 序列，其余靠方向键——与原生 radio 一致', () => {
    const html = renderToStaticMarkup(createElement(AppearanceSection));

    expect(html.match(/tabindex="0"/g)).toHaveLength(1);
    expect(html.match(/tabindex="-1"/g)).toHaveLength(2);
  });
});

describe('对话显示分区', () => {
  const renderSection = () => renderToStaticMarkup(createElement(ConversationSection));

  /** 只截某一组：三组都是 radiogroup，不能全局数。 */
  const group = (html: string, ariaLabel: string, nextAriaLabel?: string) => {
    const start = html.indexOf(`aria-label="${ariaLabel}"`);
    const end = nextAriaLabel ? html.indexOf(`aria-label="${nextAriaLabel}"`, start) : undefined;
    return html.slice(start, end === -1 ? undefined : end);
  };

  it('工作步骤四档都在，且默认选中「标准」', () => {
    const section = group(renderSection(), '工作步骤展示', '工作步骤收起时机');

    for (const label of ['简洁', '标准', '详细', '完全展开']) {
      expect(section).toContain(label);
    }
    expect(section.match(/aria-checked="true"/g)).toHaveLength(1);
    expect(section.match(/aria-checked="false"/g)).toHaveLength(3);
  });

  it('收起时机两档独立成组，默认「回答结束后」', () => {
    const section = group(renderSection(), '工作步骤收起时机', '性能与用量');

    expect(section).toContain('回答结束后');
    expect(section).toContain('下次有新消息时');
    expect(section.match(/aria-checked="true"/g)).toHaveLength(1);
    expect(section.match(/aria-checked="false"/g)).toHaveLength(1);
  });

  it('性能与用量两档独立成组，并说明不影响记账与上下文圆环', () => {
    const section = group(renderSection(), '性能与用量');

    expect(section).toContain('简洁');
    expect(section).toContain('详细');
    expect(section.match(/aria-checked="true"/g)).toHaveLength(1);
    expect(section).toContain('上下文占用环始终显示');
  });

  it('四档只改默认展开程度，不是能力开关', () => {
    expect(renderSection()).toContain('只决定默认露多少');
  });
});
