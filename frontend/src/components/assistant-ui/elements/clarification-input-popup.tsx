import React, { useState } from 'react';
import { HelpCircle, Send, X } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface ClarificationInputPopupProps {
  clarification: {
    question: string;
    options: string[];
    allow_custom_input?: boolean;
  };
  onSubmitAnswer: (answer: string) => void;
  onDismiss?: () => void;
}

export function ClarificationInputPopup({
  clarification,
  onSubmitAnswer,
  onDismiss,
}: ClarificationInputPopupProps) {
  const { question, options = [], allow_custom_input = true } = clarification;
  const [customText, setCustomText] = useState('');

  const handleSelectOption = (opt: string) => {
    onSubmitAnswer(opt);
  };

  const handleSubmitCustom = (e: React.FormEvent) => {
    e.preventDefault();
    if (!customText.trim()) return;
    onSubmitAnswer(customText.trim());
  };

  return (
    <div className="mb-2 w-full rounded-2xl border border-border/80 bg-background/95 backdrop-blur-md p-3.5 shadow-lg animate-in fade-in slide-in-from-bottom-2 duration-200">
      {/* 头部标题与提问 */}
      <div className="flex items-start justify-between gap-2 mb-2.5">
        <div className="flex items-start gap-2">
          <div className="size-5 rounded-full bg-primary/10 text-primary flex items-center justify-center shrink-0 mt-0.5">
            <HelpCircle className="size-3" />
          </div>
          <div>
            <span className="text-[11px] font-semibold text-primary block leading-none mb-1">
              意图确认
            </span>
            <p className="text-xs font-medium text-foreground leading-snug">
              {question}
            </p>
          </div>
        </div>

        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className="text-muted-foreground hover:text-foreground p-0.5 rounded transition cursor-pointer shrink-0"
            title="忽略并关闭"
          >
            <X className="size-3.5" />
          </button>
        )}
      </div>

      {/* 候选选项列表：药丸单选按钮，点击后立即提交并消失 */}
      {options.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {options.map((opt, idx) => (
            <button
              key={idx}
              type="button"
              onClick={() => handleSelectOption(opt)}
              className="inline-flex items-center px-3 py-1.5 rounded-full text-xs font-medium border border-border/80 bg-muted/40 hover:bg-primary hover:text-primary-foreground hover:border-primary text-foreground transition-all cursor-pointer shadow-2xs hover:shadow-xs active:scale-95"
            >
              <span>{opt}</span>
            </button>
          ))}
        </div>
      )}

      {/* 自定义手动输入行 */}
      {allow_custom_input && (
        <form onSubmit={handleSubmitCustom} className="flex items-center gap-1.5 pt-0.5">
          <input
            type="text"
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            placeholder="或手动输入具体要求并回车..."
            className="flex-1 bg-muted/30 border border-border/70 rounded-xl px-3 py-1.5 text-xs text-foreground placeholder:text-muted-foreground/60 outline-none focus:border-ring/50 focus:bg-background transition"
          />
          <Button
            type="submit"
            size="sm"
            disabled={!customText.trim()}
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
