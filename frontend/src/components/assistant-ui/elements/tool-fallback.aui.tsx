"use client";

import { memo, useMemo, useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  GlobeIcon,
  Loader2Icon,
  SearchIcon,
  WrenchIcon,
  AlertCircleIcon,
} from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { cn } from "@/lib/utils";

interface WebSearchResultItem {
  id: number;
  title: string;
  url: string;
  content: string;
  domain: string;
}

/** 解析 Tavily 返回的文本内容为结构化引用源列表 */
function parseSearchResults(rawResult: string): {
  answer: string | null;
  items: WebSearchResultItem[];
} {
  if (!rawResult || typeof rawResult !== "string") {
    return { answer: null, items: [] };
  }

  let answer: string | null = null;
  const items: WebSearchResultItem[] = [];

  // 提取快速摘要
  const answerMatch = rawResult.match(/【快速摘要】:\s*([\s\S]*?)(?=\n【|$)/);
  if (answerMatch && answerMatch[1]) {
    answer = answerMatch[1].trim();
  }

  // 提取搜索结果项: 数字. 标题 \n 链接: URL \n 内容: Content
  const blockRegex = /(\d+)\.\s*([^\n]+)\n\s*链接:\s*([^\n]+)\n\s*内容:\s*([\s\S]*?)(?=(?:\n\d+\.|$))/g;
  let match;
  while ((match = blockRegex.exec(rawResult)) !== null) {
    const id = parseInt(match[1], 10);
    const title = match[2].trim();
    const url = match[3].trim();
    const content = match[4].trim();

    let domain = "";
    try {
      const parsed = new URL(url);
      domain = parsed.hostname.replace(/^www\./, "");
    } catch {
      domain = url;
    }

    items.push({
      id,
      title,
      url,
      content,
      domain,
    });
  }

  return { answer, items };
}

/** 解析参数中的搜索关键词 */
function extractQueryFromArgs(argsText?: string): string {
  if (!argsText) return "";
  try {
    const parsed = JSON.parse(argsText);
    return parsed.query || parsed.q || parsed.search_query || argsText;
  } catch {
    return argsText;
  }
}

