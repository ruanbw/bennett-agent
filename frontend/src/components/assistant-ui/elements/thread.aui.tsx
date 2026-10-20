import {
  ComposerAddAttachment,
  ComposerAttachments,
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
  type ReasoningMessagePartComponent,
  type TextMessagePartComponent,
  ThreadPrimitive,
  useAui,
  useAuiEvent,
  useAuiState,
} from "@assistant-ui/react";
import {
  AlertTriangleIcon,
  ArrowDownIcon,
  ArrowLeftRightIcon,
  ArrowUpIcon,
  CalendarClockIcon,
  CheckIcon,
  CopyIcon,
  FlaskConicalIcon,
  GitCompareArrowsIcon,
  LightbulbIcon,
  ListChecksIcon,
  PenLineIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  SquareIcon,
  type LucideIcon,
} from "lucide-react";
import { type FC, type RefObject, useCallback, useRef, useState } from "react";
import { ContextUsageIndicator } from "@/components/ContextUsageIndicator";
import { ModelSelector } from "@/components/ModelSelector";
import { ReasoningEffortSelector } from "@/components/ReasoningEffortSelector";
import { ClarificationInputPopup } from "@/components/assistant-ui/elements/clarification-input-popup";
import { useClarification } from "@/context/clarification-context";
import { contextExhausted } from "@/lib/chat-message";
import { readUsage } from "@/lib/context-usage";
import { formatMessageTime, formatMessageTimeFull } from "@/lib/message-time";
import { levelsFor, shouldShowEffortSelector } from "@/lib/reasoning-effort";
import {
  availableStartSuggestions,
  pickStartSuggestions,
  type StartSuggestionIcon,
} from "@/lib/start-suggestions";
import type { FallbackState, MessageCustomMetadata, ModelItem, RunError } from "@/types";

/**
 * 起步建议的图标映射。文案与能力裁剪规则在 `lib/start-suggestions`：那是纯数据与
 * 纯函数，能在 node 环境的 vitest 里直接覆盖；组件只负责把图标名落到具体组件。
 */
const SUGGESTION_ICONS: Record<StartSuggestionIcon, LucideIcon> = {
  search: SearchIcon,
  compare: GitCompareArrowsIcon,
  checklist: ListChecksIcon,
  rewrite: PenLineIcon,
  concept: LightbulbIcon,
  calendar: CalendarClockIcon,
  test: FlaskConicalIcon,
};

const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 && !s.thread.isLoading;

const isThreadRunning = (s: AssistantState) => s.thread.isRunning;

/**
 * 「重新生成」只出现在最后一条助手消息上。
 *
 * assistant-ui 的 Reload 把被重生成消息的 parentId 交给适配层，但历史回答上摆这个按钮
 * 本身就是误导（用户会以为重跑的是那一条）。限制在最后一条之后，按钮语义与
 * ChatProvider 的落点判断就一致了。
 */
const isLastAssistantMessage = (s: AssistantState) => {
  const messages = s.thread.messages;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "assistant") return messages[i].id === s.message.id;
  }
  return false;
};

/** 本轮渲染不完整（丢过帧 / 流被中断）。字段缺席时行为不变：历史记录里本就没有它。 */
const isMessageIncomplete = (s: AssistantState) =>
  s.message.metadata.custom?.incomplete === true;

/**
 * 部件组件必须是模块级常量。
 *
 * `MessagePrimitive.Parts` 用**引用相等**判断 components 是否变化（见 @assistant-ui/core
 * 的比较器：`prev.components.Text === next.components.Text`），内联对象与箭头函数每次渲染
 * 都是新引用，于是任何一次重渲染都会整棵重挂 Markdown 子树——划走的选区被清掉、DOM 抖动。
 */
const AssistantTextPart: TextMessagePartComponent = () => <MarkdownText />;
const AssistantReasoningPart: ReasoningMessagePartComponent = ({ text, status }) => (
  <ReasoningMessage text={text} status={status} />
);
const ASSISTANT_PART_COMPONENTS = {
  Reasoning: AssistantReasoningPart,
  Text: AssistantTextPart,
  tools: { Fallback: ToolFallback },
};

