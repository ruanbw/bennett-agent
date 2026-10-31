/**
 * `todo_write` 的专属展示：输入框上方的任务清单面板。
 *
 * 为什么是这个形态：清单是**持续存在**的工作状态，不是某条消息的附件。放进消息流
 * （无论通用折叠行还是 standalone 渲染）都会被后续消息顶上去——数据一多用户得滚回
 * 去看。放在输入框正上方（与审批卡片同位），一轮对话里任何时刻抬头就能看到进度，
 * 这也是 DSH 工具面板（审批 / 澄清）的既有位置，不引入新的界面语言。
 *
 * 数据源：从会话消息里**倒序**找最近一次参数可解析的 `todo_write` 调用，取它的整表
 * 快照（工具本身是整表替换语义，最新一次调用就是当前状态）。最新一次是空清单 =
 * 用户清空了计划，面板随之消失，而不是回显旧清单。
 *
 * 消息流里的 tool-call 部件注册为 standalone 渲染 null：流内不留痕迹，避免同一份
 * 数据出现两个入口。thread.aui 的 `"standalone-tool-call": []` 分组路径配套。
 */
"use client";

import { useAuiState, useAssistantToolUI } from "@assistant-ui/react";
import {
  ChevronDownIcon,
  CircleCheckIcon,
  CircleIcon,
  ListChecksIcon,
  LoaderIcon,
} from "lucide-react";
import { useState, type FC } from "react";
import { cn } from "@/lib/utils";

/** 与服务端 `app/tools/todo.py` 的 TodoStatus 对齐。 */
type TodoStatus = "pending" | "in_progress" | "completed";

interface ParsedTodo {
  content: string;
  status: TodoStatus;
}

/**
 * 解析参数里的清单快照。
 *
 * 与服务端 `normalize_todos` 同口径：逐项校验，任何一项不合法整体放弃——半份清单
 * 会让人误以为另一部分任务被丢掉了。返回 null 表示不可展示。
 */
export function parseTodoArgs(
  argsText: string | null | undefined,
): { todos: ParsedTodo[]; empty: boolean } | null {
  if (!argsText) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(argsText);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const raw = (parsed as { todos?: unknown }).todos;
  if (!Array.isArray(raw)) return null;

  const todos: ParsedTodo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const content = (item as { content?: unknown }).content;
    const status = (item as { status?: unknown }).status;
    if (typeof content !== "string") return null;
    // 与服务端 `_normalize_content` 同口径：折叠所有空白串为单个空格（多行内容会破坏行渲染）。
    const normalized = content.split(/\s+/).filter(Boolean).join(" ");
    if (normalized === "") return null;
    if (status !== "pending" && status !== "in_progress" && status !== "completed") return null;
    todos.push({ content: normalized, status });
  }
  return { todos, empty: todos.length === 0 };
}

/**
 * 从会话消息里取**当前**清单：倒序找第一条参数可解析的 `todo_write` 调用。
 *
 * - 找到的调用是空清单 → 返回 `{ todos: [], empty: true }`（明确清空，面板隐藏）；
 * - 没有任何可解析的调用 → 返回 null（本会话没用过清单工具）。
 *
 * 兼容两种形状：运行时状态 `message.parts` 与外部存储转换前的 `message.content`。
 */
export function deriveLatestTodos(
  messages: readonly unknown[],
): { todos: ParsedTodo[]; empty: boolean } | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i] as { parts?: unknown[]; content?: unknown[] } | undefined;
    const parts = Array.isArray(message?.parts) ? message.parts : Array.isArray(message?.content) ? message.content : [];
    for (let j = parts.length - 1; j >= 0; j -= 1) {
      const part = parts[j] as { type?: unknown; toolName?: unknown; argsText?: unknown } | undefined;
      if (part?.type !== "tool-call" || part.toolName !== "todo_write") continue;
      const parsed = parseTodoArgs(typeof part.argsText === "string" ? part.argsText : null);
      if (parsed) return parsed;
    }
  }
  return null;
}

/** 折叠行的进度摘要：`X/Y 已完成`，进行中再点名第一条活跃任务（与 DSH 的 planSummary 一致）。 */
export function todoSummary(todos: readonly ParsedTodo[]): string {
  const done = todos.filter((t) => t.status === "completed").length;
  const active = todos.filter((t) => t.status === "in_progress");
  const head = `${done}/${todos.length} 已完成`;
  const first = active[0]?.content;
  return first ? `${head} · ${first}` : head;
}

