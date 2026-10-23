/**
 * 触发器（不打开菜单的部分）的行为锁。
 *
 * 仓库测试环境是 vitest 的 node，没有 jsdom，所以只能出静态 HTML：菜单展开后的
 * 交互由 `src/lib/model-menu.test.ts` 用纯函数覆盖，这里只钉住「闭着的时候用户看到
 * 什么、读屏听到什么」——这部分恰恰是渲染分支最多、最容易两套文案走偏的地方。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { ModelItem } from '../types';
import { ModelSelector, type ModelSelectorProps } from './ModelSelector';

const model = (id: string, overrides: Partial<ModelItem> = {}): ModelItem => ({
  id,
  name: id,
  provider: '默认网关',
  reasoning: true,
  supported_reasoning_levels: ['low', 'medium', 'high'],
  is_default: false,
  ...overrides,
});

const render = (overrides: Partial<ModelSelectorProps> = {}) =>
  renderToStaticMarkup(
    createElement(ModelSelector, {
      models: [model('auto/a', { name: 'DeepSeek V4.1 Flash' })],
      currentModel: 'auto/a',
      currentReasoningEffort: 'high',
      onSelectModel: vi.fn(),
      onSelectEffort: vi.fn(),
      ...overrides,
    }),
  );

describe('ModelSelector 触发器', () => {
  it('一个触发器同时显示模型名与思考等级：`模型名 · 等级`', () => {
    const html = render();

    expect(html).toContain('DeepSeek V4.1 Flash');
    expect(html).toContain('· high');
    // 只有一个 button（等级不再是并排按钮）
    expect(html.match(/<button/g)).toHaveLength(1);
  });

  it('关闭状态：aria-haspopup=menu、aria-expanded=false、没有 aria-controls', () => {
    const html = render();

    expect(html).toContain('aria-haspopup="menu"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('aria-controls');
  });

  it('无障碍名是「模型名，思考等级」——两段分色文字要合成一句话', () => {
    expect(render()).toContain('aria-label="DeepSeek V4.1 Flash，high"');
  });

  it('off 等级显示成「关闭」，不把内部取值漏给用户', () => {
    const html = render({ currentReasoningEffort: 'off' });

    expect(html).toContain('· 关闭');
    expect(html).toContain('aria-label="DeepSeek V4.1 Flash，关闭"');
    expect(html).not.toContain('· off');
  });

  it('模型不支持思考（reasoning=false）时隐藏等级文字', () => {
    const html = render({
      models: [model('auto/a', { name: 'DeepSeek V4.1 Flash', reasoning: false })],
    });

    expect(html).toContain('DeepSeek V4.1 Flash');
    expect(html).not.toContain('· high');
    expect(html).toContain('aria-label="DeepSeek V4.1 Flash"');
  });

  it('未选模型时显示常规字重的「请选择模型」，并隐藏等级', () => {
    const html = render({ currentModel: null });

    expect(html).toContain('请选择模型');
    // 「请选择模型」是占位，不能像选中项那样被加粗当作已选值
    expect(html).not.toContain('font-medium');
    expect(html).toContain('aria-label="请选择模型"');
  });

  it('当前模型已不在列表：触发器退回到 id 尾段，不显示一个空按钮', () => {
    const html = render({ currentModel: 'auto/gone-model' });

    expect(html).toContain('gone-model');
    expect(html).toContain('aria-expanded="false"');
  });

  it('加载中（空目录且没有错误）时触发器仍然可点，不锁住整条工具条', () => {
    const html = render({ models: [], modelsError: null, modelsLoading: true, currentModel: null });

    expect(html).toContain('<button');
    expect(html).not.toContain('disabled=""');
    expect(html).toContain('请选择模型');
  });

  it('空目录且加载失败后不当成「加载中」：触发器文案仍是「请选择模型」', () => {
    const html = render({ models: [], modelsError: '模型目录加载失败', modelsLoading: false, currentModel: null });

    expect(html).toContain('请选择模型');
  });

  it('渲染成行内块，交给工具条的 flex 去压缩，而不是自己占满一行', () => {
    expect(render()).toContain('class="relative inline-block min-w-0"');
  });
});
