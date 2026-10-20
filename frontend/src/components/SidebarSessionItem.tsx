import { memo, type ReactNode } from 'react';
import { Loader2, MoreHorizontal, Pin, PinOff, Edit2, Trash2 } from 'lucide-react';

import type { SessionRecord } from '../types';
import { highlightRanges, type ContentMatch, type TextRange } from '../lib/session-search';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';

/** 触发可能返回 Promise 的回调并接住 rejection：不产生 unhandled rejection。 */
function trigger(action: () => void | Promise<unknown>, label: string): void {
  void Promise.resolve(action()).catch((err: unknown) => console.error(`${label}:`, err));
}

const EMPTY_TOKENS: readonly string[] = [];
const EMPTY_RANGES: TextRange[] = [];

/** 把纯文本按高亮区间切片渲染；没有区间时原样输出（不产生额外节点）。 */
function HighlightedText({ text, ranges }: { text: string; ranges: TextRange[] }) {
  if (ranges.length === 0) return <>{text}</>;

  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start > cursor) nodes.push(text.slice(cursor, range.start));
    nodes.push(
      <mark
        key={`${range.start}-${range.end}`}
        className="rounded-[3px] bg-amber-400/25 text-inherit"
      >
        {text.slice(range.start, range.end)}
      </mark>,
    );
    cursor = range.end;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return <>{nodes}</>;
}

export interface SidebarSessionItemProps {
  session: SessionRecord;
  isActive: boolean;
  isRunning: boolean;
  isSelected: boolean;
  isSelectMode: boolean;
  isEditing: boolean;
  editingTitle: string;
  /** 当前搜索词（标题高亮用）。空数组 = 没在搜索。 */
  searchTokens?: readonly string[];
  /** 正文命中信息：有值时标题下方多一行片段。 */
  contentMatch?: ContentMatch | null;
  onEditingTitleChange: (title: string) => void;
  onStartEdit: (session: SessionRecord) => void;
  onSaveEdit: (sessionId: string) => void;
  onCancelEdit: () => void;
  onSelect: (id: string) => void;
  onToggleSelect: (id: string) => void;
  onTogglePin: (id: string) => void | Promise<unknown>;
  onRequestDelete: (session: SessionRecord) => void;
}

/**
 * 侧边栏单个会话条目 — 参照 DeepSeek 极简风格。
 *
 * - 去掉左侧图标噪音，只保留文字标题；hover 时右侧浮现三点操作菜单
 * - 运行中的会话左侧展示小转圈 Loader2；置顶只用小 Pin 角标
 *
 * ## 可达性约定（别改回去）
 *
 * - **整行是一个真正的 `<button>`（绝对定位铺满），不是 `div onClick`**。此前用 div，
 *   键盘用户按 Tab 永远进不到会话列表，读屏也读不出「这是一条可选中的会话」。
 *   内容层 `pointer-events-none`，让点击穿透到下面这层按钮；三点菜单用更高 z-index
 *   保持可点。这样按钮里没有嵌套的可交互元素，语义是干净的。
 * - **搜索命中要看得见。** 标题里的搜索词用 `<mark>` 高亮；正文命中时标题下方多一行
 *   片段（片段自带高亮区间，与当前输入无关也能自洽）。片段是视觉信息，所以行名
 *   （`aria-label`）也要带上「内容命中：…」，否则读屏用户无法解释这条会话为何出现在
 *   搜索结果里。
 * - **多选模式复用 `<button role="checkbox">`**：原生 Enter/Space 即触发 click，
 *   `aria-checked` 把选中态交给读屏，不需要再手写键盘处理。
 * - **操作菜单不能只在 hover 时出现**。Tailwind 4 的 `hover:` / `group-hover:` 会被包进
 *   `@media (hover: hover)`，触屏设备上永远不生效——而容器同时挂了 `pointer-events-none`，
 *   于是「重命名 / 置顶 / 删除」在手机上彻底点不到。除了 hover，还要在
 *   `focus-within`（键盘）与 `pointer-coarse`（触屏）下显示。
 */
