import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { HelpCircle, Send, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  MAX_VISIBLE_CLARIFICATION_OPTIONS,
  sanitizeClarificationOptions,
  shouldShowCustomInput,
  visibleClarificationOptions,
} from '@/lib/clarification-options';

interface ClarificationInputPopupProps {
  clarification: {
    question: string;
    options: string[];
    /** 后端事件里的 allow_custom_input，经 chat-store 归一化为 camelCase */
    allowCustomInput?: boolean;
  };
  onSubmitAnswer: (answer: string) => void;
  onDismiss?: () => void;
}

export function ClarificationInputPopup({
  clarification,
  onSubmitAnswer,
  onDismiss,
}: ClarificationInputPopupProps) {
  const { question, allowCustomInput = true } = clarification;
  const questionId = useId();
  const optionsId = useId();
  // 选项直出模型：空串会变成点了没反应的药丸，重复文本会撞 React key，长列表会撑破
  // sticky 输入区。清洗与截断的规则在 lib/clarification-options，这里只做绑定。
  const options = useMemo(
    () => sanitizeClarificationOptions(clarification.options),
    [clarification.options],
  );
  const [customText, setCustomText] = useState('');
  const [showAllOptions, setShowAllOptions] = useState(false);
  // 双击选项 / 连按回车会在弹层卸载前触发两次 answer；第二次会让 onNew 抢 run 槽位
  // 失败并回滚，用户看到互相矛盾的提示。提交一次后锁住整个弹层。
  const [submitted, setSubmitted] = useState(false);
  const [prevQuestion, setPrevQuestion] = useState(question);
  const firstOptionRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // 同一个事件循环里的两次点击都在 React 提交 submitted 之前，state 挡不住；
  // ref 才是「只提交一次」的真正闸门。
  const submittedRef = useRef(false);

  const { visible, hiddenCount } = visibleClarificationOptions(options, showAllOptions);
  // 清洗后一个有效选项都不剩时，输入框是唯一的出路：此时无视 allow_custom_input
  const withCustomInput = shouldShowCustomInput(options.length, allowCustomInput);

  // 换了一条提问就清掉上一条的草稿与展开状态（在渲染期重置，避免 effect 里级联 setState）
  if (prevQuestion !== question) {
    setPrevQuestion(question);
    setCustomText('');
    setShowAllOptions(false);
    setSubmitted(false);
  }

  // 提问出现时聚焦首个可操作控件。只在 question 变化时触发：依赖 clarification
  // 对象会在每次父级重渲染时抢走用户正在输入的焦点。
  useEffect(() => {
    // 新提问可以重新提交；ref 的复位放在 effect 里，避免渲染期写 ref
    submittedRef.current = false;
    (firstOptionRef.current ?? inputRef.current)?.focus();
  }, [question]);

  const submitAnswer = (answer: string) => {
    if (submittedRef.current || !answer) return;
    submittedRef.current = true;
    setSubmitted(true);
    onSubmitAnswer(answer);
  };

  const handleSelectOption = (option: string) => {
    submitAnswer(option);
  };

  const handleSubmitCustom = (e: React.FormEvent) => {
    e.preventDefault();
    if (!customText.trim()) return;
    submitAnswer(customText.trim());
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== 'Escape' || !onDismiss) return;
    event.stopPropagation();
    onDismiss();
  };

  return (
    <div
      role="group"
      aria-labelledby={questionId}
      onKeyDown={handleKeyDown}
      className="mb-2 max-h-[75dvh] w-full overflow-y-auto overscroll-contain rounded-2xl border border-border/80 bg-background/95 backdrop-blur-md p-3.5 shadow-lg animate-in fade-in slide-in-from-bottom-2 duration-200"
    >
      {/* 头部标题与提问 */}
      <div className="flex items-start justify-between gap-2 mb-2.5">
        <div className="flex min-w-0 items-start gap-2">
          <div className="size-5 rounded-full bg-primary/10 text-primary flex items-center justify-center shrink-0 mt-0.5">
            <HelpCircle className="size-3" />
          </div>
          <div className="min-w-0">
            <span className="text-[11px] font-semibold text-primary block leading-none mb-1">
              意图确认
            </span>
            {/* 后端允许 500 字问题：限高滚动，不把吸底输入区顶出屏幕 */}
            <p
              id={questionId}
              className="max-h-36 overflow-y-auto break-words whitespace-pre-wrap pr-0.5 text-xs font-medium text-foreground leading-snug"
            >
              {question}
            </p>
          </div>
        </div>

        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className="text-muted-foreground hover:text-foreground p-0.5 rounded transition cursor-pointer shrink-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            title="忽略并关闭"
          >
            <X className="size-3.5" aria-hidden="true" />
            <span className="sr-only">忽略并关闭提问</span>
          </button>
        )}
      </div>

      {/* 候选选项列表：药丸单选按钮，点击后立即提交并消失 */}
      {visible.length > 0 && (
        <div
          id={optionsId}
          className="flex flex-wrap content-start gap-1.5 mb-2 max-h-48 overflow-y-auto overscroll-contain pr-1"
        >
          {visible.map((opt, index) => (
            <button
              // 清洗后文本已唯一，拼上位置既是稳定 key，也避免同文本再次撞车
              key={`${index}-${opt}`}
              ref={index === 0 ? firstOptionRef : undefined}
              type="button"
              disabled={submitted}
              onClick={() => handleSelectOption(opt)}
              className="inline-flex max-w-full items-center px-3 py-1.5 rounded-full text-xs font-medium border border-border/80 bg-muted/40 hover:bg-primary hover:text-primary-foreground hover:border-primary text-foreground transition-all cursor-pointer shadow-2xs hover:shadow-xs active:scale-95 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            >
              <span className="text-left whitespace-normal break-words">{opt}</span>
            </button>
          ))}
          {/* 展开后按钮保留为「收起」：若展开即卸载按钮，键盘焦点会掉回 body，
              用户要重新 Tab 一遍才能到新出现的选项。 */}
          {options.length > MAX_VISIBLE_CLARIFICATION_OPTIONS && (
            <button
              type="button"
              disabled={submitted}
              aria-expanded={showAllOptions}
              aria-controls={optionsId}
              onClick={() => setShowAllOptions((value) => !value)}
              className="inline-flex items-center px-3 py-1.5 rounded-full text-xs font-medium border border-dashed border-border/80 text-muted-foreground hover:text-foreground hover:border-primary/50 transition-all cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            >
              {showAllOptions ? '收起选项' : `展开更多（还有 ${hiddenCount} 个）`}
            </button>
          )}
        </div>
      )}

      {/* 自定义手动输入行 */}
      {withCustomInput && (
        <form onSubmit={handleSubmitCustom} className="flex items-center gap-1.5 pt-0.5">
          <input
            ref={inputRef}
            type="text"
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            disabled={submitted}
            aria-label="自定义输入"
            placeholder={
              visible.length > 0 ? '或手动输入具体要求并回车...' : '输入你的答案并回车...'
            }
            className="flex-1 bg-muted/30 border border-border/70 rounded-xl px-3 py-1.5 text-base text-foreground placeholder:text-muted-foreground/60 outline-none focus:border-ring/50 focus:bg-background transition md:text-xs"
          />
          <Button
            type="submit"
            size="sm"
            disabled={!customText.trim() || submitted}
            className="rounded-xl text-xs h-7 px-2.5 cursor-pointer shrink-0 gap-1"
          >
            <Send className="size-3" />
            <span>发送</span>
          </Button>
        </form>
      )}
    </div>
  );
}