/** 状态 → 行首图标。 */
const STATUS_ICONS: Record<TodoStatus, React.ElementType> = {
  pending: CircleIcon,
  in_progress: LoaderIcon,
  completed: CircleCheckIcon,
};

/** 清单本体：状态用图标 + 颜色 + 删除线三通道表达，色弱也能读。 */
const TodoChecklist: FC<{ todos: readonly ParsedTodo[] }> = ({ todos }) => (
  <ul data-slot="todo-list" className="aui-todo-list flex flex-col gap-1">
    {todos.map((todo, index) => {
      const Icon = STATUS_ICONS[todo.status];
      return (
        <li
          key={index}
          data-status={todo.status}
          data-slot="todo-item"
          className="aui-todo-item flex items-start gap-2 text-sm"
        >
          <Icon
            data-slot="todo-item-icon"
            aria-hidden="true"
            className={cn(
              "mt-[3px] size-3.5 shrink-0",
              todo.status === "completed" && "text-emerald-600 dark:text-emerald-400",
              todo.status === "in_progress" &&
                "animate-spin text-foreground/80 [animation-duration:0.6s]",
              todo.status === "pending" && "text-muted-foreground/50",
            )}
          />
          <span
            data-slot="todo-item-label"
            className={cn(
              "min-w-0 leading-relaxed",
              todo.status === "completed" && "text-muted-foreground/70 line-through",
              todo.status === "in_progress" && "font-medium text-foreground",
              todo.status === "pending" && "text-muted-foreground",
            )}
          >
            {todo.content}
          </span>
        </li>
      );
    })}
  </ul>
);

/** 展开态清单的限高：约 8 行，超出滚动，不把输入区顶出屏幕。 */
const PANEL_LIST_MAX_HEIGHT = "16rem";

/**
 * 任务清单面板：注册 null 渲染器（流内不重复展示）+ 在输入框上方渲染当前清单。
 *
 * 默认收起成一行进度摘要；点击展开完整清单（限高滚动）。清单被清空或本会话从未
 * 使用过时整体不渲染。
 */
export function TodoToolUI() {
  // 消息流里的 todo_write 部件不再渲染任何东西：唯一入口是这个面板。
  useAssistantToolUI({
    toolName: "todo_write",
    render: () => null,
    display: "standalone",
  });

  const messages = useAuiState((s) => s.thread.messages);
  const latest = deriveLatestTodos(messages);
  const [open, setOpen] = useState(false);

  if (!latest || latest.empty) return null;

  const active = latest.todos.find((t) => t.status === "in_progress");

  return (
    <div
      data-slot="todo-panel"
      data-open={open}
      className="t-acc border-border/60 bg-background mb-2 rounded-xl border px-3 py-2 shadow-xs"
    >
      <button
        type="button"
        data-slot="todo-panel-trigger"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "flex w-full cursor-pointer items-center gap-2 rounded-md pb-0 text-left text-sm transition-colors",
          "hover:text-secondary-foreground focus-visible:ring-1 focus-visible:ring-ring/50 focus-visible:outline-none",
          open ? "text-foreground" : "text-muted-foreground",
        )}
      >
        <ListChecksIcon
          aria-hidden="true"
          className={cn(
            "size-4 shrink-0",
            active ? "text-primary" : "text-emerald-600 dark:text-emerald-400",
          )}
        />
        <span className="shrink-0 font-medium">任务清单</span>
        <span
          data-slot="todo-panel-summary"
          className={cn("min-w-0 flex-1 truncate text-[13px]", active && "shimmer motion-reduce:animate-none")}
        >
          {todoSummary(latest.todos)}
        </span>
        <ChevronDownIcon
          aria-hidden="true"
          className="t-acc-chevron size-3.5 shrink-0"
        />
      </button>
      {/*
        高度动画走 grid-template-rows 0fr↔1fr（transitions-dev 的 accordion 模式）：
        不用 JS 量高，展开/收起都是连续过渡。触发的 padding 全放在这条轨道内部
        （`pt-1.5` 在内层），不放在触发按钮上——按钮 padding 随 open 一帧生效，
        会把面板外的输入区先顶一下再开始高度动画，那就是「展开瞬间跳一下」的来源。
      */}
      <div data-slot="todo-panel-content-wrap" data-open={open} className="t-acc-panel">
        <div className="t-acc-panel-inner">
          <div
            data-slot="todo-panel-content"
            className="overflow-y-auto pt-1.5"
            style={{ maxHeight: PANEL_LIST_MAX_HEIGHT }}
          >
            <TodoChecklist todos={latest.todos} />
          </div>
        </div>
      </div>
    </div>
  );
}
