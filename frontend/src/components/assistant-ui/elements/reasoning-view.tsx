import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ArrowDown, ChevronDown, Loader2 } from "lucide-react";
import { useDeferredValue, useState, useEffect, useRef } from "react";
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

export function ReasoningView({
  text,
  isStreaming = false,
  elapsedMs = 0,
  startedAt = null,
}: ReasoningViewProps) {
  const [userToggled, setUserToggled] = useState<boolean | null>(null);
  const [liveElapsed, setLiveElapsed] = useState(0);
  const [prevStreaming, setPrevStreaming] = useState(isStreaming);
  // 是否跟随最新思考。用户往上翻后关掉，避免每个增量都把他拽回底部。
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 思考文本每个增量都会整串重渲染：推后一帧渲染，让输入与滚动先跟上
  const deferredText = useDeferredValue(text);

  // 新一轮思考开始时：重置计时，流式中默认展开并恢复跟随
  if (prevStreaming !== isStreaming) {
    setPrevStreaming(isStreaming);
    if (isStreaming) {
      setUserToggled(null);
      setLiveElapsed(0);
      setFollow(true);
    }
  }

  // 流式中默认展开，思考结束后默认收起（与主流平台一致，也可以点开看完整思考过程）
  const isOpen = userToggled !== null ? userToggled : isStreaming;
  const settledElapsed = thinkingSeconds(elapsedMs);
  const elapsed = isStreaming ? liveElapsed : settledElapsed > 0 ? settledElapsed : liveElapsed;

  // 思考计时器
  useEffect(() => {
    if (!isStreaming) return;

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
      className="my-2 w-full transition-all select-none"
    >
      <CollapsibleTrigger
        render={
          <button
            type="button"
            className="group inline-flex items-center gap-1.5 py-1.5 text-xs text-muted-foreground/80 hover:text-foreground transition-colors cursor-pointer"
          />
        }
      >
        {isStreaming ? (
          <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
        ) : (
          <ChevronDown
            className={cn(
              "size-3.5 transition-transform duration-200 text-muted-foreground/70 group-hover:text-foreground",
              isOpen ? "rotate-0" : "-rotate-90"
            )}
          />
        )}

        <span className="font-medium tracking-tight">
          {isStreaming ? '思考中' : '已思考'}
          {elapsed > 0 && (
            <span className="ml-1 tabular-nums font-mono text-[11px] text-muted-foreground/70">
              ({elapsed}s)
            </span>
          )}
        </span>

        {isStreaming && (
          <ChevronDown
            className={cn(
              "size-3 transition-transform duration-200 text-muted-foreground/60 ml-0.5",
              isOpen ? "rotate-0" : "-rotate-90"
            )}
          />
        )}
      </CollapsibleTrigger>

      {/* data-[state=*] 是 Radix 的写法，base-ui 的 CollapsiblePanel 只挂 data-open/data-closed
          （见 @base-ui/react/collapsible/panel/CollapsiblePanelDataAttributes），原类名永远不匹配 */}
      <CollapsibleContent className="transition-all data-closed:animate-collapsible-up data-open:animate-collapsible-down">
        <div className="mt-1.5 ml-1 pl-3 border-l-2 border-border/70">
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