export const SidebarSessionItem = memo(function SidebarSessionItem({
  session,
  isActive,
  isRunning,
  isSelected,
  isSelectMode,
  isEditing,
  editingTitle,
  searchTokens = EMPTY_TOKENS,
  contentMatch = null,
  onEditingTitleChange,
  onStartEdit,
  onSaveEdit,
  onCancelEdit,
  onSelect,
  onToggleSelect,
  onTogglePin,
  onRequestDelete,
}: SidebarSessionItemProps) {
  const isPinned = Boolean(session.isPinned);
  const title = session.title || '新对话';
  const titleRanges = searchTokens.length > 0 ? highlightRanges(title, searchTokens) : EMPTY_RANGES;
  const snippetRanges = contentMatch ? contentMatch.snippet.ranges : EMPTY_RANGES;

  const handleActivate = () => {
    if (isSelectMode) onToggleSelect(session.id);
    else onSelect(session.id);
  };

  // 读屏听到的名字要带上「正在生成 / 已置顶」这类只能靠视觉看到的状态
  const stateSuffix = [isPinned ? '已置顶' : '', isRunning ? '正在生成' : '']
    .filter(Boolean)
    .join('，');
  // 正文命中是视觉信息（标题下一行小字），读屏必须能从行名里听到，否则「为什么这个会话
  // 会出现在搜索结果里」对读屏用户完全不可解释。片段过长时截断，避免行名变成一段正文。
  const snippetLabel = contentMatch ? contentMatch.snippet.text.slice(0, 40) : '';
  const baseLabel = stateSuffix ? `${title}（${stateSuffix}）` : title;
  const rowLabel = snippetLabel ? `${baseLabel}，内容命中：${snippetLabel}` : baseLabel;

  return (
    <div
      className={`group relative flex items-center gap-2 px-3 py-2.5 rounded-xl text-[13px] transition-colors ${
        isSelected
          ? 'bg-accent/80 text-accent-foreground font-medium'
          : isActive
            ? 'bg-accent/70 text-foreground font-medium'
            : 'text-foreground/80 hover:bg-accent/40'
      }`}
    >
      {/*
        铺满整行的点击/聚焦层。编辑态下撤掉：输入框自己接收指针，且一行里不该同时有
        两个可聚焦的「主体」。
      */}
      {!isEditing && (
        <button
          type="button"
          onClick={handleActivate}
          aria-label={rowLabel}
          aria-current={!isSelectMode && isActive ? 'true' : undefined}
          role={isSelectMode ? 'checkbox' : undefined}
          aria-checked={isSelectMode ? isSelected : undefined}
          className="absolute inset-0 z-0 cursor-pointer rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:ring-inset"
        />
      )}

      {/* 内容层：不接收指针，点击穿透到下面的整行按钮。
          非编辑态对读屏隐藏：整行按钮已经用 aria-label 报过标题与状态，留着文本节点
          会让读屏把同一个标题念两遍。 */}
      <div
        aria-hidden={isEditing ? undefined : true}
        className="pointer-events-none relative z-10 flex min-w-0 flex-1 items-center gap-2"
      >
        {isRunning && (
          <Loader2 size={14} className="shrink-0 animate-spin text-foreground/60" aria-hidden="true" />
        )}

        {isEditing ? (
          <input
            type="text"
            value={editingTitle}
            onChange={(event) => onEditingTitleChange(event.target.value)}
            onBlur={() => onSaveEdit(session.id)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                onSaveEdit(session.id);
              }
              if (event.key === 'Escape') {
                event.preventDefault();
                onCancelEdit();
              }
            }}
            aria-label="重命名会话"
            autoFocus
            className="pointer-events-auto flex-1 rounded-lg border border-ring/40 bg-background px-2 py-0.5 text-[13px] text-foreground outline-none focus:ring-1 focus:ring-ring/40"
          />
        ) : (
          <div className="min-w-0 flex-1">
            <span className="block truncate select-none">
              <HighlightedText text={title} ranges={titleRanges} />
            </span>
            {contentMatch && (
              <span className="mt-0.5 block truncate text-[11px] leading-4 text-muted-foreground/80 select-none">
                <HighlightedText text={contentMatch.snippet.text} ranges={snippetRanges} />
              </span>
            )}
          </div>
        )}

        {isPinned && !isEditing && !isSelectMode && (
          <Pin
            size={10}
            className="shrink-0 fill-muted-foreground/30 text-muted-foreground/50 rotate-45"
            aria-hidden="true"
          />
        )}
      </div>

      {/*
        操作菜单：hover / 键盘焦点 / 触屏（coarse pointer）/ 当前会话四种情况都要可见。
        - group-focus-within：Tab 到整行按钮时就能看到入口，而不是先按 Enter 切过去；
        - pointer-coarse：触屏没有 hover，必须常显，否则手机上没有重命名/删除入口。
      */}
      {!isEditing && !isSelectMode && (
        <div
          className={`relative z-20 shrink-0 transition-opacity ${
            isActive
              ? 'opacity-100 pointer-events-auto'
              : 'opacity-0 pointer-events-none group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100 pointer-coarse:pointer-events-auto pointer-coarse:opacity-100'
          }`}
          onClick={(e) => e.stopPropagation()}
        >
          <DropdownMenu>
            <DropdownMenuTrigger
              className="size-6 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent rounded-md transition cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-ring"
              aria-label={`会话操作：${title}`}
            >
              <MoreHorizontal size={14} />
            </DropdownMenuTrigger>
            <DropdownMenuContent side="right" align="start" sideOffset={4}>
              <DropdownMenuItem onClick={() => onStartEdit(session)}>
                <Edit2 size={14} className="text-muted-foreground" />
                <span>重命名</span>
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => trigger(() => onTogglePin(session.id), '切换置顶失败')}
              >
                {isPinned ? (
                  <PinOff size={14} className="text-muted-foreground" />
                ) : (
                  <Pin size={14} className="text-muted-foreground" />
                )}
                <span>{isPinned ? '取消置顶' : '置顶对话'}</span>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={() => onRequestDelete(session)}
                className="text-destructive hover:bg-destructive/10 data-[highlighted]:bg-destructive/10 data-[highlighted]:text-destructive"
              >
                <Trash2 size={14} />
                <span>删除</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
    </div>
  );
});