export const ToolFallback = memo(function ToolFallback({
  toolName,
  argsText,
  result,
  status,
}: ToolCallMessagePartProps) {
  const [isOpen, setIsOpen] = useState(false);
  const isSearchTool = toolName === "tavily_search" || toolName.includes("search");
  const isRunning = status.type === "running";
  const isError = status.type === "incomplete" || (status as any).error;

  const query = useMemo(() => extractQueryFromArgs(argsText), [argsText]);
  const parsedData = useMemo(() => {
    if (typeof result === "string") return parseSearchResults(result);
    return { answer: null, items: [] };
  }, [result]);

  // 通用非搜索工具展示
  if (!isSearchTool) {
    return (
      <div className="my-2 rounded-xl border border-border/60 bg-muted/20 text-xs overflow-hidden">
        <div
          onClick={() => setIsOpen((prev) => !prev)}
          className="flex items-center justify-between px-3.5 py-2 cursor-pointer hover:bg-muted/40 transition select-none"
        >
          <div className="flex items-center gap-2">
            {isRunning ? (
              <Loader2Icon className="size-3.5 animate-spin text-sky-400" />
            ) : isError ? (
              <AlertCircleIcon className="size-3.5 text-destructive" />
            ) : (
              <WrenchIcon className="size-3.5 text-muted-foreground" />
            )}
            <span className="font-medium text-foreground">
              {isRunning ? `正在调用工具: ${toolName}...` : `已调用工具: ${toolName}`}
            </span>
          </div>
          <ChevronDownIcon
            className={cn("size-3.5 text-muted-foreground transition-transform", isOpen && "rotate-180")}
          />
        </div>

        {isOpen && (
          <div className="p-3 border-t border-border/40 font-mono text-[11px] bg-background/50 space-y-2">
            {argsText && (
              <div>
                <span className="text-muted-foreground">参数: </span>
                <pre className="overflow-x-auto whitespace-pre-wrap text-foreground/80">{argsText}</pre>
              </div>
            )}
            {result !== undefined && (
              <div>
                <span className="text-muted-foreground">结果: </span>
                <pre className="overflow-x-auto whitespace-pre-wrap text-foreground/80 max-h-48">
                  {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // DeepSeek 经典风格联网搜索卡片
  return (
    <div className="my-2.5 w-full rounded-xl border border-sky-500/20 bg-sky-500/5 transition-all overflow-hidden text-xs select-none shadow-xs">
      {/* 头部状态条 */}
      <div
        onClick={() => !isRunning && setIsOpen((prev) => !prev)}
        className={cn(
          "flex items-center justify-between px-3.5 py-2.5 transition",
          !isRunning && "cursor-pointer hover:bg-sky-500/10"
        )}
      >
        <div className="flex items-center gap-2">
          {isRunning ? (
            <div className="relative flex items-center justify-center size-4 text-sky-400">
              <SearchIcon className="size-3.5 animate-pulse" />
              <span className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-sky-400 animate-ping" />
            </div>
          ) : (
            <div className="flex items-center justify-center size-4 rounded-full bg-sky-500/15 text-sky-400">
              <CheckIcon className="size-2.5 stroke-[3]" />
            </div>
          )}

          <div className="flex items-center gap-1.5 flex-wrap">
            {isRunning ? (
              <span className="text-foreground font-medium flex items-center gap-1.5">
                <span>正在检索网络</span>
                {query && (
                  <span className="text-sky-400 font-mono text-[11px] max-w-[200px] truncate">
                    "{query}"
                  </span>
                )}
                <span className="size-1 rounded-full bg-sky-400 animate-ping" />
              </span>
            ) : (
              <span className="text-muted-foreground font-medium flex items-center gap-1.5">
                <span className="text-foreground">已检索网络</span>
                {query && (
                  <span className="text-sky-400/90 font-mono text-[11px] max-w-[220px] truncate">
                    "{query}"
                  </span>
                )}
                <span className="text-[11px] font-mono text-muted-foreground/70">
                  ({parsedData.items.length > 0 ? `${parsedData.items.length} 篇参考来源` : "完成"})
                </span>
              </span>
            )}
          </div>
        </div>

        {!isRunning && (
          <div className="flex items-center gap-1 text-[11px] text-muted-foreground/80">
            <span>{isOpen ? "收起来源" : "查看来源"}</span>
            <ChevronDownIcon
              className={cn(
                "size-3.5 transition-transform duration-200 text-muted-foreground/70",
                isOpen && "rotate-180"
              )}
            />
          </div>
        )}
      </div>

      {/* 来源列表展示：水平/网格卡片排列 */}
      {(!isRunning && isOpen && parsedData.items.length > 0) && (
        <div className="p-3 border-t border-sky-500/15 bg-background/50 space-y-2 animate-in fade-in duration-150">
          {parsedData.answer && (
            <div className="p-2.5 rounded-lg bg-sky-500/10 border border-sky-500/20 text-xs text-foreground/90 leading-relaxed mb-2">
              <span className="text-sky-400 font-semibold mr-1">检索摘要:</span>
              {parsedData.answer}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {parsedData.items.map((item) => (
              <a
                key={item.id}
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                title={`${item.title}\n${item.content}`}
                className="group flex flex-col justify-between p-2.5 rounded-lg border border-border/60 bg-muted/20 hover:bg-sky-500/10 hover:border-sky-500/40 transition cursor-pointer select-text"
              >
                <div className="flex items-start justify-between gap-1.5 mb-1.5">
                  <span className="font-medium text-foreground text-xs line-clamp-1 group-hover:text-sky-400 transition-colors">
                    {item.title}
                  </span>
                  <ExternalLinkIcon className="size-3 text-muted-foreground shrink-0 opacity-60 group-hover:opacity-100 group-hover:text-sky-400 transition" />
                </div>

                <p className="text-[11px] text-muted-foreground line-clamp-2 leading-relaxed mb-1.5">
                  {item.content}
                </p>

                <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground/70 group-hover:text-sky-400/80">
                  <GlobeIcon className="size-2.5 shrink-0" />
                  <span className="truncate">{item.domain}</span>
                </div>
              </a>
            ))}
          </div>
        </div>
      )}
    </div>
  );
});
