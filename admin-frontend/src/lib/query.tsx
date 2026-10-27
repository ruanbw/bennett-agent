/**
 * 服务端状态层（TanStack Query）。
 *
 * 之前每个面板各自写 `useState` + `useEffect` + `reloadToken`，结果是三份互相独立的
 * 「加载中 / 失败 / 重试」逻辑，且刷新会把整块数据重拉（提供商列表连带所有模型）。
 * Query 接管的正是这些通用问题：缓存、去重、失效、后台重取、失败重试。
 *
 * 约定：
 *
 * - **queries 只读**：写操作走 `useMutation`，成功后只失效受影响的那几个 key。
 * - **不做乐观更新**：管理动作失败必须让人看见真实状态；数据量小、动作少，
 *   「点了 → 等结果 → 再刷新」比「先改界面、失败回滚」更不容易骗到管理员。
 * - 401/403/404 不重试：重试一个权限错误只会拖慢错误呈现。
 */

import type { ReactNode } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';

import { queryClient } from '@/lib/query-client';

export function QueryProvider({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
