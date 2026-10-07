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
  CheckIcon,
  CopyIcon,
  RefreshCwIcon,
  SquareIcon,
} from "lucide-react";
import type { FC } from "react";
import { ModelSelector } from "@/components/ModelSelector";
import { ReasoningEffortSelector } from "@/components/ReasoningEffortSelector";

const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 && !s.thread.isLoading;

interface ThreadProps {
  models?: any[];
  modelsError?: string | null;
  currentModel?: string | null;
  currentReasoningEffort?: string;
  onSelectModel?: (modelId: string) => void;
  onSelectReasoningEffort?: (effort: string) => void;
  onRefreshModels?: () => void;
  onSubmitClarificationAnswer?: (answer: string) => void;
}

export const Thread: FC<ThreadProps> = ({
  models = [],
  modelsError = null,
  currentModel = null,
  currentReasoningEffort = "medium",
  onSelectModel = () => {},
  onSelectReasoningEffort = () => {},
  onRefreshModels,
  onSubmitClarificationAnswer,
}) => {
  const isEmpty = useAuiState(isNewChatView);

  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root bg-background flex h-full flex-col w-full"
      style={{
        ["--thread-max-width" as string]: "46rem",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-6 pb-2",
            isEmpty && "justify-center",
          )}
        >
          {/* 空对话欢迎语 */}
          <AuiIf condition={isNewChatView}>
            <div className="flex flex-col items-center justify-center text-center my-auto pb-12 select-none">
              <h2 className="text-2xl font-semibold tracking-tight text-foreground mb-2">
                今天有什么可以帮您？
              </h2>
              <p className="text-sm text-muted-foreground max-w-sm">
                输入您的问题开始对话，所有数据保存在您本地浏览器中。
              </p>
            </div>
          </AuiIf>

          {/* 消息流列表 */}
          <div className="mb-6 flex flex-col gap-y-6 empty:hidden">
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage onSubmitClarificationAnswer={onSubmitClarificationAnswer} />}
            </ThreadPrimitive.Messages>
          </div>

          {/* 吸底输入框区域：用渐隐替代硬边框，避免视觉上切断最后一条消息 */}
          <div className="bg-background sticky bottom-0 mt-auto relative pb-4 pt-3">
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
              currentReasoningEffort={currentReasoningEffort}
              onSelectModel={onSelectModel}
              onSelectReasoningEffort={onSelectReasoningEffort}
              onRefreshModels={onRefreshModels}
            />
          </div>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

