import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { usePresentationPolicy } from "@/context/detail-level-context";
import { cn } from "@/lib/utils";
import { ArrowDown, Brain, Loader2 } from "lucide-react";
import { useDeferredValue, useEffect, useRef, useState } from "react";
import { thinkingSeconds, thinkingStartTime } from "@/lib/reasoning-time";
import { isNearScrollBottom } from "@/lib/scroll-follow";
import { reasoningPreview } from "@/lib/trace-summary";

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
 * 思考行：**一行**，不是一块折叠面板。
 *
 * 对齐 DSH 的 `ReasoningRow`：思考图标 + 「思考」+ 分隔点 + 一行预览摘要。
 * 点开后是正文（次要字号、左内边距 22px 对齐到标题文字，而不是对齐到图标）。
 *
 * 为什么把「已思考（15s）」改掉：耗时是「这一轮花了多久」的一部分，现在挂在整轮的
 * 时长行上；思考行自己只需要说「这里有一段思考」，以及它大概在讲什么。
 *
 * 预览只在「标准 / 详细 / 完全展开」出现（`settledReasoningPreview`）；「简洁」档
 * 只留「思考」两个字——那一档的整个主张就是「过程别占地方」。
 */
export function ReasoningView({
  text,
  isStreaming = false,
  elapsedMs = 0,
  startedAt = null,
}: ReasoningViewProps) {
  const policy = usePresentationPolicy();
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
  // 「详细」档同样收起——那一档展开的是**过程组**，不是每一段思考正文。
  const isOpen = userToggled ?? false;
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

  // 运行中取「最后一个已完成段落的首行」，已结束取首行；没有可用预览时为空串
  const preview = reasoningPreview(text, isStreaming);
  /*
    收起时才需要预览：展开后正文就在下面，再顶一行摘要只是重复。

    简洁档**连运行中也不给预览**（只有「思考」两个字）：那一档的整个主张是「过程别
    占地方」，而流式过程中的预览还在不停变化——它既占地方又抢注意力。DSH 在简洁档下
    运行中仍会显示预览，这里按本项目的四档定义收得更紧。
  */
  const showPreview = !isOpen && policy.settledReasoningPreview && preview !== "";

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={setUserToggled}
      className="flex w-full flex-col select-none"
    >
      {/*
        单行：图标 + 「思考」 + 分隔点 + 预览 + 耗时。
        `py-0.5` 让整行高约 24px（DSH 的 24px 行高），和工具行对齐。

        `w-full` 是必需的：耗时用 `ml-auto` 钉在行尾，得先有一条满宽的 flex 行可推。
        之前是 `inline-flex`（宽度跟着内容长），耗时只能紧贴在预览后面，于是
        「预览多长」直接决定了耗时的横向位置。
      */}
      <CollapsibleTrigger
        render={
          <button
            type="button"
            className="group/thought flex w-full cursor-pointer items-center gap-1.5 rounded-md py-0.5 text-left text-sm text-muted-foreground transition-colors hover:text-secondary-foreground focus-visible:ring-1 focus-visible:ring-ring/50 focus-visible:outline-none"
          />
        }
      >
        {isStreaming ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden="true" />
        ) : (
          <Brain className="size-3.5 shrink-0" aria-hidden="true" />
        )}
        <span className="shrink-0">思考</span>
        {/* 分隔点：2×2 的圆角方块，caption 色。没有预览时整组隐藏 */}
        {showPreview && (
          <span
            className="mx-0.5 inline-block size-0.5 shrink-0 rounded-[1px] bg-muted-foreground/40"
            aria-hidden="true"
          />
        )}
        {showPreview && (
          <span className="min-w-0 flex-1 truncate text-[13px] text-muted-foreground/80">
            <span className={cn(isStreaming && "shimmer motion-reduce:animate-none")}>{preview}</span>
          </span>
        )}
        {/*
          耗时**始终**渲染并固定在行尾（`ml-auto` + `shrink-0`）：展开、收起、不同
          预览长度下都在同一个位置。此前只在展开时渲染，且紧贴「思考」二字，于是
          一点开耗时就从行尾跳到左边——那正是「位置不对」。
          没有耗时数据时整段不渲染，不显示「0s」。
        */}
        {elapsed > 0 && (
          <span className="ml-auto shrink-0 pl-2 font-mono text-[11px] tabular-nums text-muted-foreground/60">
            {elapsed}s
          </span>
        )}
      </CollapsibleTrigger>

      {/* base-ui 的 Panel 只挂 data-open/data-closed，不是 Radix 的 data-state */}
      <CollapsibleContent>
        {/*
          正文：次要字号、次要色。左内边距 22px = 图标(14) + gap(6) + 2px 微调，
          让正文左缘与「思考」两个字对齐，而不是与图标对齐——读者是在读正文，
          不是在读图标列。
        */}
        <div className="py-1 pl-[22px] pr-0">
          <div
            ref={scrollRef}
            role="group"
            aria-label="思考过程"
            tabIndex={0}
            onScroll={handleScroll}
            className="max-h-72 overflow-y-auto text-[13px] leading-relaxed whitespace-pre-wrap text-muted-foreground select-text [scrollbar-gutter:stable]"
          >
            {deferredText || "思考中…"}
          </div>
          {isStreaming && !follow && (
            <div className="mt-1 flex justify-end">
              <button
                type="button"
                onClick={resumeFollow}
                className="inline-flex cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 text-[10px] font-medium text-primary transition-colors hover:bg-primary/10 focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none"
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
