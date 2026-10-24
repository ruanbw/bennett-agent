import {
  ComposerAddAttachment,
  ComposerAttachments,
} from "@/components/assistant-ui/elements/attachment.aui";
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text";
import { ReasoningView } from "@/components/assistant-ui/elements/reasoning-view";
import { TraceGroup } from "@/components/assistant-ui/elements/trace-group";
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { shouldFocusComposerAfterScrollToBottom } from "@/lib/scroll-follow";
import { shouldStopOnEscape } from "@/lib/stop-generation";
import {
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  groupPartByType,
  type TextMessagePartComponent,
  type ToolCallMessagePartProps,
  ThreadPrimitive,
  useAui,
  useAuiEvent,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowLeftRightIcon,
  ArrowUpIcon,
  CalendarClockIcon,
  CheckIcon,
  CopyIcon,
  FlaskConicalIcon,
  MonitorIcon,
  GitCompareArrowsIcon,
  LightbulbIcon,
  ListChecksIcon,
  MinimizeIcon,
  PenLineIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  SquareIcon,
  type LucideIcon,
} from "lucide-react";
import {
  type FC,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { ComposerStats } from "@/components/ComposerStats";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { messageTextForCopy } from "@/lib/message-copy";
import { describeCompactionNotice } from "@/lib/run-error";
import { describeErrorHint, readKnownErrorCode } from "@/lib/run-error";
import { describeCooldown, remainingCooldownMs } from "@/lib/rate-limit-cooldown";
import { ModelSelector } from "@/components/ModelSelector";
import { TurnStatusRow } from "@/components/assistant-ui/elements/turn-status-row";
import { ApprovalCard } from "@/components/assistant-ui/elements/approval-card";
import { ClarificationInputPopup } from "@/components/assistant-ui/elements/clarification-input-popup";
import { useApproval } from "@/context/approval-context";
import { useClarification } from "@/context/clarification-context";
import { useChatSessionId } from "@/context/chat-session";
import { getDraftStorage, readDraft, shouldClearDraft, writeDraft } from "@/lib/composer-draft";
import { attachmentUnsupportedMessage } from "@/lib/attachment-labels";
import { contextExhausted } from "@/lib/chat-message";
import { resolveSendGate } from "@/lib/composer-send";
import { formatMessageTime, formatMessageTimeFull } from "@/lib/message-time";
import { readUsageTiming } from "@/lib/usage-timing";
import { formatRunDurationText, type TurnEndReason } from "@/lib/step-process";
import { notify } from "@/lib/toast";
import {
  availableStartSuggestions,
  pickStartSuggestions,
  type StartSuggestionIcon,
} from "@/lib/start-suggestions";
import type {
  CompactionState,
  FallbackState,
  MessageCustomMetadata,
  ModelItem,
  RunError,
} from "@/types";

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
  computer: MonitorIcon,
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
 * 思考与工具调用合成一个「过程」块（`group-trace`）。
 *
 * 相邻的 reasoning / tool-call 部件共享同一个前缀，于是被 `GroupedParts` 合并成一个
 * 可折叠分组；正文（text）不参与分组，始终露在外面。这正是「只有最终响应展示在外面」
 * 的实现方式——过程收进一行摘要，正文才是页面上真正要读的东西。
 */
const ASSISTANT_GROUP_BY = groupPartByType({
  reasoning: ["group-trace"],
  "tool-call": ["group-trace"],
});

/**
 * 分组渲染入口。
 *
 * 参数形状是 assistant-ui 的 `RenderInfo`：可能是分组（`group-…`）、部件（`text` /
 * `tool-call` / `reasoning`）或流式占位（`indicator`）。按 `type` 分发；展开后的子部件
 * 仍走与原来一致的分部组件，所以「详细」档下的外观没有变化。
 *
 * 返回类型显式写成 `ReactNode`：`GroupedParts` 的 children 是普通函数而不是组件，
 * 用 `FC` 会把「组件可以返回 Promise（React 19 异步组件）」的类型也带进来。
 */
function renderAssistantPart({ part, children }: { part: { type: string }; children: ReactNode }): ReactNode {
  if (part.type === "group-trace") {
    const group = part as unknown as {
      indices: readonly number[];
      counts: { running: number; complete: number; incomplete: number; requiresAction: number };
    };
    return (
      <TraceGroup indices={group.indices} counts={group.counts}>
        {children}
      </TraceGroup>
    );
  }
  if (part.type === "text") return <MarkdownText />;
  if (part.type === "tool-call") {
    // 注册过专用 UI 的用专用 UI，其余走通用折叠行
    const toolPart = part as unknown as ToolCallMessagePartProps & { toolUI?: ReactNode };
    return toolPart.toolUI ?? <ToolFallback {...toolPart} />;
  }
  if (part.type === "reasoning") {
    const reasoningPart = part as unknown as { text?: string; status?: { type?: string } };
    return <ReasoningMessage {...reasoningPart} />;
  }
  return null;
}

const UserTextPart: TextMessagePartComponent = ({ text }) => <span>{text}</span>;
const USER_PART_COMPONENTS = {
  Text: UserTextPart,
};

interface ThreadProps {
  models?: ModelItem[];
  modelsError?: string | null;
  /** 服务端是否装配了联网搜索；决定起步建议里是否出现搜索示例。 */
  webSearchAvailable?: boolean;
  /** 「我的电脑」（隔离执行环境）是否可用：决定要不要展示「跑段代码」那条示例。 */
  sandboxAvailable?: boolean;
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
  sandboxAvailable = false,
  currentModel = null,
  currentReasoningEffort = "medium",
  onSelectModel = () => {},
  onSelectReasoningEffort = () => {},
  onRefreshModels,
  onCreateSession,
}) => {
  const isEmpty = useAuiState(isNewChatView);
  // 输入框已有内容时收起建议：否则点示例会 setText 覆盖用户刚打的草稿
  const composerEmpty = useAuiState((s) => s.composer.isEmpty);

  const isRunning = useAuiState(isThreadRunning);
  // 上下文已满：输入框锁住并给出「新建会话」。服务端每轮都会拒绝，这里只是不让用户
  // 白点一次发送，并把他直接送到唯一的出路（推导规则见 lib/chat-message.ts）。
  const contextLocked = useAuiState((s) => contextExhausted(s.thread.messages, currentModel));
  // 提问状态来自 context，不再由 ChatProvider 通过 cloneElement 注入
  const clarification = useClarification();
  const { dismiss: dismissPendingClarification } = clarification;
  // 审批卡片同样来自 context：它与提问是两件事（提交动作不同），所以是两个 context
  const approval = useApproval();

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
  const suggestions = pickStartSuggestions(webSearchAvailable, suggestionOffset, 4, sandboxAvailable);
  const hasMoreSuggestions =
    availableStartSuggestions(webSearchAvailable, sandboxAvailable).length > suggestions.length;
  const cycleSuggestions = useCallback(() => {
    setSuggestionOffset((offset) => offset + suggestions.length);
  }, [suggestions.length]);

  /** 去掉 autoFocus 后，桌面端「点按钮接着打字」靠这里把焦点交回输入框。 */
  const focusComposer = useCallback(() => {
    composerInputRef.current?.focus({ preventScroll: true });
  }, []);

  /** 触屏上点「回到底部」只是要读最新消息，抢焦点会弹出软键盘挡住内容。 */
  const handleScrollToBottom = useCallback(() => {
    const coarse = window.matchMedia?.("(pointer: coarse)")?.matches ?? false;
    if (!shouldFocusComposerAfterScrollToBottom(coarse)) return;
    // 滚到底后按钮会立刻 disabled/invisible；同步 focus 会被这次状态更新清掉，
    // 等这一轮 DOM 更新结束再把焦点交给输入框。
    window.setTimeout(() => focusComposer(), 0);
  }, [focusComposer]);

  /** 忽略提问后焦点交回输入框：弹层随即卸载，不接管焦点就会落到 body 上。 */
  const dismissClarification = useCallback(() => {
    dismissPendingClarification();
    focusComposer();
  }, [dismissPendingClarification, focusComposer]);

  /**
   * 生成中按 Esc 停止。
   *
   * 「停止」按钮在生成结束时被换回发送按钮，点完/按完它就不在 DOM 里了——浏览器把焦点
   * 退回 `<body>`，键盘用户得从头 Tab 一遍。所以停止后延后一拍把焦点交回输入框，这也
   * 是用户接下来最可能操作的地方。判定（输入法、弹层、已被处理的按键）在
   * `lib/stop-generation.ts`，有问题时可以直接单测。
   */
  const stopGeneration = useCallback(() => {
    aui.thread.cancelRun();
    window.setTimeout(() => composerInputRef.current?.focus({ preventScroll: true }), 0);
  }, [aui]);

  useEffect(() => {
    if (!isRunning) return undefined;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const target = event.target;
      const inModal =
        target instanceof Element && target.closest('[role="dialog"], [role="alertdialog"]') !== null;
      if (
        !shouldStopOnEscape({
          isRunning: true,
          // KeyboardEvent 上 isComposing 只在部分浏览器存在，读 nativeEvent 会更稳
          isComposing: event.isComposing,
          hasOpenOverlay:
            document.querySelector('[role="dialog"], [role="alertdialog"], [role="listbox"], [role="menu"]') !== null,
          defaultPrevented: event.defaultPrevented,
          inModal,
        })
      ) {
        return;
      }
      event.preventDefault();
      stopGeneration();
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isRunning, stopGeneration]);

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
        className="relative flex flex-1 flex-col overflow-x-hidden overflow-y-auto"
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
                输入您的问题开始对话。对话先保存在本机，登录账号会同步到服务器。
              </p>

              {composerEmpty && (
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
              )}
            </div>
          </AuiIf>

          {/*
            消息流列表：role="log" 让读屏把它当对话日志（多轮追加，不是一条新页面）。

            `aria-busy` 让读屏在生成期间只标记「内容还在变」，等本轮结束再一次性播报。
            没有它的话，每个 token 都是一次 live-region 变更：读屏读不完，浏览器的
            可访问性树也要为每帧做一次增量更新（长回答下这部分会吃掉可观的帧预算）。
          */}
          <div
            role="log"
            aria-live="polite"
            aria-relevant="additions text"
            aria-busy={isRunning}
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
            <ThreadScrollToBottom onClick={handleScrollToBottom} />

            {/* 工具审批卡片：位置与澄清浮层一致（输入框正上方），因为它同样在等用户输入。
                两者不会同时出现——澄清是模型问问题，审批是模型要执行写操作，一轮只有一种。 */}
            {approval.pending && (
              <ApprovalCard
                toolCalls={approval.pending.toolCalls}
                onSubmit={approval.submit}
                onDismiss={approval.dismiss}
              />
            )}

            {/* 输入框正上方弹出的意图澄清浮层卡片：提交后即刻消失 */}
            {clarification.pending && (
              <ClarificationInputPopup
                clarification={clarification.pending}
                onSubmitAnswer={clarification.answer}
                onDismiss={dismissClarification}
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
    // behavior 显式传 smooth：视口上的 scroll-smooth 已经去掉（它会把键盘翻页、
    // 锚点跳转等所有程序化滚动都变成动画，长会话上就是「按了 End 半天不动」），
    // 只有这个按钮需要平滑。
    <ThreadPrimitive.ScrollToBottom behavior="smooth" asChild>
      <TooltipIconButton
        tooltip="滚动到底部"
        variant="outline"
        onClick={onClick}
        // 靠右对齐输入框右边缘：之前是 self-center（居中），会盖在消息正文中轴线上。
        // size-11：触屏上要有接近 44px 的点击区域，桌面缩到 32px。
        className="absolute -top-12 right-4 z-10 size-11 rounded-full bg-background p-0 shadow-md border hover:bg-muted disabled:invisible md:size-8"
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
  // 草稿按会话存：切走再回来、或刷新页面，正在组织的那句话都还在。
  // 草稿是增强手段——取不到 storage 时（隐私模式）读为空、写静默失败，不打扰用户。
  const sessionId = useChatSessionId();
  const aui = useAui();
  const restoredFor = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionId || restoredFor.current === sessionId) return;
    restoredFor.current = sessionId;
    const draft = readDraft(getDraftStorage(), sessionId);
    // 只在该会话的输入框仍为空时回填：用户已经在打字的场景（切回来立刻输入）不能被覆盖
    if (draft && aui.composer.getState().text.trim() === "") aui.composer.setText(draft);
  }, [aui, sessionId]);

  // 整个会话的消息数组。
  //
  // selector 只返回 `s.thread.messages` 本身：它是 state 里的原始数组，引用稳定。**不能**
  // 在这里 `.map` / `.filter`——那会每次返回一个新数组，selector 的相等判断永远失败，
  // 组件会在同一轮里无限重渲染。用量、成本、轮次/步数/速度的聚合都交给
  // `ComposerStats` 内部做一次遍历（它拿到的也是这个原数组）。
  const messages = useAuiState((s) => s.thread.messages);

  // 附件暂不支持（后端只接受文本）。有附件时直接禁用发送，
  // 使「贴了图然后被静默丢掉」不可能发生——只靠提示语容易被忽略。
  const attachmentCount = useAuiState((s) => s.composer.attachments.length);
  const blockedByAttachments = attachmentCount > 0;
  const composerEmpty = useAuiState((s) => s.composer.isEmpty);
  // 发送成功后 assistant-ui 是**程序化**清空输入框的：那条路径不一定触发一次真实的
  // `input` 事件，于是 `onChange` 里写的草稿还留在 localStorage 里，切走再切回来就被
  // 回填成「我已经发出去的那句话」。这里盯住「同一会话从有内容变空」来删草稿——
  // 判据与边界写在 `shouldClearDraft` 里（含「换会话不算」这条）。
  const lastComposer = useRef<{ sessionId: string | null; empty: boolean }>({
    sessionId,
    empty: composerEmpty,
  });
  useEffect(() => {
    const next = { sessionId, empty: composerEmpty };
    if (shouldClearDraft(lastComposer.current, next)) {
      writeDraft(getDraftStorage(), sessionId, "");
    }
    lastComposer.current = next;
  }, [composerEmpty, sessionId]);

  // 两个禁用理由共存时以上下文为准：附件可以移除，换模型/新会话不能省
  const blocked = contextLocked || blockedByAttachments;
  const inputPlaceholder = contextLocked ? "本会话上下文已满，请新建会话继续" : "问点什么吧...";
  // 空输入也要禁用：ComposerPrimitive.Send 内部会拒绝发送，但 asChild 会把按钮
  // 自身的 disabled 保留下来，不显式传就渲染成「看起来能点、点了没反应」。
  const sendGate = resolveSendGate({
    contextLocked,
    hasAttachments: blockedByAttachments,
    isEmpty: composerEmpty,
  });
  const { disabled: sendDisabled, tooltip: sendTooltip } = sendGate;

  return (
    <ComposerPrimitive.Root
      // 窗口级「拖文件到别处」的兜底靠这个标记区分「输入框内」与「界面其它地方」
      // （见 lib/file-drop.ts）：框内自己处理并给更具体的提示，框外才拦浏览器导航。
      data-slot="aui_composer-root"
      className="relative flex w-full flex-col"
      onSubmit={(e) => {
        if (blocked) {
          e.preventDefault();
          return;
        }
        // 提交时就把草稿删掉：上面的 effect 盯的是「输入框变空」，而发送成功到状态更新
        // 之间有一小段窗口（用户在这期间切走会话，effect 就不会跑了）。发送失败时文本
        // 仍在输入框里，用户继续改一个字就会重新落草稿——代价远小于「已发出的内容又
        // 回到输入框」。
        writeDraft(getDraftStorage(), sessionId, "");
      }}
      // 附件入口已禁用，但拖拽/粘贴仍可能绕过按钮；不要静默丢弃，也不让浏览器直接
      // 打开被拖进来的文件。真正支持附件时，这里要改成交给 AttachmentDropzone。
      onDragOver={(e) => {
        if (Array.from(e.dataTransfer.types).includes("Files")) e.preventDefault();
      }}
      onDrop={(e) => {
        if (e.dataTransfer.files.length === 0) return;
        e.preventDefault();
        notify("info", attachmentUnsupportedMessage(e.dataTransfer.files.length));
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
          onPaste={(e) => {
            // assistant-ui 默认只在运行时声明支持附件时才接管粘贴；这里在输入框这一层
            // 直接拦文件，避免「贴了图什么都没发生」。
            if (e.clipboardData.files.length === 0) return;
            e.preventDefault();
            notify("info", attachmentUnsupportedMessage(e.clipboardData.files.length));
          }}
          onChange={(e) => {
            // 每次改动都落一次草稿；清空即删除条目（发送成功后输入框会清空）
            writeDraft(getDraftStorage(), sessionId, e.target.value);
          }}
          onKeyDown={(e) => {
            if (blocked && e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
            }
          }}
        />

        {/* 输入框底部工具条：附件/模型选择器在左侧，发送/停止在右侧。
            模型与思考等级已经合成**一个**触发器（`模型名 · 等级`）：DSH 里它们是同一次
            选择的两个维度，拆成两个并排按钮会让人以为可以分别生效，也会在窄屏上把
            发送按钮顶到第二行。min-w-0 让模型名先被截断而不是撑破工具栏。 */}
        <div className="flex flex-nowrap items-center justify-between gap-x-1 gap-y-1 px-2 pb-2.5 pt-1 sm:gap-x-2 sm:px-3">
          <div className="flex min-w-0 items-center gap-1 sm:gap-2">
            <ComposerAddAttachment />
            {/*
              不再按 `models.length > 0 || modelsError` 包一层：空目录时组件自己会显示
              错误文案与「重试」按钮，被条件挡住就等于用户永远点不到那次重试。
            */}
            <ModelSelector
              models={models}
              modelsError={modelsError}
              currentModel={currentModel}
              currentReasoningEffort={currentReasoningEffort}
              onSelectModel={onSelectModel}
              onSelectEffort={onSelectReasoningEffort}
              onRefreshModels={onRefreshModels}
            />
          </div>

          {/* 发送/停止合并为同一个位置：生成中显示停止，空闲时显示发送 */}
          <div className="ml-auto flex shrink-0 items-center gap-1 sm:gap-1.5">
            <AuiIf condition={(s) => s.thread.isRunning}>
              <ComposerPrimitive.Cancel asChild>
                <Button
                  type="button"
                  variant="destructive"
                  size="icon"
                  className="size-8 pointer-coarse:size-10 rounded-xl cursor-pointer"
                  title="停止生成（Esc）"
                  aria-label="停止生成"
                  aria-keyshortcuts="Escape"
                  // 停止后这个按钮会被换回发送按钮，焦点本来会掉到 body；
                  // 延后一拍交回输入框，键盘用户可以接着改问题。
                  onClick={() => {
                    window.setTimeout(() => inputRef?.current?.focus({ preventScroll: true }), 0);
                  }}
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
                  disabled={sendDisabled}
                  className="size-8 pointer-coarse:size-10 rounded-xl cursor-pointer"
                  aria-label={sendTooltip}
                >
                  <ArrowUpIcon className="size-4" />
                </TooltipIconButton>
              </ComposerPrimitive.Send>
            </AuiIf>
          </div>
        </div>
      </div>

      {/*
        统计读数在**输入卡片下方**（DSH 的位置），不在卡片里面：它们是「这一轮/这个
        会话花了多少」的旁注，压进卡片会把输入区撑高、也会和发送按钮抢视线。
        上下文占用、token 账单、轮次/步数/速度三颗药丸都在这里面——此前上下文指示器
        单独挂在发送按钮左侧，是为了「能不能继续问」必须可见；现在它作为第一颗药丸
        仍在同一视线上，却与另外两个读数归到一处，口径也统一由 ComposerStats 算一次。
      */}
      <ComposerStats
        messages={messages}
        currentModel={currentModel}
        modelContextWindow={selectedModel?.context_window ?? null}
        locked={contextLocked}
      />
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

/**
 * 思考过程：`ReasoningView` 自己是一条折叠条（`已思考（15s）`），默认收起。
 * 这里只把消息上持久化的耗时与开始时间递进去——组件重挂载（切会话再回来）时
 * 靠 `startedAt` 续计时。
 */
const ReasoningMessage: FC<{ text?: string; status?: { type?: string } }> = (props) => {
  const elapsedMs = useAuiState((s) => {
    const value = s.message.metadata.custom?.thinkingMs;
    return typeof value === "number" ? value : 0;
  });
  const startedAt = useAuiState((s) => {
    const value = s.message.metadata.custom?.thinkingStartedAt;
    return typeof value === "number" ? value : null;
  });

  return (
    <ReasoningView
      text={props.text}
      isStreaming={props.status?.type === "running"}
      elapsedMs={elapsedMs}
      startedAt={startedAt}
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
  const { fromModel, toModel, reason } = value as Partial<FallbackState>;
  if (typeof fromModel !== "string" || typeof toModel !== "string") return null;
  // 原因只在确实是字符串时带上：老记录/老服务端没有这个字段
  return { fromModel, toModel, reason: typeof reason === "string" ? reason : null };
};

/**
 * 压缩信息同理：四个数字缺一不可，否则宁可不显示提示行。
 *
 * 不显示比显示一个「压缩了 0 条消息」更诚实：后者会让用户以为压缩出了 bug。
 */
const readCompaction = (value: unknown): CompactionState | null => {
  if (!value || typeof value !== "object") return null;
  const {
    strategy,
    droppedMessages,
    truncatedMessages,
    usedTokensBefore,
    usedTokensAfter,
    limitTokens,
  } = value as Partial<CompactionState>;
  if (strategy !== "truncate" && strategy !== "summarize") return null;
  if (typeof usedTokensBefore !== "number" || typeof usedTokensAfter !== "number") return null;
  return {
    strategy,
    droppedMessages: typeof droppedMessages === "number" ? droppedMessages : 0,
    truncatedMessages: typeof truncatedMessages === "number" ? truncatedMessages : 0,
    usedTokensBefore,
    usedTokensAfter,
    limitTokens: typeof limitTokens === "number" ? limitTokens : 0,
  };
};

/** 失败信息同理：只在结构完整时返回，否则当作没有（老记录/写坏的数据）。 */
const readError = (value: unknown): RunError | null => {
  if (!value || typeof value !== "object") return null;
  const { message, retryable, code, retryAfterAt } = value as Partial<RunError>;
  if (typeof message !== "string") return null;
  return {
    message,
    retryable: retryable === true,
    code: readKnownErrorCode(code),
    retryAfterAt: typeof retryAfterAt === "number" ? retryAfterAt : null,
  };
};

/**
 * 429 冷却：等到 `retryAfterAt` 才允许重试，期间每秒重渲染一次用于倒计时。
 *
 * 用 `setInterval` 而不是 `requestAnimationFrame`：这是个一秒级的倒计时，60fps 重渲染
 * 纯属浪费；组件卸载时清理，避免「消息滚出视口后还在跑」。
 */
const useRetryCooldown = (retryAfterAt: number | null | undefined): string => {
  const [now, setNow] = useState(() => Date.now());
  const remaining = remainingCooldownMs(retryAfterAt, now);

  useEffect(() => {
    if (remaining <= 0) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [remaining]);

  return describeCooldown(remainingCooldownMs(retryAfterAt, now));
};

const AssistantMessage: FC = () => {
  const stopped = useAuiState((s) => s.message.metadata.custom?.stopped === true);
  const incomplete = useAuiState(isMessageIncomplete);
  const fallback = readFallback(useAuiState((s) => s.message.metadata.custom?.fallback));
  const compaction = readCompaction(useAuiState((s) => s.message.metadata.custom?.compaction));
  const error = readError(useAuiState((s) => s.message.metadata.custom?.error));
  // 只给最后一条助手消息显示重试：重跑历史回答会覆盖别的轮次（与 Reload 的约束一致）
  const isLast = useAuiState(isLastAssistantMessage);
  // 上下文超限即使被标成可重试也不给按钮：重试的请求只会更长，必然再超一次；
  // 模型不可用同理——同一个模型已经不在配置里，重试只会再拿一次同样的结论。
  const canRetry =
    isLast &&
    error?.retryable === true &&
    error.code !== "context_overflow" &&
    error.code !== "model_unavailable";
  // 分类能给出确定动作时补一行「下一步」：只说「出错了」用户不知道该动哪里
  const errorHint = error ? describeErrorHint(error.code) : "";
  // 429 有冷却期：把「还要等几秒」直接写在按钮上，否则用户只能反复点、反复失败
  const cooldownLabel = useRetryCooldown(error?.retryAfterAt);

  /**
   * 本轮墙钟耗时（服务端实测，含工具执行与排队）。
   *
   * 正常结束的一轮把它显示在**底部操作栏**里（`14:32 · 用时 1分12秒`），异常结束的
   * 一轮显示在顶部状态行里。仅当 `usage.timing` 完整时才有时长——老记录没有这个
   * 字段，那就不显示，不编一个 0。
   */
  const turnMs = useAuiState((s) => {
    const timing = readUsageTiming(s.message.metadata.custom?.usage);
    return timing ? timing.turnMs : null;
  });
  const messageStatus = useAuiState((s) => s.message.status?.type);
  const settled = messageStatus !== "running";
  // 任何 error 都是「处理失败」：此前只认 context_overflow / model_unavailable 两个码，
  // 上游 500、鉴权失败这类错误会被归成 ok，于是显示成「已完成」。
  const turnReason: TurnEndReason = error
    ? "error"
    : stopped
      ? "aborted"
      : incomplete
        ? "interrupted"
        : "ok";
  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="group flex flex-col gap-1 w-full"
    >
      {/*
        只有**异常结束**才在内容上方留一行状态（已停止 / 回复不完整 / 处理失败）。
        正常结束不再有「已完成，用时 …」这一行：它给每条回答都加一行文字与一条下边框，
        而信息量为零——回答在下面，耗时在底部操作栏。这一行同时取代了原先内容下方
        重复的「生成已停止」「回复不完整（已中断）」两条。
      */}
      {settled && <TurnStatusRow reason={turnReason} elapsedMs={turnMs} className="mb-1" />}

      <div className="text-foreground text-sm leading-relaxed wrap-break-word">
        <MessagePrimitive.GroupedParts groupBy={ASSISTANT_GROUP_BY}>
          {renderAssistantPart}
        </MessagePrimitive.GroupedParts>
        <MessageError />
      </div>

      {compaction && (
        <div className="text-muted-foreground/70 mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
          <MinimizeIcon className="size-3" />
          {describeCompactionNotice(compaction)}
        </div>
      )}

      {fallback && (
        <div className="text-muted-foreground/70 mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
          <ArrowLeftRightIcon className="size-3" />
          已切换备用模型：{fallback.fromModel} → {fallback.toModel}
          {/* 只说「已切换」不说为什么，用户与运维都只能猜 */}
          {fallback.reason ? `（${fallback.reason}）` : ''}
        </div>
      )}

      {errorHint && (
        <p className="text-muted-foreground/80 mt-1 text-[11px] leading-relaxed">{errorHint}</p>
      )}

      {canRetry && (
        <div className="mt-2 flex">
          <ActionBarPrimitive.Reload asChild>
            <button
              type="button"
              // 冷却期内 disabled：再点一次只会再吃一个 429，还多烧一次限流桶
              disabled={cooldownLabel !== ""}
              className="border-border/70 text-foreground hover:bg-muted focus-visible:ring-ring/50 inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs transition-colors focus-visible:ring-1 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50"
            >
              <RefreshCwIcon className="size-3.5" />
              {cooldownLabel || '重试'}
            </button>
          </ActionBarPrimitive.Reload>
        </div>
      )}

      {/* 操作栏容器保持恒定高度 min-h-7，避免出现/消失时导致消息卡片高度变化产生抖动 */}
      {/* 时间紧跟在操作按钮之后：和操作归为一组，而不是飘到容器最右侧 */}
      <div className="flex items-center min-h-7 mt-1 gap-1">
        <AssistantActionBar />
        <MessageTimestamp className="ml-1" />
        {/* 正常结束的耗时并进这一排：和「复制 / 重新生成 / 时间」同属一轮的旁注 */}
        {settled && <TurnElapsed elapsedMs={turnReason === "ok" ? turnMs : null} />}
      </div>
    </MessagePrimitive.Root>
  );
};

/**
 * 正常结束的一轮在操作栏末尾的耗时旁注：`· 用时 1分12秒`。
 *
 * 从原先的整轮抬头行搬过来。抬头行把「这一轮花了多久」放在正文之前，等于让一个旁注
 * 占据最贵的版面；放进操作栏后它仍随时可读，但只占一行里的一小截。
 *
 * 拿不到耗时（老记录没有 `usage.timing`）时整段不渲染——不写「用时 0秒」。
 */
const TurnElapsed: FC<{ elapsedMs: number | null }> = ({ elapsedMs }) => {
  if (elapsedMs === null || !Number.isFinite(elapsedMs)) return null;
  return (
    <span className="select-none text-[10px] tabular-nums text-muted-foreground/60">
      · 用时 {formatRunDurationText(Math.max(1000, elapsedMs))}
    </span>
  );
};

/**
 * 「复制回答」。
 *
 * 不用 `ActionBarPrimitive.Copy`：它只走 `navigator.clipboard`，在非安全上下文
 * （局域网 http 访问）里该 API 不存在，点了复制既没反应也没提示。这里复用项目里带
 * `execCommand` 回退的 `useCopyToClipboard`，文本由 `messageTextForCopy` 从文本分片拼出
 * （不含折叠的思考链与工具调用）。失败时按钮文案变成「复制失败，请重试」，不再是静默失败。
 */
const AssistantCopyButton: FC = () => {
  const text = useAuiState((s) => messageTextForCopy(s.message.content as { type?: string; text?: string }[]));
  const { isCopied, error, copyToClipboard } = useCopyToClipboard();
  const label = error ? "复制失败，请重试" : isCopied ? "已复制" : "复制回答";

  return (
    <>
      {/* 状态变化要能被读屏听到：图标从复制变对勾是不可读的视觉反馈 */}
      <span role="status" className="sr-only">
        {isCopied ? "已复制回答" : error ? "复制回答失败" : ""}
      </span>
      <TooltipIconButton
        tooltip={label}
        aria-label={label}
        onClick={() => copyToClipboard(text)}
        disabled={!text}
        className="size-7 p-0"
      >
        {isCopied ? (
          <CheckIcon className="size-3.5 text-foreground" />
        ) : (
          <CopyIcon className="size-3.5" />
        )}
      </TooltipIconButton>
    </>
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
      <AssistantCopyButton />
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