const ThreadMessage: FC<{ onSubmitClarificationAnswer?: (answer: string) => void }> = ({
  onSubmitClarificationAnswer,
}) => {
  const role = useAuiState((s) => s.message.role);
  if (role === "user") return <UserMessage />;
  return <AssistantMessage onSubmitClarificationAnswer={onSubmitClarificationAnswer} />;
};

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="滚动到底部"
        variant="outline"
        className="absolute -top-12 z-10 self-center rounded-full p-2.5 bg-background shadow-md border hover:bg-muted disabled:invisible"
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
  currentReasoningEffort?: string;
  onSelectModel: (id: string) => void;
  onSelectReasoningEffort?: (effort: string) => void;
  onRefreshModels?: () => void;
}> = ({
  models,
  modelsError = null,
  currentModel,
  currentReasoningEffort = "medium",
  onSelectModel,
  onSelectReasoningEffort = () => {},
  onRefreshModels,
}) => {
  const selectedModelObj = models.find((m) => m.id === currentModel);
  const isReasoningSupported = selectedModelObj ? selectedModelObj.reasoning : true;
  const supportedLevels = selectedModelObj?.supported_reasoning_levels || [];
  return (
    <ComposerPrimitive.Root className="relative flex w-full flex-col">
      <div className="flex w-full flex-col rounded-2xl border border-border bg-muted/20 focus-within:border-ring/50 focus-within:bg-background transition-all shadow-sm">
        <ComposerAttachments />
        <ComposerPrimitive.Input
          placeholder="问点什么吧..."
          className="max-h-48 min-h-12 w-full resize-none bg-transparent px-4 pt-3 pb-2 text-sm leading-relaxed outline-none placeholder:text-muted-foreground/60"
          rows={1}
          autoFocus
          aria-label="输入消息"
        />

        {/* 输入框底部工具条：模型选择器与思考等级在左侧，发送/停止在右侧 */}
        <div className="flex items-center justify-between px-3 pb-2.5 pt-1">
          <div className="flex items-center gap-2">
            {(models.length > 0 || modelsError) && (
              <ModelSelector
                models={models}
                modelsError={modelsError}
                currentModel={currentModel}
                onSelectModel={onSelectModel}
                onRefreshModels={onRefreshModels}
              />
            )}
            {isReasoningSupported && (
              <ReasoningEffortSelector
                supportedLevels={supportedLevels}
                currentEffort={currentReasoningEffort}
                onSelectEffort={onSelectReasoningEffort}
              />
            )}
          </div>

          {/* 发送/停止合并为同一个位置：生成中显示停止，空闲时显示发送 */}
          <div className="flex items-center gap-1.5">
            <AuiIf condition={(s) => s.thread.isRunning}>
              <ComposerPrimitive.Cancel asChild>
                <Button
                  type="button"
                  variant="destructive"
                  size="icon"
                  className="size-8 rounded-xl cursor-pointer"
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
                  className="size-8 rounded-xl cursor-pointer"
                  aria-label="发送消息"
                >
                  <ArrowUpIcon className="size-4" />
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

const ReasoningMessage: FC<{ text?: string; status?: { type?: string } }> = (props) => {
  // 历史消息把思考耗时持久化在 metadata.custom.thinkingMs
  const elapsedMs = useAuiState((s) => {
    const value = s.message.metadata.custom?.thinkingMs;
    return typeof value === "number" ? value : 0;
  });

  return (
    <ReasoningView
      text={props.text}
      isStreaming={props.status?.type === "running"}
      elapsedMs={elapsedMs}
    />
  );
};

import { AskClarificationTool } from "@/components/assistant-ui/elements/ask-clarification-tool";

const AssistantMessage: FC<{ onSubmitClarificationAnswer?: (answer: string) => void }> = ({
  onSubmitClarificationAnswer,
}) => {
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
            Reasoning: (props) => <ReasoningMessage {...props} />,
            Text: () => <MarkdownText />,
            tools: {
              by_name: {
                ask_user_clarification: (props) => (
                  <AskClarificationTool
                    {...props}
                    onSubmitAnswer={onSubmitClarificationAnswer}
                  />
                ),
              },
              Fallback: (props) => <ToolFallback {...props} />,
            },
          }}
        />
        <MessageError />
      </div>

      {stopped && (
        <div className="text-muted-foreground/70 mt-1 flex items-center gap-1.5 text-[11px]">
          <SquareIcon className="size-2.5 fill-current" />
          生成已停止
        </div>
      )}

      {/* 操作栏容器保持恒定高度 min-h-7，避免出现/消失时导致消息卡片高度变化产生抖动 */}
      <div className="flex items-center min-h-7 mt-1">
        <AssistantActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: FC = () => {
  const isLast = useAuiState((s) => s.message.isLast);

  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="never"
      className={cn(
        "text-muted-foreground flex gap-1 text-xs transition-opacity duration-200",
        isLast
          ? "opacity-100 pointer-events-auto"
          : "opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto focus-within:opacity-100 focus-within:pointer-events-auto"
      )}
    >
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton tooltip="复制回答" className="size-7 p-0">
          <AuiIf condition={(s) => s.message.isCopied}>
            <CheckIcon className="size-3.5 text-foreground" />
          </AuiIf>
          <AuiIf condition={(s) => !s.message.isCopied}>
            <CopyIcon className="size-3.5" />
          </AuiIf>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload asChild>
        <TooltipIconButton tooltip="重新生成" className="size-7 p-0">
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
      className="flex flex-col justify-end w-full pt-8"
    >
      <div className="flex justify-end w-full">
        <UserMessageAttachments />
        <div className="bg-muted text-foreground max-w-[85%] rounded-2xl rounded-tr-xs px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
          <MessagePrimitive.Parts
            components={{
              Text: ({ text }) => <span>{text}</span>,
            }}
          />
        </div>
      </div>
    </MessagePrimitive.Root>
  );
};
