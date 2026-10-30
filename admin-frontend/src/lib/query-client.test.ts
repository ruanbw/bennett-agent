import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AdminApiError } from './admin';
import { createQueryClient, setUnauthorizedHandler } from './query-client';

/**
 * 401 与 403 的分流。
 *
 * 旧行为：两者都渲染成面板里的「加载失败 + 重试」，而 401 点重试必然再吃一次 401——
 * 界面唯一给出的动作恰恰是没用动作。这里钉住的是「谁触发全局失效、谁不触发」以及
 * 「401 不重试」。
 *
 * 每个用例新建一个 client：单例在用例之间共享缓存，上一条的失败结果会被下一条读到。
 */
const onUnauthorized = vi.fn();

beforeEach(() => {
  onUnauthorized.mockClear();
  setUnauthorizedHandler(onUnauthorized);
});

afterEach(() => {
  setUnauthorizedHandler(null);
});

/** 用给定的错误跑一次查询，返回 queryFn 的调用次数（重试次数 = 次数 - 1）。 */
async function failWith(status: number): Promise<number> {
  const client = createQueryClient();
  const queryFn = vi.fn(() => Promise.reject(new AdminApiError(`HTTP ${status}`, status)));

  await expect(
    // retryDelay 归零：这里关心的是重试**几次**，不是退避曲线
    client.fetchQuery({ queryKey: ['probe', status], queryFn, retryDelay: 0 })
  ).rejects.toBeInstanceOf(AdminApiError);

  return queryFn.mock.calls.length;
}

describe('QueryCache 的错误分流', () => {
  it('401 触发全局失效：凭据没了，只有重新认证能救', async () => {
    await failWith(401);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('403 不触发全局失效：权限不够不是「未登录」，踢回登录页也没用', async () => {
    await failWith(403);

    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('401 不重试：拿同一套失效凭据再试只会再吃一次 401', async () => {
    const calls = await failWith(401);

    expect(calls).toBe(1);
  });

  it('其他失败照旧重试一次，且不把人踢回登录页', async () => {
    const calls = await failWith(500);

    expect(calls).toBe(2);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
