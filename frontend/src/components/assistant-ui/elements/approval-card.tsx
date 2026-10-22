import { useId, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Check, ShieldAlert, ShieldCheck, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { ApprovedToolCallRecord } from '@/types';

/**
 * 工具审批卡片。
 *
 * ## 为什么每条调用都要显示参数
 *
 * 用户要做的决定是「这条命令能不能执行」，而**工具名不足以判断**：`delete_document`
 * 删的是哪一篇、`transfer` 转给谁，都只在参数里。只显示「AI 想调用 delete_document」
 * 的审批框等于让用户盲签。所以参数是这一层的核心内容，不是补充信息。
 *
 * ## 为什么默认「全部拒绝」而不是「全部批准」
 *
 * 危险操作的默认值必须是**不执行**：用户打开页面时手可能正好在回车键上，而
 * 「批准全部」一旦是默认，一次误触就把删除执行了。反过来的代价只是多点两下。
 *
 * ## 为什么批准还要二次确认
 *
 * `dangerous` 级别的调用是一次点击就能造成不可撤销后果的操作。这不是「再问一遍
 * 显得严谨」，而是「点错之后没有 undo」——这一层是最后一道拦截。
 */

/** 单条调用的决定：`approve` / `deny` / `undecided`。 */
type Decision = 'approve' | 'deny';
type Decisions = Record<string, Decision>;

export interface ApprovalCardProps {
  toolCalls: ApprovedToolCallRecord[];
  /**
   * 提交决定。返回 `false` 表示没能起跑（槽位被占等），卡片据此取消 loading 并允许重试。
   */
  onSubmit: (decisions: Record<string, boolean>) => Promise<boolean>;
  onDismiss?: () => void;
}

/** 风险等级 → 给用户看的说明与配色。未知等级按最高风险呈现（宁可多提醒）。 */
function describeRisk(riskLevel: string): {
  label: string;
  tone: string;
  hint: string;
} {
  switch (riskLevel) {
    case 'read_only':
      return { label: '只读', tone: 'text-muted-foreground', hint: '不会修改任何数据' };
    case 'safe_write':
      return {
        label: '写入',
        tone: 'text-amber-600 dark:text-amber-500',
        hint: '会修改数据，且通常无法撤销',
      };
    case 'dangerous':
      return {
        label: '高风险',
        tone: 'text-destructive',
        hint: '可能造成不可恢复的后果',
      };
    default:
      return {
        label: '未知风险',
        tone: 'text-amber-600 dark:text-amber-500',
        hint: '无法确认这次调用的副作用',
      };
  }
}

/** 参数展示：美化过的 JSON 优先，解析不了就原样给出（它是模型生成的原始产物）。 */
function prettyArgs(argsText: string): string {
  const trimmed = argsText.trim();
  if (!trimmed || trimmed === '{}') return '（无参数）';
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return trimmed;
  }
}

