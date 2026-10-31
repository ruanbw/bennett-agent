/**
 * 导航表完整性：侧边栏菜单与面包屑共用同一份数据源，表坏了两边一起坏。
 *
 * 这组测试锁住三件事：每项都能渲染（label 是非空字符串、Icon 是真实组件）、
 * path 不重复（面包屑按 path 查表，重复会让先出现的吞掉后出现的）、
 * 默认落点确实在表里（否则登录后跳到一个「页面不存在」）。
 */

import { describe, expect, it } from 'vitest';

import { HOME_PATH, NAV_ITEMS, breadcrumbsFor } from './nav';

describe('NAV_ITEMS', () => {
  it('每项都有绝对路径、可渲染标题、图标与分组', () => {
    expect(NAV_ITEMS.length).toBeGreaterThan(0);

    for (const item of NAV_ITEMS) {
      // path 必须以 / 开头：react-router 的绝对路由与面包屑查表都依赖这个约定
      expect(item.to, `${item.to} 应以 / 开头`).toMatch(/^\//);
      expect(item.label.trim().length, `${item.to} 的 label 不能是空白`).toBeGreaterThan(0);
      // Icon 是 lucide 的组件（forwardRef 对象），null/undefined 意味着侧边栏会渲染出空位
      expect(item.Icon, `${item.to} 的 Icon 应是组件`).toBeTruthy();
      expect(item.group.trim().length, `${item.to} 缺少侧边栏分组`).toBeGreaterThan(0);
    }
  });

  it('没有重复路径：面包屑按 path 查表，重复会让后者永远查不到', () => {
    const paths = NAV_ITEMS.map((item) => item.to);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('默认落点在导航表内：登录后跳到的是一个真实存在的页面', () => {
    expect(NAV_ITEMS.some((item) => item.to === HOME_PATH)).toBe(true);
  });

  it('App.tsx 声明的路由全部入表：新增页面漏配导航时在这里暴露', () => {
    // App.tsx 里 admin 区域的路由（不含通配的 *）；表少一条，面包屑就会显示「页面不存在」
    expect(NAV_ITEMS.map((item) => item.to).sort()).toEqual(
      ['/overview', '/providers', '/users'].sort(),
    );
  });
});

describe('breadcrumbsFor', () => {
  it('已知页面返回「首页 + 当前页」两级', () => {
    expect(breadcrumbsFor('/providers')).toEqual([
      { label: '首页', to: HOME_PATH },
      { label: '模型提供商' },
    ]);
  });

  it('未知路径只剩「页面不存在」一级（不编造上级）', () => {
    expect(breadcrumbsFor('/nope')).toEqual([{ label: '页面不存在' }]);
  });
});
