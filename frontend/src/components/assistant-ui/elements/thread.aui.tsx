import {
  ComposerAttachments,
  UserMessageAttachments,
} from "@/components/assistant-ui/elements/attachment.aui";
import { ReasoningView } from "@/components/assistant-ui/elements/reasoning-view";
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text";
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  Brain,
  CheckIcon,
  CopyIcon,
  GlobeIcon,
  RefreshCwIcon,
  Sparkles,
  SquareIcon,
} from "lucide-react";
import type { FC } from "react";
import { ModelSelector } from "@/components/ModelSelector";

const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 && !s.thread.isLoading;

interface ThreadProps {
  models?: any[];
  modelsError?: string | null;
  currentModel?: string | null;
  webSearch?: boolean;
  onSelectModel?: (modelId: string) => void;
  onToggleWebSearch?: (checked: boolean) => void;
  onRefreshModels?: () => void;
}

export const Thread: FC<ThreadProps> = ({
  models = [],
  modelsError = null,
  currentModel = null,
  webSearch = false,
  onSelectModel = () => {},
  onToggleWebSearch,
  onRefreshModels,
}) => {
  const isEmpty = useAuiState(isNewChatView);

  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root bg-background flex h-full flex-col w-full"
      style={{
        ["--thread-max-width" as string]: "48rem",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4 pb-2 transition-all duration-300",
            isEmpty && "justify-center max-w-[44rem] my-auto",
          )}
        >
          {/* DeepSeek 经典首屏：未开始对话时居中大标题与引导提示 */}
          <AuiIf condition={isNewChatView}>
            <div className="flex flex-col items-center justify-center text-center pb-8 select-none animate-in fade-in zoom-in-95 duration-300">
              <div className="flex items-center justify-center size-14 rounded-2xl bg-gradient-to-br from-sky-500/20 via-blue-600/10 to-indigo-600/20 border border-sky-500/20 mb-4 shadow-sm">
                <Sparkles className="size-7 text-sky-400 animate-pulse" />
              </div>
              <h1 className="text-3xl font-semibold tracking-tight text-foreground mb-2">
                今天有什么可以帮您？
              </h1>
              <p className="text-xs text-muted-foreground max-w-md leading-relaxed">
                全功能 AI 深度思考模型已就绪，支持深度推理过程与实时联网检索。
              </p>
            </div>
          </AuiIf>

          {/* 消息流列表 */}
          <div className="mb-6 flex flex-col gap-y-6 empty:hidden">
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
            </ThreadPrimitive.Messages>
          </div>

          {/* 输入框区域：空闲时在中间，对话开始后平滑吸底 */}
          <div
            className={cn(
              "bg-background transition-all duration-200 relative",
              isEmpty ? "w-full" : "sticky bottom-0 mt-auto pb-4 pt-3",
            )}
          >
            {!isEmpty && (
              <div
                aria-hidden="true"
                className="pointer-events-none absolute inset-x-0 -top-6 h-6 bg-gradient-to-t from-background to-transparent"
              />
            )}
            <ThreadScrollToBottom />
            <Composer
              models={models}
              modelsError={modelsError}
              currentModel={currentModel}
              webSearch={webSearch}
              onSelectModel={onSelectModel}
              onToggleWebSearch={onToggleWebSearch}
              onRefreshModels={onRefreshModels}
            />
          </div>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

const ThreadMessage: FC = () => {
  const role = useAuiState((s) => s.message.role);
  if (role === "user") return <UserMessage />;
  return <AssistantMessage />;
};

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="滚动到底部"
        variant="outline"
        className="absolute -top-12 z-10 self-center rounded-full p-2.5 bg-background/90 shadow-md border hover:bg-muted disabled:invisible backdrop-blur-sm"
      >
        <ArrowDownIcon size={14} />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const Composer: FC<{
  models: any[];
  modelsError?: string | null;
  currentModel: string | null;
  webSearch?: boolean;
  onSelectModel: (id: string) => void;
  onToggleWebSearch?: (checked: boolean) => void;
  onRefreshModels?: () => void;
}> = ({
  models,
  modelsError = null,
  currentModel,
  webSearch = false,
  onSelectModel,
  onToggleWebSearch,
  onRefreshModels,
}) => {
  const currentModelData = models.find((m) => m.id === currentModel);
  const isReasoningModel = currentModelData?.reasoning ?? true;

  return (
    <ComposerPrimitive.Root className="relative flex w-full flex-col">
      <div className="flex w-full flex-col rounded-2xl border border-border/80 bg-muted/20 hover:border-border focus-within:border-sky-500/60 focus-within:ring-2 focus-within:ring-sky-500/10 focus-within:bg-background/95 transition-all shadow-sm">
        <ComposerAttachments />
        <ComposerPrimitive.Input
          placeholder="问点什么吧..."
          className="max-h-48 min-h-14 w-full resize-none bg-transparent px-4.5 pt-3.5 pb-2 text-sm leading-relaxed outline-none placeholder:text-muted-foreground/60 select-text"
          rows={1}
          autoFocus
          aria-label="输入消息"
        />

        {/* DeepSeek 风格底部工具条：双胶囊功能键（深度思考 + 联网搜索）+ 模型选择 + 发送/停止 */}
        <div className="flex items-center justify-between px-3 pb-3 pt-1 border-t border-transparent">
          <div className="flex items-center gap-2 flex-wrap">
            {/* 模型选择器 */}
            {(models.length > 0 || modelsError) && (
              <ModelSelector
                models={models}
                modelsError={modelsError}
                currentModel={currentModel}
                onSelectModel={onSelectModel}
                onRefreshModels={onRefreshModels}
              />
            )}

            {/* 深度思考 胶囊开关 */}
            <button
              type="button"
              title={
                isReasoningModel
                  ? "当前模型支持深度思考"
                  : "当前模型为通用快速模型"
              }
              className={cn(
                "flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition cursor-pointer select-none shadow-xs",
                isReasoningModel
                  ? "border-sky-500/40 bg-sky-500/10 text-sky-400 shadow-sky-500/5"
                  : "border-border/60 bg-muted/30 text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              <Brain
                size={13}
                className={cn(
                  "shrink-0 transition-colors",
                  isReasoningModel ? "text-sky-400" : "text-muted-foreground"
                )}
              />
              <span className="text-xs font-medium tracking-tight">深度思考</span>
              {isReasoningModel && (
                <span className="size-1.5 rounded-full bg-sky-400" />
              )}
            </button>

            {/* 联网搜索 胶囊开关 */}
            <button
              type="button"
              onClick={() => onToggleWebSearch?.(!webSearch)}
              title={webSearch ? "联网搜索已启用（点击关闭）" : "联网搜索已关闭（点击开启）"}
              aria-pressed={webSearch}
              className={cn(
                "flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition cursor-pointer select-none shadow-xs",
                webSearch
                  ? "border-sky-500/40 bg-sky-500/10 text-sky-400 shadow-sky-500/5"
                  : "border-border/60 bg-muted/30 text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              <GlobeIcon
                size={13}
                className={cn(
                  "shrink-0 transition-colors",
                  webSearch ? "text-sky-400" : "text-muted-foreground"
                )}
              />
              <span className="text-xs font-medium tracking-tight">联网搜索</span>
              {webSearch && (
                <span className="size-1.5 rounded-full bg-sky-400 animate-pulse" />
              )}
            </button>
          </div>

          {/* 发送 / 停止 组合键 */}
          <div className="flex items-center gap-1.5 shrink-0 pl-2">
            <AuiIf condition={(s) => s.thread.isRunning}>
              <ComposerPrimitive.Cancel asChild>
                <Button
                  type="button"
                  variant="destructive"
                  size="icon"
                  className="size-8 rounded-xl cursor-pointer hover:scale-105 active:scale-95 transition-transform"
                  title="停止生成"
                  aria-label="停止生成"
                >
                  <SquareIcon className="size-3.5 fill-current" />
                </Button>
              </ComposerPrimitive.Cancel>
            </AuiIf>

            <AuiIf condition={(s) => !s.thread.isRunning}>
              <ComposerPrimitive.Send asChild>
                <TooltipIconButton
                  tooltip="发送消息"
                  side="top"
                  type="button"
                  variant="default"
                  size="icon"
                  className="size-8 rounded-xl cursor-pointer bg-sky-500 hover:bg-sky-600 text-white disabled:bg-muted disabled:text-muted-foreground disabled:opacity-50 hover:scale-105 active:scale-95 transition-all"
                  aria-label="发送消息"
                >
                  <ArrowUpIcon className="size-4 stroke-[2.5]" />
                </TooltipIconButton>
              </ComposerPrimitive.Send>
            </AuiIf>
          </div>
        </div>
      </div>
    </ComposerPrimitive.Root>
  );
};

const MessageError: FC = () => {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root className="mt-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
        <ErrorPrimitive.Message className="line-clamp-2" />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};

const ReasoningMessage: FC<{ text?: string; status?: { type?: string } }> = ({
  text,
  status,
}) => {
  // 历史消息把思考耗时持久化在 metadata.custom.thinkingMs
  const elapsedMs = useAuiState((s) => {
    const value = s.message.metadata.custom?.thinkingMs;
    return typeof value === "number" ? value : 0;
  });

  return (
    <ReasoningView
      text={text}
      isStreaming={status?.type === "running"}
      elapsedMs={elapsedMs}
    />
  );
};

const AssistantMessage: FC = () => {
  const stopped = useAuiState((s) => s.message.metadata.custom?.stopped === true);

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="group flex flex-col gap-1 w-full"
    >
      <div className="text-foreground text-sm leading-relaxed wrap-break-word">
        <MessagePrimitive.Parts
          components={{
            Empty: ({ status }) =>
              status.type === "running" ? (
                <div className="flex items-center gap-1.5 py-2 px-1 text-xs text-muted-foreground select-none">
                  <div className="flex items-center gap-1">
                    <span className="size-1.5 rounded-full bg-sky-400 animate-bounce [animation-delay:-0.3s]" />
                    <span className="size-1.5 rounded-full bg-sky-400 animate-bounce [animation-delay:-0.15s]" />
                    <span className="size-1.5 rounded-full bg-sky-400 animate-bounce" />
                  </div>
                  <span className="text-[11px] font-medium text-muted-foreground/80 ml-1">
                    思考中...
                  </span>
                </div>
              ) : null,
            Reasoning: (props) => <ReasoningMessage {...props} />,
            Text: () => <MarkdownText />,
            tools: {
              Fallback: (props) => <ToolFallback {...props} />,
            },
          }}
        />
        <MessageError />
      </div>

      {stopped && (
        <div className="text-muted-foreground/60 mt-1 flex items-center gap-1.5 text-[11px] select-none">
          <SquareIcon className="size-2.5 fill-current" />
          生成已停止
        </div>
      )}

      {/* 操作栏：悬浮时浮现，提供复制与重新生成 */}
      <div className="flex items-center mt-1">
        <AssistantActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1 text-xs pt-1"
    >
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton
          tooltip="复制回答"
          className="size-7 p-0 rounded-md hover:bg-muted hover:text-foreground text-muted-foreground transition"
        >
          <AuiIf condition={(s) => s.message.isCopied}>
            <CheckIcon className="size-3.5 text-sky-400" />
          </AuiIf>
          <AuiIf condition={(s) => !s.message.isCopied}>
            <CopyIcon className="size-3.5" />
          </AuiIf>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload asChild>
        <TooltipIconButton
          tooltip="重新生成"
          className="size-7 p-0 rounded-md hover:bg-muted hover:text-foreground text-muted-foreground transition"
        >
          <RefreshCwIcon className="size-3.5" />
        </TooltipIconButton>
      </ActionBarPrimitive.Reload>
    </ActionBarPrimitive.Root>
  );
};

const UserMessage: FC = () => {
  return (
    <MessagePrimitive.Root
      data-slot="aui_user-message-root"
      data-role="user"
      className="flex justify-end w-full"
    >
      <UserMessageAttachments />
      <div className="bg-muted/80 text-foreground max-w-[85%] rounded-2xl rounded-tr-xs px-4 py-2.5 text-sm leading-relaxed wrap-break-word shadow-xs border border-border/40 select-text">
        <MessagePrimitive.Parts
          components={{
            Text: ({ text }) => <span>{text}</span>,
          }}
        />
      </div>
    </MessagePrimitive.Root>
  );
};
