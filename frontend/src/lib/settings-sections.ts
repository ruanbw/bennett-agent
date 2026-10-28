/**
 * 设置页的分区清单。
 *
 * 单独放在 `lib` 而不是页面组件里：它同时被页面（渲染菜单）与测试（断言「菜单里
 * 有哪几项」）使用，留在组件文件里会让 Vite 的 Fast Refresh 退化成整页刷新
 * （同一个模块既导出组件又导出常量）。
 *
 * ## 只收「设置」
 *
 * 知识库、本机数据、关于系统都是**信息展示**，不是可以调的东西：它们一进来，
 * 设置页就被撑成一张三栏大表，右栏大片留白。这三块已从设置里移出。
 *
 * 顺序即页面顺序：账号在最上（最先被找）。
 */

import { AlignLeft, Cpu, Sun, User, type LucideIcon } from 'lucide-react';

export type SettingsSectionId = 'account' | 'appearance' | 'conversation' | 'capabilities';

export interface SettingsSection {
  id: SettingsSectionId;
  label: string;
  /** 右栏标题下的一句话：说明这个分区改的是什么。 */
  description: string;
  Icon: LucideIcon;
}

export const SETTINGS_SECTIONS: ReadonlyArray<SettingsSection> = [
  {
    id: 'account',
    label: '账号与安全',
    description: '当前登录身份与登录密码',
    Icon: User,
  },
  {
    id: 'appearance',
    label: '外观与主题',
    description: '界面配色，以及深浅色如何跟随系统',
    Icon: Sun,
  },
  {
    id: 'conversation',
    label: '对话显示',
    description: '工作步骤露多少、什么时候收起，以及用量读数显示多少',
    Icon: AlignLeft,
  },
  /*
   * 运行时能力。
   *
   * 这一项严格说也是「信息展示」，破例放进来的理由是它**解释别的功能的缺失**：
   * 没有联网搜索时起步建议里搜不到「搜索最新动态」，用户只会以为这个助手不会搜索；
   * MCP 配了但连不上时那些工具直接从列表里消失，运维会以为配置从来没生效。
   * 把状态摆出来，问题就从「功能缺失」变成「部署没配」——前者用户没法处理，后者可以。
   */
  {
    id: 'capabilities',
    label: '运行时能力',
    description: '当前服务端实际装配了什么：联网搜索、隔离执行环境、工具与 MCP',
    Icon: Cpu,
  },
];

/** 默认分区：账号。`defaultSection` 不传时用它，也是测试的锚点。 */
export const DEFAULT_SETTINGS_SECTION: SettingsSectionId = 'account';
