import { AlertTriangleIcon, SquareIcon } from "lucide-react";
import { type FC, useState } from "react";

import { cn } from "@/lib/utils";
import {
  formatRunDurationText,
  turnStatusLabel,
  type TurnEndReason,
} from "@/lib/step-process";

/**
 * 一轮**异常结束**时的状态行：`已停止` / `回复不完整（已中断）` / `处理失败`。
 *
 * 正常结束（`reason === "ok"`）**不渲染任何东西**：一条回答存在本身就说明这一轮跑完了，
 * 再压一行「已完成」只是给每条回答加一行与一个下边框。耗时属于「这一轮花了多久」，
 * 挂在消息底部的操作栏里（`thread.aui.tsx` 的 `TurnElapsed`），不进这个状态行。
 *
 * 为什么不再是按钮：此前这一行渲染成一个 `canCollapse=false` 的 `<button disabled>`
 * ——不可点、不可聚焦，读屏却仍会把它当控件读。整轮折叠至今没有实现，先把它做成
 * 纯文本状态，别让界面长出一个假控件。
 */
export interface TurnStatusRowProps {
  /** 结束原因；`ok` 传进来也不会渲染。 */
  reason: TurnEndReason;
  /** 本轮墙钟耗时（毫秒）；拿不到时传 null，只显示状态不显示耗时。 */
  elapsedMs?: number | null;
  className?: string;
}

export const TurnStatusRow: FC<TurnStatusRowProps> = ({ reason, elapsedMs, className }) => {
  if (reason === "ok") return null;

  const Icon = reason === "aborted" ? SquareIcon : AlertTriangleIcon;
  // 与服务端一致：不足 1 秒按 1 秒报，「用时 0秒」比不报更像 bug
  const duration =
    elapsedMs !== undefined && elapsedMs !== null && Number.isFinite(elapsedMs)
      ? formatRunDurationText(Math.max(1000, elapsedMs))
      : "";

  return (
    <p
      data-turn-status={reason}
      // role=status：这一行是本轮结果的附注，不该在用户读正文时打断他
      role="status"
      className={cn(
        "flex flex-wrap items-center gap-1.5 text-[11px]",
        reason === "aborted" ? "text-muted-foreground/80" : "text-destructive/80",
        className,
      )}
    >
      <Icon
        className={cn("shrink-0", reason === "aborted" ? "size-2.5 fill-current" : "size-3")}
        aria-hidden="true"
      />
      <span>
        {turnStatusLabel(reason)}
        {duration !== "" && `，用时 ${duration}`}
      </span>
    </p>
  );
};

/**
 * 播报内容：只说轮次，不说正文。
 *
 * 把最后一条回答塞进 live region 等于再念一遍整段，用户既不能暂停也挑不了段落。
 * 单独成一个组件（而不是一个返回字符串的函数），是因为本仓库没有能跑 effect 的
 * renderer：用 `react-dom/server` 直接出静态 HTML 是唯一能断言文案实际内容的办法。
 */
export const TurnAnnouncementText: FC<{ turnNumber: number | null }> = ({ turnNumber }) => (
  <>{turnNumber === null ? "回复完成" : `第 ${turnNumber} 轮回复完成`}</>
);

export interface TurnAnnouncementProps {
  /** 线程是否在生成。真正的播报发生在 true → false 的那一帧。 */
  isRunning: boolean;
  /** 本轮是第几轮（1 起）：只用来让相邻两轮的文案互换，否则内容不变的 live region 不会重念。 */
  turnNumber?: number | null;
}

/**
 * 流式回答结束时的**一次性**播报（视觉隐藏）。
 *
 * 为什么需要它：消息列表是 `role="log" aria-live="polite"`，`aria-busy` 在生成结束时
 * 翻回 false，此刻整段新回答作为一次 live-region 变更交给读屏——一次几千字的注入，
 * 用户既插不上话也不知道什么时候算结束。这里另设一个只读一句话的状态区，把「结束」
 * 这件事本身说清楚，而且始终**不**包含正文。
 *
 * 为什么不直接把消息列表改成 `aria-live="off"`：那样连用户自己的消息、审批卡片上的
 * 变化也不会被念到。两者分工更稳：容器负责「存在与追加」，这一个负责「这一轮结束了」。
 */
export const TurnAnnouncement: FC<TurnAnnouncementProps> = ({ isRunning, turnNumber = null }) => {
  // 「上一帧在生成、这一帧不在了」是个跨渲染的状态，只能用 state 记：它在渲染期间
  // 条件更新（React 文档里的 adjusting state when props change），不进 effect——
  // 进 effect 就得多渲染一帧，播报也会晚一拍。
  const [phase, setPhase] = useState({ running: isRunning, completed: false });
  if (phase.running !== isRunning) {
    setPhase({ running: isRunning, completed: !isRunning });
  }

  return (
    // role=status 隐含 aria-live=polite：不打断用户正在听的正文，但一定会被念到
    <p role="status" className="sr-only">
      {/* 新一轮开始时回到空内容：相邻两轮的文案可能一字不差，内容没变的 live region
          不会被重念——不先清空，下一轮结束时读屏就静默了 */}
      {phase.completed && <TurnAnnouncementText turnNumber={turnNumber} />}
    </p>
  );
};
