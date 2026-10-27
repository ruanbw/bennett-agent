/**
 * QueryClient 单例与默认策略。
 *
 * 单独一个模块（Provider 与它分开）：组件文件只导出组件，Fast Refresh 才不用整页重挂；
 * 测试也能直接 import 这个 client 而不带 React。
 */

import { QueryClient } from '@tanstack/react-query';

import { AdminApiError } from '@/lib/admin';

export const queryClient = new QueryClient({
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
