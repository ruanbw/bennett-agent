/**
 * QueryClient 单例与默认策略。
 *
 * 单独一个模块（Provider 与它分开）：组件文件只导出组件，Fast Refresh 才不用整页重挂；
 * 测试也能直接 import 这个 client 而不带 React。
 *
 * ## 401 与 403 的分流
 *
 * 两者过去走同一条路径（面板里一行「加载失败 + 重试」），但它们的解药不同：
 *
 *   - 403 是「凭据有效，但没有这个权限」：换账号/要权限才有救，重试必然再 403。
 *     留在面板里当提示是对的。
 *   - 401 是「凭据失效」：只有重新认证能救。把它渲染成「重试」按钮是骗人的——点一次
 *     再吃一次 401。RFC 9110 §15.5.2 也明说：凭据不足时不应拿同一套凭据自动重试。
 *
 * 所以 401 走一个注册进来的「全局失效」回调（`App.tsx` 注册成重新探测登录态），403
 * 保持原地呈现。这仍然是**不做**全局 401 广播：管理后台的请求面小，广播会把「一次
 * 面板请求失败」升级成整个应用的登录态变更，那是用户端的做法（那边有同步引擎要停）。
 */

import { QueryCache, QueryClient } from '@tanstack/react-query';

import { AdminApiError } from '@/lib/admin';

/** 401 的处理入口。由 `App.tsx` 在挂载时注册（探测登录态），卸载时清掉。 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/**
 * 造一个带默认策略的 client。
 *
 * 导出工厂而不只导出单例，是为了让用例能建一个**独立**的 client：单例在测试之间共享
 * 缓存，一条用例的失败结果会被下一条读到。
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        // 只在重试耗尽后触发（401/403 不重试，所以是立即触发）
        if (error instanceof AdminApiError && error.status === 401) onUnauthorized?.();
      },
    }),
    defaultOptions: {
      queries: {
        // 后台数据变动只来自管理员本人或同事，窗口重新聚焦时重取是有价值的
        refetchOnWindowFocus: true,
        staleTime: 15_000,
        retry: (failureCount, error) => {
          if (error instanceof AdminApiError && [401, 403, 404].includes(error.status)) return false;
          return failureCount < 1;
        },
      },
      mutations: { retry: false },
    },
  });
}

export const queryClient = createQueryClient();
