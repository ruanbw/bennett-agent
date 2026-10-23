/**
 * 「对话显示」上下文。
 *
 * context 与读取 hook 同文件（与 `theme-context.ts` 同一形态）。纯逻辑（白名单、
 * 存储读写、storage 事件解析）在 `lib/detail-level.ts`，这里只负责把值递给组件树。
 *
 * 值由 `DetailLevelProvider` 提供；消费者：
 *   - 设置面板的「详细 / 简洁」选择器（唯一入口）；
 *   - 消息里的「过程」折叠块（`trace-group.tsx`）与思考块（`reasoning-view.tsx`），
 *     决定默认展开还是默认收起。
 */

import { createContext, useContext } from 'react';

import { DEFAULT_DETAIL_LEVEL, type DetailLevel } from '../lib/detail-level';

export interface DetailLevelContextValue {
  level: DetailLevel;
  setLevel: (level: DetailLevel) => void;
}

export const DetailLevelContext = createContext<DetailLevelContextValue>({
  level: DEFAULT_DETAIL_LEVEL,
  setLevel: () => {},
});

export function useDetailLevel(): DetailLevelContextValue {
  return useContext(DetailLevelContext);
}
