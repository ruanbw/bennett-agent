import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronDown, Loader2 } from "lucide-react";
import { useState, useEffect, useRef } from "react";

const toSeconds = (ms: number) => (ms > 0 ? Math.max(1, Math.round(ms / 1000)) : 0);

interface ReasoningViewProps {
  text?: string;
  isStreaming?: boolean;
  /** 持久化的思考耗时（毫秒） */
  elapsedMs?: number;
}

export function ReasoningView({ text, isStreaming = false, elapsedMs = 0 }: ReasoningViewProps) {
  const [userToggled, setUserToggled] = useState<boolean | null>(null);
  const [liveElapsed, setLiveElapsed] = useState(0);
  const [prevStreaming, setPrevStreaming] = useState(isStreaming);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 新一轮思考开始时：重置计时，流式中默认展开
  if (prevStreaming !== isStreaming) {
    setPrevStreaming(isStreaming);
    if (isStreaming) {
      setUserToggled(null);
      setLiveElapsed(0);
    }
  }

  // 流式中默认展开，思考结束后默认收起（与主流平台一致，也可以点开看完整思考过程）
  const isOpen = userToggled !== null ? userToggled : isStreaming;
  const elapsed = isStreaming ? liveElapsed : elapsedMs > 0 ? toSeconds(elapsedMs) : liveElapsed;

  // 思考计时器
  useEffect(() => {
    if (!isStreaming) return;

    const start = Date.now() - elapsedMs;
    const timer = setInterval(() => {
      setLiveElapsed(Math.max(1, Math.floor((Date.now() - start) / 1000)));
    }, 500);
    return () => clearInterval(timer);
  }, [isStreaming, elapsedMs]);

  // 流式输出时思考内容自动平滑吸底
  useEffect(() => {
    if (isStreaming && isOpen && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [text, isStreaming, isOpen]);

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
            className="group inline-flex items-center gap-1.5 py-1 text-xs text-muted-foreground/80 hover:text-foreground transition-colors cursor-pointer"
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
          思考中
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

      <CollapsibleContent className="transition-all data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down">
        <div className="mt-1.5 ml-1 pl-3 border-l-2 border-border/70">
          <div
            ref={scrollRef}
            className="max-h-64 overflow-y-auto pr-2 text-xs leading-relaxed text-muted-foreground/85 whitespace-pre-wrap select-text font-sans"
          >
            {text || "思考中..."}
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
