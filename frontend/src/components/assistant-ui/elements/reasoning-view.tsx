import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ArrowDown, ChevronDown, Loader2 } from "lucide-react";
import { useDeferredValue, useState, useEffect, useRef } from "react";
import { useDetailLevel } from "@/context/detail-level-context";
import { thinkingSeconds, thinkingStartTime } from "@/lib/reasoning-time";
import { isNearScrollBottom } from "@/lib/scroll-follow";

interface ReasoningViewProps {
  text?: string;
  isStreaming?: boolean;
  /** 持久化的思考耗时（毫秒） */
  elapsedMs?: number;
  /**
   * 本轮思考开始的时间戳（仅生成中的内存消息有）。
   *
   * 切到别的会话再回来时组件会重挂载；没有它就只能从 0 重新计时。
   */
  startedAt?: number | null;
}

/**
 * 思考过程：自己也是**一条折叠条**（`已思考（15s）`），默认收起。
 *
 * 它嵌在「过程」块里面：外层那一行是「这一轮做了什么」的摘要，点开后才看到
 * 「想了多久 / 想了什么」。所以这里**不能**默认铺开原文——否则外层刚收好的东西
 * 一点开就变成一大段灰色思考，工具调用反而被挤到下面。
 *
 * 展开与否跟设置里的「对话显示」走（简洁收起 / 详细展开）；用户点过以自己为准。
 */
export function ReasoningView({
  text,
  isStreaming = false,
  elapsedMs = 0,
  startedAt = null,
}: ReasoningViewProps) {
  const { level } = useDetailLevel();
  const [userToggled, setUserToggled] = useState<boolean | null>(null);
  const [liveElapsed, setLiveElapsed] = useState(0);
  const [prevStreaming, setPrevStreaming] = useState(isStreaming);
  // 是否跟随最新思考。用户往上翻后关掉，避免每个增量都把他拽回底部。
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 思考文本每个增量都会整串重渲染：推后一帧渲染，让输入与滚动先跟上
  const deferredText = useDeferredValue(text);

  // 新一轮思考开始时：重置计时并恢复跟随（折叠状态由用户/模式决定，不在流式时强行摊开）
  if (prevStreaming !== isStreaming) {
    setPrevStreaming(isStreaming);
    if (isStreaming) {
      setLiveElapsed(0);
      setFollow(true);
    }
  }

  // 默认收起：思考是「过程」里的细节，不该默认占据阅读视线。
  // 只有「详细」档才默认摊开。
  const isOpen = userToggled ?? level === "detailed";
  const settledElapsed = thinkingSeconds(elapsedMs);
  const elapsed = isStreaming ? liveElapsed : settledElapsed > 0 ? settledElapsed : liveElapsed;

  // 思考计时器
  useEffect(() => {
    if (!isStreaming) return undefined;

    const start = thinkingStartTime(startedAt, elapsedMs);
    const timer = setInterval(() => {
      setLiveElapsed(thinkingSeconds(Date.now() - start));
    }, 500);
    return () => clearInterval(timer);
  }, [isStreaming, elapsedMs, startedAt]);

  // 流式输出时自动吸底；用户已经往上翻（follow=false）就不再抢滚动位置
  useEffect(() => {
    if (!isStreaming || !isOpen || !follow) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [deferredText, isStreaming, isOpen, follow]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const near = isNearScrollBottom(el);
    setFollow((prev) => (prev === near ? prev : near));
  };

  const resumeFollow = () => {
    setFollow(true);
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  if (!text && !isStreaming) return null;

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={setUserToggled}
      className="w-full select-none"
    >
      <CollapsibleTrigger
        render={
          <button
            type="button"
            className="group/thought inline-flex items-center gap-1.5 py-1 text-xs text-muted-foreground/80 hover:text-foreground transition-colors cursor-pointer rounded-md focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
          />
        }
      >
        {isStreaming ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
        ) : (
          <ChevronDown
            className={cn(
              "size-3.5 shrink-0 text-muted-foreground/70 transition-transform duration-200 group-hover/thought:text-foreground",
              isOpen ? "rotate-0" : "-rotate-90"
            )}
          />
        )}
        <span className="font-medium tracking-tight">
          {isStreaming ? "思考中" : "已思考"}
          {elapsed > 0 && <span className="ml-0.5 tabular-nums font-mono text-[11px]">（{elapsed}s）</span>}
        </span>
      </CollapsibleTrigger>

      {/* base-ui 的 Panel 只挂 data-open/data-closed，不是 Radix 的 data-state */}
      <CollapsibleContent className="transition-all data-closed:animate-collapsible-up data-open:animate-collapsible-down">
        <div className="mt-1 ml-1 pl-3 border-l-2 border-border/70">
          <div
            ref={scrollRef}
            role="group"
            aria-label="思考过程"
            tabIndex={0}
            onScroll={handleScroll}
            className="max-h-64 overflow-y-auto pr-2 text-xs leading-relaxed text-muted-foreground/85 whitespace-pre-wrap select-text font-sans"
          >
            {deferredText || "思考中..."}
          </div>
          {isStreaming && !follow && (
            <div className="mt-1 flex justify-end">
              <button
                type="button"
                onClick={resumeFollow}
                className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[10px] font-medium text-primary transition-colors hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 cursor-pointer"
              >
                <ArrowDown className="size-3" aria-hidden="true" />
                回到最新思考
              </button>
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
