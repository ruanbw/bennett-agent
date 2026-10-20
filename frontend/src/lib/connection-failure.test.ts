import { describe, expect, it } from 'vitest';

import { ChatRequestError } from './chat-request-error';
import { describeConnectionFailure } from './connection-failure';

describe('describeConnectionFailure', () => {
  it('5xx 归为服务端暂时不可用，并允许重试', () => {
    const failure = describeConnectionFailure(new ChatRequestError(503));

    expect(failure.retryable).toBe(true);
    expect(failure.message).toContain('服务器暂时不可用');
    expect(failure.message).toContain('503');
  });

  it('429 沿用后端给的「等几秒」，仍可重试', () => {
    const failure = describeConnectionFailure(new ChatRequestError(429, '请求过于频繁，请 17 秒后重试', 17));

    expect(failure.retryable).toBe(true);
    expect(failure.message).toBe('请求过于频繁，请 17 秒后重试');
  });

  it('4xx 是请求本身的问题：给详情但不再给重试承诺', () => {
    const failure = describeConnectionFailure(new ChatRequestError(422, '对话历史总长度超出上限 400000 字符'));

    expect(failure.retryable).toBe(false);
    expect(failure.message).toBe('对话历史总长度超出上限 400000 字符');
  });

  it('401 明确说清要重新登录，并且不提供重试', () => {
    expect(describeConnectionFailure(new ChatRequestError(401))).toEqual({
      message: '登录状态已失效，请重新登录',
      retryable: false,
    });
  });

  it('浏览器网络失败（TypeError / Failed to fetch / Load failed）翻译成中文', () => {
    for (const error of [
      new TypeError('Failed to fetch'),
      new Error('Load failed'),
      new Error('NetworkError when attempting to fetch resource.'),
    ]) {
      const failure = describeConnectionFailure(error);
      expect(failure.retryable).toBe(true);
      expect(failure.message).toBe('网络连接中断，请检查网络后重试');
    }
  });

  it('超时（AbortError / TimeoutError）与用户取消区分开，给单独文案', () => {
    for (const name of ['AbortError', 'TimeoutError']) {
      const error = Object.assign(new Error('aborted'), { name });
      expect(describeConnectionFailure(error)).toEqual({ message: '请求超时，请重试', retryable: true });
    }
  });

  it('只有状态码的 HTTP Error（App 里的模型列表请求）也能按状态归类', () => {
    expect(describeConnectionFailure(new Error('HTTP 500'))).toMatchObject({
      retryable: true,
      message: '服务器暂时不可用（HTTP 500），请稍后重试',
    });
    expect(describeConnectionFailure(new Error('HTTP 400'))).toMatchObject({ retryable: false });
  });

  it('自带中文说明的错误原样透传（前缀标出这是本轮失败）', () => {
    expect(describeConnectionFailure(new Error('响应没有可读的流式主体'))).toEqual({
      message: '请求失败：响应没有可读的流式主体',
      retryable: true,
    });
  });

  it('不认识的值退化为通用提示，不把英文原文或 undefined 抛给用户', () => {
    for (const error of [new Error('boom'), null, undefined, 'oops']) {
      const failure = describeConnectionFailure(error);
      expect(failure.retryable).toBe(true);
      expect(failure.message).toBe('请求失败，请稍后重试');
    }
  });
});
