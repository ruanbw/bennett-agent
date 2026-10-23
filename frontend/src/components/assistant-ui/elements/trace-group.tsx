import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useAuiState } from "@assistant-ui/react";
import { useDetailLevel } from "@/context/detail-level-context";
import { cn } from "@/lib/utils";
import { ChevronDown, Loader2, SquareTerminal } from "lucide-react";
import { type FC, type ReactNode, useState } from "react";
import { describeToolActions, traceToggleLabel } from "@/lib/trace-summary";
import { resolveToolCallViewStatus } from "@/lib/tool-call-view";
import { thinkingSeconds } from "@/lib/reasoning-time";

/**
 * 「过程」折叠块：把**思考**与**工具调用**一起收成一行，外面只留最终回答。
 *
 * 这一层是整条消息的分界线：折叠条以上的东西（想了多久、调了什么工具、读了哪些文件）
 * 都属于「怎么得出答案」，不是答案本身。收进同一个可点目标后，一轮回答在页面上只剩
 * 两样东西——一行过程摘要，和正文。
 *
 * 此前思考与每次工具调用各自带折叠头，结果是：
 *   - 「已思考（1s）」单独挂在外面，明明属于过程却和正文平级；
 *   - 连着调三个工具就是三条独立横条，看不出它们属于同一段推理。
 *
 * 默认展开与否来自设置里的「对话显示」（简洁收起 / 详细展开）；生成中始终展开——
 * 那时用户要的是「它在动」的反馈。用户手动点过之后以自己的选择为准。
 */
export const TraceGroup: FC<{
  /** 组内部件下标（来自 `MessagePrimitive.GroupedParts`）。 */
  indices: readonly number[];
  /** 组内各状态的计数（来自 `GroupedParts` 的 counts）。 */
  counts: { running: number; complete: number; incomplete: number; requiresAction: number };
  children: ReactNode;
}> = ({ indices, counts, children }) => {
  const { level } = useDetailLevel();
  const [userToggled, setUserToggled] = useState<boolean | null>(null);

  // 历史消息把思考耗时持久化在 metadata.custom.thinkingMs；生成中的消息另有
  // thinkingStartedAt。折叠条上的「已思考 Ns」用它。
  const elapsedMs = useAuiState((s) => {
    const value = s.message.metadata.custom?.thinkingMs;
    return typeof value === "number" ? value : 0;
  });
  const parts = useAuiState((s) => s.message.parts);

  const selected = indices.map((index) => parts[index]).filter(Boolean);
  const hasReasoning = selected.some((part) => part.type === "reasoning");
  const toolNames = selected
    .filter((part) => part.type === "tool-call")
    .map((part) => part.toolName);
  // 失败/被拒绝/中断都算「出了问题」：折叠着也要能看出这轮不顺利，
  // 不能只靠展开后逐个看图标
  const failedCount = selected.filter((part) => {
    if (part.type !== "tool-call") return false;
    const viewStatus = resolveToolCallViewStatus(part.status, part.result);
    return viewStatus === "failed" || viewStatus === "denied" || viewStatus === "interrupted";
  }).length;

  const running = counts.running > 0;
  const seconds = thinkingSeconds(elapsedMs);

  const segments: string[] = [];
  if (hasReasoning) segments.push(seconds > 0 ? `思考 ${seconds}s` : "思考中");
  if (toolNames.length > 0) segments.push(describeToolActions(toolNames));
  if (failedCount > 0) segments.push(`${failedCount} 个失败`);
  const summary = segments.join("，") || (running ? "正在处理…" : "过程");

  // 默认**始终收起**：连生成中也不自动摊开。用户要的是「只看到最终回答」，
  // 生成期间自己会点开看进度；强行展开会让每轮对话都被一段过程占满。
  const isOpen = userToggled ?? level === "detailed";

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={setUserToggled}
      className="my-1.5 w-full select-none"
    >
      <CollapsibleTrigger
        render={
          <button
            type="button"
            aria-label={traceToggleLabel(summary, isOpen)}
            className="group/trace inline-flex w-fit max-w-full items-center gap-1.5 rounded-md py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 cursor-pointer"
          />
        }
      >
        {running ? (
          <Loader2 className="size-4 shrink-0 animate-spin text-muted-foreground" />
        ) : (
          <SquareTerminal
            className={cn(
              "size-4 shrink-0 text-muted-foreground/70",
              failedCount > 0 && "text-amber-600 dark:text-amber-400"
            )}
            aria-hidden="true"
          />
        )}
        <span className="truncate">{summary}</span>
        <ChevronDown
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground/60 transition-transform duration-200",
            isOpen ? "rotate-0" : "-rotate-90"
          )}
          aria-hidden="true"
        />
      </CollapsibleTrigger>

      {/* base-ui 的 Panel 只挂 data-open/data-closed，不是 Radix 的 data-state */}
      <CollapsibleContent className="transition-all data-closed:animate-collapsible-up data-open:animate-collapsible-down">
        <div className="mt-1 mb-1 ml-1 flex flex-col gap-1 border-l-2 border-border/70 pl-3">
          {children}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};
