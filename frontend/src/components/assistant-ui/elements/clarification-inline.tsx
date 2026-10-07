import React, { useState } from 'react';
import { HelpCircle, Send, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface ClarificationInlineProps {
  toolName?: string;
  argsText?: string;
  _result?: any;
  status?: any;
  onSubmitAnswer?: (answer: string) => void;
}

export function ClarificationInline({
  argsText = '',
  onSubmitAnswer,
}: ClarificationInlineProps) {
  let parsedArgs: {
    question?: string;
    options?: string[];
    allow_custom_input?: boolean;
  } = {};

  try {
    if (argsText) {
      parsedArgs = JSON.parse(argsText);
    }
  } catch {
    // 忽略异常
  }

  const question = parsedArgs.question || '请问您需要针对哪个具体方向进行说明？';
  const options = Array.isArray(parsedArgs.options) ? parsedArgs.options : [];
  const allowCustomInput = parsedArgs.allow_custom_input !== false;

  const [selectedOption, setSelectedOption] = useState<string | null>(null);
  const [customText, setCustomText] = useState('');
  const [submittedAnswer, setSubmittedAnswer] = useState<string | null>(null);

  const handleSubmit = (answerValue?: string) => {
    const finalAnswer = answerValue || selectedOption || customText.trim();
    if (!finalAnswer) return;
    setSubmittedAnswer(finalAnswer);
    if (onSubmitAnswer) {
      onSubmitAnswer(finalAnswer);
    }
  };

  const handleSelectOption = (opt: string) => {
    setSelectedOption(opt);
    setCustomText('');
    // 主流产品体验：点选选项即可直接快速提交确认
    handleSubmit(opt);
  };

  const canSubmitCustom = Boolean(customText.trim());

  return (
    <div className="my-3.5 w-full max-w-xl rounded-2xl border border-border/80 bg-muted/30 p-4 transition-all shadow-xs space-y-3.5">
      {/* 提问标题头 */}
      <div className="flex items-start gap-2.5">
        <div className="size-6 rounded-full bg-primary/10 text-primary flex items-center justify-center shrink-0 mt-0.5">
          <HelpCircle className="size-3.5" />
        </div>
        <div className="flex-1 space-y-1">
          <span className="text-xs font-semibold text-foreground tracking-tight block">
            意图确认
          </span>
          <p className="text-sm font-medium text-foreground/90 leading-relaxed">
            {question}
          </p>
        </div>
      </div>

      {/* 状态展示：已提交后的紧凑胶囊反馈 */}
      {submittedAnswer ? (
        <div className="flex items-center gap-2 pt-1 text-xs text-primary font-medium bg-primary/5 px-3 py-2 rounded-xl border border-primary/20">
          <Check className="size-3.5 shrink-0" />
          <span>已选择：{submittedAnswer}</span>
        </div>
      ) : (
        <>
          {/* 候选项推荐：药丸按钮 / 卡片式单选 */}
          {options.length > 0 && (
            <div className="space-y-1.5 pt-0.5">
              <div className="flex flex-wrap gap-2">
                {options.map((opt, idx) => {
                  const isSelected = selectedOption === opt;
                  return (
                    <button
                      key={idx}
                      type="button"
                      onClick={() => handleSelectOption(opt)}
                      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium border transition-all cursor-pointer ${
                        isSelected
                          ? 'border-primary bg-primary text-primary-foreground shadow-xs'
                          : 'border-border/70 bg-background hover:bg-muted hover:border-border text-foreground hover:shadow-xs'
                      }`}
                    >
                      <span>{opt}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* 手动输入补充区：极简内嵌输入框 */}
          {allowCustomInput && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                handleSubmit();
              }}
              className="flex items-center gap-2 pt-1"
            >
              <input
                type="text"
                value={customText}
                onChange={(e) => {
                  setCustomText(e.target.value);
                  if (e.target.value) setSelectedOption(null);
                }}
                placeholder="或输入自定义说明并提交..."
                className="flex-1 bg-background border border-border/70 rounded-xl px-3 py-2 text-xs text-foreground placeholder:text-muted-foreground/60 outline-none focus:border-ring/50 transition-all shadow-2xs"
              />
              <Button
                type="submit"
                size="sm"
                disabled={!canSubmitCustom}
                className="rounded-xl text-xs h-8 px-3 cursor-pointer shrink-0 gap-1 font-medium"
              >
                <Send className="size-3" />
                <span>发送</span>
              </Button>
            </form>
          )}
        </>
      )}
    </div>
  );
}
