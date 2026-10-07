import React, { useState } from 'react';
import { HelpCircle, Send, X, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface ClarificationModalProps {
  question: string;
  options: string[];
  allowCustomInput?: boolean;
  onSubmit: (answer: string) => void;
  onClose?: () => void;
}

export function ClarificationModal({
  question,
  options = [],
  allowCustomInput = true,
  onSubmit,
  onClose,
}: ClarificationModalProps) {
  const [selectedOption, setSelectedOption] = useState<string | null>(null);
  const [customText, setCustomText] = useState('');

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const finalAnswer = selectedOption || customText.trim();
    if (!finalAnswer) return;
    onSubmit(finalAnswer);
  };

  const handleSelect = (opt: string) => {
    setSelectedOption(opt);
    setCustomText('');
  };

  const canSubmit = Boolean(selectedOption || customText.trim());

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-xs animate-in fade-in duration-200">
      <div
        className="w-full max-w-lg bg-background border border-border rounded-xl shadow-2xl p-5 flex flex-col gap-4 text-foreground animate-in zoom-in-95 duration-200"
        role="dialog"
        aria-modal="true"
      >
        {/* 顶部标题区 */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2 text-primary font-medium text-sm">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <HelpCircle className="size-4" />
            </div>
            <span>意图确认与澄清</span>
          </div>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="text-muted-foreground hover:text-foreground p-1 rounded-md transition cursor-pointer"
              title="关闭弹窗"
            >
              <X className="size-4" />
            </button>
          )}
        </div>

        {/* 提问内容 */}
        <div className="bg-muted/40 p-3.5 rounded-lg border border-border/50">
          <p className="text-sm font-medium leading-relaxed text-foreground">
            {question}
          </p>
        </div>

        {/* 候选选项列表 */}
        {options.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-xs text-muted-foreground font-medium">请选择：</span>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {options.map((opt, idx) => {
                const isSelected = selectedOption === opt;
                return (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => handleSelect(opt)}
                    className={`flex items-center justify-between px-3 py-2.5 rounded-lg text-xs font-medium border text-left transition cursor-pointer ${
                      isSelected
                        ? 'border-primary bg-primary/10 text-primary shadow-xs'
                        : 'border-border bg-background hover:bg-muted/60 text-foreground'
                    }`}
                  >
                    <span className="truncate">{opt}</span>
                    {isSelected && <Check className="size-3.5 shrink-0 text-primary" />}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* 手动输入补充 */}
        {allowCustomInput && (
          <form onSubmit={handleSubmit} className="flex flex-col gap-2 pt-1">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground font-medium">
                或者手动输入自定义答案：
              </span>
              {selectedOption && (
                <button
                  type="button"
                  onClick={() => setSelectedOption(null)}
                  className="text-[11px] text-muted-foreground hover:text-foreground underline cursor-pointer"
                >
                  清除已选
                </button>
              )}
            </div>
            <textarea
              value={customText}
              onChange={(e) => {
                setCustomText(e.target.value);
                if (e.target.value.trim()) {
                  setSelectedOption(null);
                }
              }}
              placeholder="输入更具体的说明或补充要求..."
              rows={2}
              className="w-full bg-muted/20 border border-border rounded-lg p-2.5 text-xs text-foreground placeholder:text-muted-foreground/60 outline-none focus:border-ring/50 focus:bg-background transition resize-none"
            />
          </form>
        )}

        {/* 底部操作 */}
        <div className="flex items-center justify-end gap-2 pt-2 border-t border-border/50">
          {onClose && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onClose}
              className="text-xs cursor-pointer"
            >
              稍后回答
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            onClick={() => handleSubmit()}
            disabled={!canSubmit}
            className="text-xs gap-1.5 cursor-pointer"
          >
            <Send className="size-3" />
            <span>确认提交</span>
          </Button>
        </div>
      </div>
    </div>
  );
}
