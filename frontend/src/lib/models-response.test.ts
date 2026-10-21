import { describe, expect, it } from 'vitest';

import {
  ModelListShapeError,
  parseModelListResponse,
  readModelListResponse,
} from './models-response';

const valid = {
  id: 'gw/openai/gpt-4o',
  name: 'GPT-4o',
  provider: '网关',
  reasoning: true,
  supported_reasoning_levels: ['low', 'medium'],
  context_window: 128000,
  is_default: true,
};

describe('parseModelListResponse', () => {
  it('正常响应原样解析', () => {
    const parsed = parseModelListResponse({ models: [valid], default_model: 'gw/openai/gpt-4o' });

    expect(parsed.models).toHaveLength(1);
    expect(parsed.models[0]).toMatchObject({ id: 'gw/openai/gpt-4o', name: 'GPT-4o', context_window: 128000 });
    expect(parsed.defaultModel).toBe('gw/openai/gpt-4o');
    expect(parsed.dropped).toBe(0);
  });

  it('models 不是数组时抛可读错误，而不是当成空列表', () => {
    // 空列表会被显示成「模型列表为空」——让用户以为管理员没配模型，
    // 而真实原因是响应格式不对，两种情况的下一步动作不同。
    expect(() => parseModelListResponse({ models: 'oops' })).toThrow(ModelListShapeError);
    expect(() => parseModelListResponse({ models: 'oops' })).toThrow(/不是数组/);
    expect(() => parseModelListResponse({})).toThrow(ModelListShapeError);
    expect(() => parseModelListResponse(null)).toThrow(/JSON 对象/);
    expect(() => parseModelListResponse([1, 2])).toThrow(/JSON 对象/);
  });

  it('缺 id 的条目被丢掉并计数，其余条目照常可用', () => {
    const parsed = parseModelListResponse({ models: [valid, { name: '没有 id' }, null, 42] });

    expect(parsed.models).toHaveLength(1);
    expect(parsed.dropped).toBe(3);
  });

  it('缺省字段按默认值补齐（老网关不返回 reasoning 时条目仍然可用）', () => {
    const parsed = parseModelListResponse({ models: [{ id: 'gw/x' }] });

    expect(parsed.models[0]).toEqual({
      id: 'gw/x',
      name: 'gw/x', // 没有 name 就用 id，选择器不会显示空白
      provider: '',
      model_id: null,
      description: null,
      reasoning: null,
      supported_reasoning_levels: [],
      context_window: null,
      max_output_tokens: null,
      is_default: false,
    });
  });

  it('字段类型不对时回落默认值，而不是让坏值进入渲染', () => {
    const parsed = parseModelListResponse({
      models: [
        {
          id: 'gw/x',
          reasoning: 'yes', // 不是布尔 → 当「未声明」
          supported_reasoning_levels: ['low', 42, null],
          context_window: '128000', // 字符串 → 当没有
          is_default: 1, // 非 true → false
        },
      ],
    });

    const item = parsed.models[0];
    expect(item.reasoning).toBeNull();
    expect(item.supported_reasoning_levels).toEqual(['low']);
    expect(item.context_window).toBeNull();
    expect(item.is_default).toBe(false);
  });

  it('空模型列表是合法响应（管理员确实没启用模型）', () => {
    const parsed = parseModelListResponse({ models: [], default_model: null });

    expect(parsed.models).toEqual([]);
    expect(parsed.defaultModel).toBeNull();
  });
});

describe('readModelListResponse', () => {
  it('读到合法 JSON 时解析成功', async () => {
    const response = new Response(JSON.stringify({ models: [valid] }), { status: 200 });

    await expect(readModelListResponse(response)).resolves.toMatchObject({ dropped: 0 });
  });

  it('响应体不是 JSON 时抛可读错误（而不是 SyntaxError 一路冒到 ErrorBoundary）', async () => {
    const response = new Response('<html>502 Bad Gateway</html>', { status: 200 });

    await expect(readModelListResponse(response)).rejects.toThrow(/不是合法 JSON/);
  });
});
