/**
 * 「对话显示」上下文。
 *
 * context 与读取 hook 同文件（与 `theme-context.ts` 同一形态）。纯逻辑（白名单、
 * 存储读写、storage 事件解析、四档策略表）在 `lib/detail-level.ts`，这里只负责把值
 * 递给组件树。
 *
 * 值是**一个策略对象**而不是散着传四个布尔：消费者（过程组、思考行）真正需要的
 * 是「这一档该展开还是收起、要不要预览」这些组合行为，四项单独读会各自重渲染，
 * 也容易在某处漏读一项。`presentationPolicyFor` 对同一对入参返回同一个对象引用，
 * 所以 selector 里的相等判断是稳的（见 `lib/detail-level.ts` 的注释）。
 *
 * 消费者：
 *   - 设置面板的「工作步骤展示」四档与「工作步骤收起时机」两档（唯一入口）；
 *   - 消息里的「过程」折叠块（`trace-group.tsx`）与思考行（`reasoning-view.tsx`）。
 */

import { createContext, useContext } from 'react';

import {
  DEFAULT_COLLAPSE_TIMING,
  DEFAULT_DETAIL_LEVEL,
  presentationPolicyFor,
  type CollapseTiming,
  type DetailLevel,
  type PresentationPolicy,
} from '../lib/detail-level';

export interface DetailLevelContextValue {
  level: DetailLevel;
  setLevel: (level: DetailLevel) => void;
  /** 刚结束的一轮何时收起；与 `level` 正交。 */
  collapseTiming: CollapseTiming;
  setCollapseTiming: (timing: CollapseTiming) => void;
  /** 由上面两者推导出的完整行为策略（引用稳定）。 */
  policy: PresentationPolicy;
}

export const DetailLevelContext = createContext<DetailLevelContextValue>({
  level: DEFAULT_DETAIL_LEVEL,
  setLevel: () => {},
  collapseTiming: DEFAULT_COLLAPSE_TIMING,
  setCollapseTiming: () => {},
  policy: presentationPolicyFor(DEFAULT_DETAIL_LEVEL, DEFAULT_COLLAPSE_TIMING),
});

export function useDetailLevel(): DetailLevelContextValue {
  return useContext(DetailLevelContext);
}

/**
 * 只要行为策略时用这个。
 *
 * 单独导出是为了让「只是要读一档策略」的组件不必解构整个 context（也就不会因为
 * `setLevel` 这种无关字段变化而跟着重渲染——虽然它们本来也是稳定的）。
 */
export function usePresentationPolicy(): PresentationPolicy {
  return useContext(DetailLevelContext).policy;
}