const UserTextPart: TextMessagePartComponent = ({ text }) => <span>{text}</span>;
const USER_PART_COMPONENTS = {
  Text: UserTextPart,
};

interface ThreadProps {
  models?: ModelItem[];
  modelsError?: string | null;
  /** 服务端是否装配了联网搜索；决定起步建议里是否出现搜索示例。 */
  webSearchAvailable?: boolean;
  currentModel?: string | null;
  currentReasoningEffort?: string;
  onSelectModel?: (modelId: string) => void;
  onSelectReasoningEffort?: (effort: string) => void;
  onRefreshModels?: () => void | Promise<unknown>;
  /** 上下文已满时的出路：开一个新会话。由 ChatContainer 透传（App 提供）。 */
  onCreateSession?: () => void | Promise<unknown>;
}

export const Thread: FC<ThreadProps> = ({
  models = [],
  modelsError = null,
  webSearchAvailable = false,
  currentModel = null,
  currentReasoningEffort = "medium",
  onSelectModel = () => {},
  onSelectReasoningEffort = () => {},
  onRefreshModels,
  onCreateSession,
}) => {
  const isEmpty = useAuiState(isNewChatView);
  const isRunning = useAuiState(isThreadRunning);
  // 上下文已满：输入框锁住并给出「新建会话」。服务端每轮都会拒绝，这里只是不让用户
  // 白点一次发送，并把他直接送到唯一的出路（推导规则见 lib/chat-message.ts）。
  const contextLocked = useAuiState((s) => contextExhausted(s.thread.messages, currentModel));
  // 提问状态来自 context，不再由 ChatProvider 通过 cloneElement 注入
  const clarification = useClarification();

  /**
   * 发送后的焦点回到输入框。
   *
   * 不能在输入框上写 `autoFocus`：ChatProvider 以 `key={currentSessionId}` 重挂载，
   * 挂载即聚焦会让「切到历史会话」平白弹出软键盘、抢走焦点。而 run 启动只可能来自用户
   * 主动发送（新会话的首条消息也算），挂在 `thread.runStart` 上就同时保住了「发送完可以
   * 接着打字」的手感。
   */
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  useAuiEvent("thread.runStart", () => {
    composerInputRef.current?.focus({ preventScroll: true });
  });

  /** 空会话的起步建议：只填入输入框，**不自动发送**（发送会产生费用，也可能不是用户本意）。 */
  const aui = useAui();
  const applySuggestion = useCallback(
    (text: string) => {
      aui.composer.setText(text);
      composerInputRef.current?.focus({ preventScroll: true });
    },
    [aui],
  );

  /**
   * 建议批次。offset 只在「换一批」时前进；能力清单稍后返回（搜索变为可用）时池子
   * 变长，`pickStartSuggestions` 自己按当前长度取模，不需要重置。
   */
  const [suggestionOffset, setSuggestionOffset] = useState(0);
  const suggestions = pickStartSuggestions(webSearchAvailable, suggestionOffset);
  const hasMoreSuggestions =
    availableStartSuggestions(webSearchAvailable).length > suggestions.length;
  const cycleSuggestions = useCallback(() => {
    setSuggestionOffset((offset) => offset + suggestions.length);
  }, [suggestions.length]);

  /** 滚到底部按钮顺带把焦点交回输入框：去掉 autoFocus 后，这里是「点按钮接着打字」的唯一入口。 */
  const focusComposer = useCallback(() => {
    composerInputRef.current?.focus({ preventScroll: true });
  }, []);

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
        aria-busy={isRunning}
        className="relative flex flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-6 pb-2",
            isEmpty && "justify-center",
          )}
        >
          {/* 空对话欢迎语 + 起步建议：新用户不知道这个 agent 能做什么（联网搜索 / 澄清 /
              长上下文），给几条与真实工具能力对应的例子。搜索示例只在能力探针确认
              联网可用时出现；点击只填入，不直接发送。 */}
          <AuiIf condition={isNewChatView}>
            <div className="flex flex-col items-center justify-center text-center my-auto pb-12 select-none">
              <h2 className="text-2xl font-semibold tracking-tight text-foreground mb-2">
                今天有什么可以帮您？
              </h2>
              <p className="text-sm text-muted-foreground max-w-sm">
                输入您的问题开始对话，所有数据保存在您本地浏览器中。
              </p>

              <div className="mt-6 w-full max-w-md">
                <div role="group" aria-label="起步建议" className="grid grid-cols-2 gap-2">
                  {suggestions.map((suggestion) => {
                    const Icon = SUGGESTION_ICONS[suggestion.icon];
                    return (
                      <button
                        key={suggestion.id}
                        type="button"
                        onClick={() => applySuggestion(suggestion.text)}
                        className="group flex min-h-[3.25rem] cursor-pointer items-center gap-2 rounded-xl border border-border/70 bg-muted/30 px-3 py-2 text-left text-xs leading-relaxed text-foreground/80 transition hover:border-border hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
                      >
                        <Icon
                          className="size-3.5 shrink-0 text-muted-foreground/60 transition group-hover:text-primary"
                          aria-hidden="true"
                        />
                        <span>{suggestion.text}</span>
                      </button>
                    );
                  })}
                </div>
                <div className="mt-2 flex flex-wrap items-center justify-center gap-x-2 gap-y-1">
                  <p className="text-[11px] text-muted-foreground/70">
                    点击会填入输入框，可先修改再发送
                  </p>
                  {hasMoreSuggestions && (
                    <>
                      <span aria-hidden="true" className="text-[11px] text-muted-foreground/40">
                        ·
                      </span>
                      <button
                        type="button"
                        onClick={cycleSuggestions}
                        aria-label="换一批起步建议"
                        className="inline-flex cursor-pointer items-center gap-1 rounded text-[11px] text-muted-foreground/70 transition hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
                      >
                        <RefreshCwIcon className="size-3" aria-hidden="true" />
                        换一批
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          </AuiIf>

          {/* 消息流列表：role="log" 让读屏把它当对话日志（多轮追加，不是一条新页面） */}
          <div
            role="log"
            aria-live="polite"
            aria-relevant="additions text"
            className="mb-6 flex flex-col gap-y-6 empty:hidden"
          >
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
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
            <ThreadScrollToBottom onClick={focusComposer} />

            {/* 输入框正上方弹出的意图澄清浮层卡片：提交后即刻消失 */}
            {clarification.pending && (
              <ClarificationInputPopup
                clarification={clarification.pending}
                onSubmitAnswer={clarification.answer}
                onDismiss={clarification.dismiss}
              />
            )}

            <Composer
              inputRef={composerInputRef}
              models={models}
              modelsError={modelsError}
              currentModel={currentModel}
              currentReasoningEffort={currentReasoningEffort}
              onSelectModel={onSelectModel}
              onSelectReasoningEffort={onSelectReasoningEffort}
              onRefreshModels={onRefreshModels}
              contextLocked={contextLocked}
              onCreateSession={onCreateSession}
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

const ThreadScrollToBottom: FC<{ onClick?: () => void }> = ({ onClick }) => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="滚动到底部"
        variant="outline"
        onClick={onClick}
        // 靠右对齐输入框右边缘：之前是 self-center（居中），会盖在消息正文中轴线上
        className="absolute -top-12 right-4 z-10 rounded-full p-2.5 bg-background shadow-md border hover:bg-muted disabled:invisible"
      >
        <ArrowDownIcon size={14} />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const Composer: FC<{
  models: ModelItem[];
  modelsError?: string | null;
  currentModel: string | null;
  currentReasoningEffort?: string;
  onSelectModel: (id: string) => void;
  onSelectReasoningEffort?: (effort: string) => void;
  onRefreshModels?: () => void | Promise<unknown>;
  inputRef?: RefObject<HTMLTextAreaElement | null>;
  /** 本会话上下文已满：锁住输入框并给出「新建会话」按钮。 */
  contextLocked?: boolean;
  onCreateSession?: () => void | Promise<unknown>;
}> = ({
  models,
  modelsError = null,
  currentModel,
  currentReasoningEffort = "medium",
  onSelectModel,
  onSelectReasoningEffort = () => {},
  onRefreshModels,
  inputRef,
  contextLocked = false,
  onCreateSession,
}) => {
  const selectedModel = models.find((m) => m.id === currentModel);

  // 整个会话里最近一次回报的用量：上下文会不会被锁、占比多少，只跟最新那次请求有关。
  // 用量此前挂在每条回答下面，越往上越旧却长得像「当前上下文」，现在统一收进输入框。
  // selector 必须返回稳定引用，所以这里返回 metadata.custom 原始对象，校验交给 readUsage。
  const latestUsageMeta = useAuiState((s) => {
    for (let index = s.thread.messages.length - 1; index >= 0; index -= 1) {
      const custom = s.thread.messages[index].metadata.custom;
      if (custom && typeof custom.usage === "object" && custom.usage !== null) return custom;
    }
    return null;
  });
  const latestUsage = readUsage(latestUsageMeta?.usage);
  const latestUsageModel =
    typeof latestUsageMeta?.model === "string" ? latestUsageMeta.model : null;

  // 附件暂不支持（后端只接受文本）。有附件时直接禁用发送，
  // 使「贴了图然后被静默丢掉」不可能发生——只靠提示语容易被忽略。
  const attachmentCount = useAuiState((s) => s.composer.attachments.length);
  const blockedByAttachments = attachmentCount > 0;
  // 两个禁用理由共存时以上下文为准：附件可以移除，换模型/新会话不能省
  const blocked = contextLocked || blockedByAttachments;
  const inputPlaceholder = contextLocked ? "本会话上下文已满，请新建会话继续" : "问点什么吧...";
  const sendTooltip = contextLocked
    ? "本会话上下文已满，请新建会话"
    : blockedByAttachments
      ? "附件暂不支持，请先移除"
      : "发送消息";

  return (
    <ComposerPrimitive.Root
      className="relative flex w-full flex-col"
      onSubmit={(e) => {
        if (blocked) {
          e.preventDefault();
        }
      }}
    >
      {/*
        上下文已满：输入框还在，但不是可以继续提问的输入框。把它做成一条常驻横条而不是
        一次性 toast——「不能继续提问」是个持续状态，刷新后依旧成立（记录上的 error.code
        推导而来），而 toast 会消失、也不解释为什么发不出去。
      */}
      {contextLocked && (
        <div
          role="status"
          className="border-destructive/40 bg-destructive/10 text-destructive mb-2 flex flex-wrap items-center justify-between gap-2 rounded-xl border px-3 py-2 text-xs"
        >
          <span>本会话上下文已满，无法继续提问；请新建会话，或换用上下文窗口更大的模型。</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-6 shrink-0 cursor-pointer px-2 text-[11px]"
            onClick={() => void onCreateSession?.()}
          >
            <PlusIcon className="size-3" />
            新建会话
          </Button>
        </div>
      )}
      <div className="flex w-full flex-col rounded-2xl border border-border bg-muted/20 focus-within:border-ring/50 focus-within:bg-background transition-all shadow-sm">
        <ComposerAttachments />
        {/*
          刻意不写 autoFocus：挂载时的自动聚焦会在切历史会话（ChatProvider 按会话 id 重挂载）
          时弹出软键盘抢焦点。发送后的聚焦由 Thread 订阅 `thread.runStart` 完成。
        */}
        <ComposerPrimitive.Input
          ref={inputRef}
          placeholder={inputPlaceholder}
          disabled={contextLocked}
          className="max-h-48 min-h-12 w-full resize-none bg-transparent px-4 pt-3 pb-2 text-base leading-relaxed outline-none placeholder:text-muted-foreground/60 disabled:cursor-not-allowed disabled:opacity-60 md:text-sm"
          rows={1}
          aria-label="输入消息"
          onKeyDown={(e) => {
            if (blocked && e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
            }
          }}
        />

        {/* 输入框底部工具条：附件/模型选择器/思考等级在左侧，发送/停止在右侧 */}
        <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 px-3 pb-2.5 pt-1">
          <div className="flex min-w-0 items-center gap-2">
            <ComposerAddAttachment />
            {(models.length > 0 || modelsError) && (
              <ModelSelector
                models={models}
                modelsError={modelsError}
                currentModel={currentModel}
                onSelectModel={onSelectModel}
                onRefreshModels={onRefreshModels}
              />
            )}
            {shouldShowEffortSelector(selectedModel) && (
              <ReasoningEffortSelector
                levels={levelsFor(selectedModel)}
                currentEffort={currentReasoningEffort}
                onSelectEffort={onSelectReasoningEffort}
              />
            )}
          </div>

          {/* 发送/停止合并为同一个位置：生成中显示停止，空闲时显示发送 */}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {/* 上下文用量紧贴发送按钮左侧：它是「能不能继续问」的唯一依据，位置要固定。 */}
            <ContextUsageIndicator
              usage={latestUsage}
              usageModel={latestUsageModel}
              currentModel={currentModel}
              modelContextWindow={selectedModel?.context_window ?? null}
              locked={contextLocked}
            />
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
                  tooltip={sendTooltip}
                  side="top"
                  type="button"
                  variant="default"
                  size="icon"
                  disabled={blocked}
                  className="size-8 rounded-xl cursor-pointer"
                  aria-label={sendTooltip}
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

/**
 * `metadata.custom` 是运行时透传的任意形状：按鸭子类型取，字段缺失/写坏就当没有。
 *
 * 校验放在组件外而不是 `useAuiState` 的 selector 里：selector 必须返回稳定引用
 * （这里返回的是 metadata 上的原始值），在里面构造新对象会让快照每次都变。
 */
const readFallback = (value: unknown): FallbackState | null => {
  if (!value || typeof value !== "object") return null;
  const { fromModel, toModel } = value as Partial<FallbackState>;
  return typeof fromModel === "string" && typeof toModel === "string"
    ? { fromModel, toModel }
    : null;
};

/** 失败信息同理：只在结构完整时返回，否则当作没有（老记录/写坏的数据）。 */
const readError = (value: unknown): RunError | null => {
  if (!value || typeof value !== "object") return null;
  const { message, retryable, code } = value as Partial<RunError>;
  return typeof message === "string"
    ? { message, retryable: retryable === true, code: code === "context_overflow" ? code : null }
    : null;
};

const AssistantMessage: FC = () => {
  const stopped = useAuiState((s) => s.message.metadata.custom?.stopped === true);
  const incomplete = useAuiState(isMessageIncomplete);
  const fallback = readFallback(useAuiState((s) => s.message.metadata.custom?.fallback));
  const error = readError(useAuiState((s) => s.message.metadata.custom?.error));
  // 只给最后一条助手消息显示重试：重跑历史回答会覆盖别的轮次（与 Reload 的约束一致）
  const isLast = useAuiState(isLastAssistantMessage);
  // 上下文超限即使被标成可重试也不给按钮：重试的请求只会更长，必然再超一次
  const canRetry = isLast && error?.retryable === true && error.code !== "context_overflow";

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="group flex flex-col gap-1 w-full"
    >
      <div className="text-foreground text-sm leading-relaxed wrap-break-word">
        <MessagePrimitive.Parts components={ASSISTANT_PART_COMPONENTS} />
        <MessageError />
      </div>

      {incomplete && (
        <div className="text-destructive/80 mt-1 flex items-center gap-1.5 text-[11px]">
          <AlertTriangleIcon className="size-3" />
          回复不完整（已中断）
        </div>
      )}

      {stopped && (
        <div className="text-muted-foreground/70 mt-1 flex items-center gap-1.5 text-[11px]">
          <SquareIcon className="size-2.5 fill-current" />
          生成已停止
        </div>
      )}

      {fallback && (
        <div className="text-muted-foreground/70 mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
          <ArrowLeftRightIcon className="size-3" />
          已切换备用模型：{fallback.fromModel} → {fallback.toModel}
        </div>
      )}

      {canRetry && (
        <div className="mt-2 flex">
          <ActionBarPrimitive.Reload asChild>
            <button
              type="button"
              className="border-border/70 text-foreground hover:bg-muted focus-visible:ring-ring/50 inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs transition-colors focus-visible:ring-1 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50"
            >
              <RefreshCwIcon className="size-3.5" />
              重试
            </button>
          </ActionBarPrimitive.Reload>
        </div>
      )}

      {/* 操作栏容器保持恒定高度 min-h-7，避免出现/消失时导致消息卡片高度变化产生抖动 */}
      {/* 时间紧跟在操作按钮之后：和操作归为一组，而不是飘到容器最右侧 */}
      <div className="flex items-center min-h-7 mt-1 gap-1">
        <AssistantActionBar />
        <MessageTimestamp className="ml-1" />
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: FC = () => {
  const isLast = useAuiState((s) => s.message.isLast);
  // 重新生成只对最后一条助手消息开放：历史回答上的 Reload 会重跑别的那条
  const canReload = useAuiState(isLastAssistantMessage);
  // 上下文已满时不提供「重新生成」：历史只会更长，同一个模型下重跑必然同样失败。
  // 保留「复制」是对的——用户可能只想要刚才那段回答。
  const contextLocked = useAuiState(
    (s) => (s.message.metadata.custom as MessageCustomMetadata | undefined)?.error?.code === "context_overflow",
  );

  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="never"
      className={cn(
        "text-muted-foreground flex gap-1 text-xs transition-opacity duration-200",
        // 非最后一条回答：hover / 键盘焦点 / 触屏时可见。
        // 触屏必须单独写 pointer-coarse：Tailwind 4 的 group-hover 被包在
        // `@media (hover: hover)` 里，手机/平板上永远不生效，而这里还挂着
        // pointer-events-none——不补这一条，历史回答的「复制」在触屏上根本点不到。
        isLast
          ? "opacity-100 pointer-events-auto"
          : "opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto focus-within:opacity-100 focus-within:pointer-events-auto pointer-coarse:opacity-100 pointer-coarse:pointer-events-auto"
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
      {canReload && !contextLocked && (
        <ActionBarPrimitive.Reload asChild>
          <TooltipIconButton tooltip="重新生成" className="size-7 p-0">
            <RefreshCwIcon className="size-3.5" />
          </TooltipIconButton>
        </ActionBarPrimitive.Reload>
      )}
    </ActionBarPrimitive.Root>
  );
};

/**
 * 消息时间：不依赖 hover（触屏没有 hover，键盘也不该为了看时间而 Tab 一圈）。
 * 完整时间放 `title`，视觉上只留时分/日期。
 */
const MessageTimestamp: FC<{ className?: string }> = ({ className }) => {
  const createdAt = useAuiState((s) => s.message.createdAt);
  const label = formatMessageTime(createdAt);
  if (!label) return null;

  return (
    <time
      dateTime={createdAt.toISOString()}
      title={formatMessageTimeFull(createdAt)}
      className={cn(
        "text-[10px] tabular-nums text-muted-foreground/60 select-none",
        className,
      )}
    >
      {label}
    </time>
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
        <div className="bg-muted text-foreground max-w-[85%] rounded-2xl rounded-tr-xs px-4 py-2.5 text-sm leading-relaxed wrap-break-word">
          <MessagePrimitive.Parts components={USER_PART_COMPONENTS} />
        </div>
      </div>
      <div className="mt-1 flex justify-end pr-1">
        <MessageTimestamp />
      </div>
    </MessagePrimitive.Root>
  );
};
