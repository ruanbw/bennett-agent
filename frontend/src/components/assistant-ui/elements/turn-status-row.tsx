import { AlertTriangleIcon, SquareIcon } from "lucide-react";
import { type FC } from "react";

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
