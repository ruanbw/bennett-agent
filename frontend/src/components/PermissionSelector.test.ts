/**
 * 权限选择器触发器的静态结构锁。
 *
 * 菜单展开后的交互依赖浏览器事件，纯函数那半由 `lib/permissions.test.ts` 覆盖；
 * 这里只锁「闭着的时候用户看到什么、读屏听到什么」，以及危险权限不能被静默藏起来。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { FALLBACK_PERMISSION_CATALOG, FALLBACK_PERMISSION_PRESETS, defaultSelection } from '@/lib/permissions';
import { PermissionSelector } from './PermissionSelector';

function render() {
  return renderToStaticMarkup(
    createElement(PermissionSelector, {
      catalog: FALLBACK_PERMISSION_CATALOG,
      presets: FALLBACK_PERMISSION_PRESETS,
      defaultPermissions: defaultSelection().permissions,
      disabled: false,
    }),
  );
}

describe('PermissionSelector 触发器', () => {
  it('显示「权限 · 当前预设」，默认是每次询问', () => {
    const html = render();
    expect(html).toContain('权限 · 每次询问');
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).toContain('aria-expanded="false"');
  });

  it('无障碍名把「权限」和当前档位合成一句话', () => {
    expect(render()).toContain('aria-label="权限：每次询问"');
  });

  it('是行内块，交给输入框工具条的 flex 压缩', () => {
    expect(render()).toContain('relative inline-block min-w-0');
  });
});
