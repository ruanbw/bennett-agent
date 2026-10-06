import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { Brain, ChevronDown, Sparkles } from "lucide-react";
import { useState, useEffect, useRef } from "react";

const toSeconds = (ms: number) => (ms > 0 ? Math.max(1, Math.round(ms / 1000)) : 0);

interface ReasoningViewProps {
  text?: string;
  isStreaming?: boolean;
  /** 持久化的思考耗时（毫秒），历史消息用它显示"已深度思考 (Ns)" */
  elapsedMs?: number;
}

export function ReasoningView({ text, isStreaming = false, elapsedMs = 0 }: ReasoningViewProps) {
  const [userToggled, setUserToggled] = useState<boolean | null>(null);
  const [liveElapsed, setLiveElapsed] = useState(0);
  const [prevStreaming, setPrevStreaming] = useState(isStreaming);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 新一轮思考（含"重新生成"复用同一组件）开始时：重置计时、恢复自动展开
  if (prevStreaming !== isStreaming) {
    setPrevStreaming(isStreaming);
    if (isStreaming) {
      setUserToggled(null);
      setLiveElapsed(0);
    }
  }

  const isOpen = userToggled !== null ? userToggled : isStreaming;
  // 流式中用实时计时；结束后优先用持久化的最终耗时
  const elapsed = isStreaming ? liveElapsed : elapsedMs > 0 ? toSeconds(elapsedMs) : liveElapsed;

  // 思考计时：仅在流式期间走动
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
      className="my-2.5 w-full overflow-hidden rounded-xl border border-border/60 bg-muted/30 transition-all select-none"
    >
      <CollapsibleTrigger
        render={
          <button
            type="button"
            className="flex w-full items-center justify-between px-3.5 py-2 text-xs font-medium text-muted-foreground hover:bg-muted/50 hover:text-foreground transition cursor-pointer"
          />
        }
      >
        <div className="flex items-center gap-2">
          {isStreaming ? (
            <div className="relative flex items-center justify-center size-3.5 text-foreground">
              <Brain className="size-3.5 animate-pulse" />
              <span className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-emerald-500 animate-ping" />
            </div>
          ) : (
            <Sparkles className="size-3.5 text-muted-foreground" />
          )}

          <span className="tabular-nums">
            {isStreaming ? (
              <span className="font-medium text-foreground flex items-center gap-1.5">
                Thinking...
                <span className="text-[11px] font-mono text-muted-foreground">({elapsed}s)</span>
              </span>
            ) : (
              <span>Thought for {elapsed > 0 ? `${elapsed}s` : '<1s'}</span>
            )}
          </span>
        </div>

        <div className="flex items-center gap-1 text-[11px] text-muted-foreground/80">
          <span>{isOpen ? 'Collapse' : 'Expand'}</span>
          <ChevronDown
            className={cn(
              'size-3.5 transition-transform duration-200',
              isOpen && 'rotate-180'
            )}
          />
        </div>
      </CollapsibleTrigger>

      <CollapsibleContent className="transition-all data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down">
        <div
          ref={scrollRef}
          className="max-h-60 overflow-y-auto border-t border-border/40 bg-background/50 px-4 py-3 font-mono text-xs leading-relaxed text-muted-foreground/90 whitespace-pre-wrap select-text"
        >
          {text || 'Thinking...'}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