export function ApprovalCard({ toolCalls, onSubmit, onDismiss }: ApprovalCardProps) {
  const titleId = useId();
  const [decisions, setDecisions] = useState<Decisions>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDangerous, setConfirmingDangerous] = useState(false);
  // 同一个事件循环里的两次点击都在 React 提交 state 之前：ref 才是「只提交一次」的闸门。
  const submittingRef = useRef(false);

  // 换了一批调用就重置（渲染期重置，避免 effect 里级联 setState）
  const signature = useMemo(
    () => toolCalls.map((call) => call.toolCallId).join('|'),
    [toolCalls],
  );
  const [prevSignature, setPrevSignature] = useState(signature);
  if (prevSignature !== signature) {
    setPrevSignature(signature);
    setDecisions({});
    setError(null);
    setSubmitting(false);
    setConfirmingDangerous(false);
  }

  const undecided = toolCalls.filter((call) => !decisions[call.toolCallId]);
  const approveCount = toolCalls.filter((call) => decisions[call.toolCallId] === 'approve').length;
  const denyCount = toolCalls.filter((call) => decisions[call.toolCallId] === 'deny').length;
  const hasDangerousApproval = toolCalls.some(
    (call) => decisions[call.toolCallId] === 'approve' && call.riskLevel !== 'read_only',
  );

  const setOne = (toolCallId: string, decision: Decision) => {
    setDecisions((prev) => ({ ...prev, [toolCallId]: decision }));
    setError(null);
  };

  const setAll = (decision: Decision) => {
    setDecisions(Object.fromEntries(toolCalls.map((call) => [call.toolCallId, decision])));
    setError(null);
  };

  const submit = async () => {
    if (submittingRef.current) return;
    if (undecided.length > 0) {
      // 不做「未决定的按拒绝处理」：用户可能只是漏点了一条，悄悄替他拒绝会让模型
      // 收到一个他没做出的决定。要求逐条表态，代价是一次点选。
      setError(`还有 ${undecided.length} 条调用未表态`);
      return;
    }

    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const payload = Object.fromEntries(
        toolCalls.map((call) => [call.toolCallId, decisions[call.toolCallId] === 'approve']),
      );
      const ok = await onSubmit(payload);
      if (!ok) {
        setError('未能开始执行，已有回复正在生成，请稍后重试');
        submittingRef.current = false;
        setSubmitting(false);
      }
      // 成功时保持 submitting：组件即将随消息更新而卸载，解锁会让用户再点一次
    } catch (err) {
      console.warn('提交审批决定失败:', err);
      setError('提交失败，请重试');
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <div
      role="group"
      aria-labelledby={titleId}
      className="my-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 space-y-2.5"
    >
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-500" />
        <div className="min-w-0 flex-1">
          <h3 id={titleId} className="text-xs font-semibold text-foreground">
            需要你批准才能继续
          </h3>
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
            以下工具调用尚未执行。逐条确认后才会执行；拒绝的调用不会运行。
          </p>
        </div>
        {onDismiss && (
          <button
            type="button"
            aria-label="暂不处理这些调用"
            onClick={onDismiss}
            className="rounded p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
          >
            <X className="size-3.5" />
          </button>
        )}
      </div>

      <ul className="space-y-2">
        {toolCalls.map((call) => {
          const risk = describeRisk(call.riskLevel);
          const decision = decisions[call.toolCallId];
          return (
            <li
              key={call.toolCallId}
              className="rounded-md border border-border/70 bg-background/60 p-2 space-y-1.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-[11px] font-medium text-foreground">
                  {call.toolName}
                </span>
                <span className={`shrink-0 text-[10px] font-medium ${risk.tone}`} title={risk.hint}>
                  {risk.label}
                </span>
              </div>
              {call.description && (
                <p className="text-[11px] leading-relaxed text-muted-foreground">{call.description}</p>
              )}
              {/* 参数是这一层的核心内容：工具名不足以判断后果 */}
              <pre className="max-h-32 overflow-auto rounded bg-muted/50 p-1.5 font-mono text-[10.5px] leading-relaxed text-foreground">
                {prettyArgs(call.argsText)}
              </pre>
              <div className="flex gap-1.5">
                <button
                  type="button"
                  aria-pressed={decision === 'approve'}
                  disabled={submitting}
                  onClick={() => setOne(call.toolCallId, 'approve')}
                  className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50 ${
                    decision === 'approve'
                      ? 'border-emerald-500/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-400'
                      : 'border-border/70 text-muted-foreground hover:bg-muted'
                  }`}
                >
                  <Check className="size-3" />
                  批准
                </button>
                <button
                  type="button"
                  aria-pressed={decision === 'deny'}
                  disabled={submitting}
                  onClick={() => setOne(call.toolCallId, 'deny')}
                  className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50 ${
                    decision === 'deny'
                      ? 'border-destructive/60 bg-destructive/10 text-destructive'
                      : 'border-border/70 text-muted-foreground hover:bg-muted'
                  }`}
                >
                  <X className="size-3" />
                  拒绝
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      {/* 高风险批准要二次确认：一次误点就执行了一个没有 undo 的操作 */}
      {confirmingDangerous ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 space-y-1.5">
          <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-destructive">
            <AlertTriangle className="mt-0.5 size-3 shrink-0" />
            其中包含写入或高风险调用，执行后可能无法撤销。确定继续？
          </p>
          <div className="flex gap-1.5">
            <Button size="sm" variant="destructive" onClick={() => void submit()} disabled={submitting}>
              {submitting ? '执行中…' : '确认执行'}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setConfirmingDangerous(false)}
              disabled={submitting}
            >
              返回
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            disabled={submitting || undecided.length > 0}
            onClick={() => {
              // 有写入类被批准时才要二次确认；全是只读就直接提交
              if (hasDangerousApproval) setConfirmingDangerous(true);
              else void submit();
            }}
          >
            <ShieldCheck className="mr-1 size-3.5" />
            {submitting ? '提交中…' : '提交决定'}
          </Button>
          <Button size="sm" variant="ghost" disabled={submitting} onClick={() => setAll('deny')}>
            全部拒绝
          </Button>
          <Button size="sm" variant="ghost" disabled={submitting} onClick={() => setAll('approve')}>
            全部批准
          </Button>
          <span className="ml-auto text-[10.5px] text-muted-foreground">
            {approveCount > 0 || denyCount > 0
              ? `${approveCount} 批准 · ${denyCount} 拒绝 · ${undecided.length} 未定`
              : `${toolCalls.length} 条待确认`}
          </span>
        </div>
      )}

      {error && (
        <p className="flex items-center gap-1.5 text-[11px] text-destructive">
          <AlertTriangle className="size-3" />
          {error}
        </p>
      )}
    </div>
  );
}
