import React, { useState } from 'react';
import { HelpCircle, Check, Send } from 'lucide-react';
import { ToolFallbackRoot, ToolFallbackTrigger, ToolFallbackContent } from './tool-fallback.aui';
import { Button } from '@/components/ui/button';

interface AskClarificationToolProps {
  toolName?: string;
  argsText?: string;
  result?: any;
  status?: any;
  onSubmitAnswer?: (answer: string) => void;
}

export function AskClarificationTool({
  toolName = 'ask_user_clarification',
  argsText = '',
  _result,
  status,
  onSubmitAnswer,
}: AskClarificationToolProps & { _result?: any }) {
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
    // 解析失败回退
  }

  const question = parsedArgs.question || '模型请求确认您的意图：';
  const options = Array.isArray(parsedArgs.options) ? parsedArgs.options : [];
  const allowCustomInput = parsedArgs.allow_custom_input !== false;

  const [selectedOption, setSelectedOption] = useState<string | null>(null);
  const [customText, setCustomText] = useState('');
  const [submittedAnswer, setSubmittedAnswer] = useState<string | null>(null);

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const finalAnswer = selectedOption || customText.trim();
    if (!finalAnswer) return;
    setSubmittedAnswer(finalAnswer);
    if (onSubmitAnswer) {
      onSubmitAnswer(finalAnswer);
    }
  };

  const handleSelect = (opt: string) => {
    setSelectedOption(opt);
    setCustomText('');
  };

  const canSubmit = Boolean(selectedOption || customText.trim());

  return (
    <ToolFallbackRoot defaultOpen={true} className="my-2 border rounded-xl overflow-hidden bg-card/60 shadow-xs">
      <div className="px-3 py-2 bg-muted/40 border-b flex items-center justify-between">
        <div className="flex items-center gap-2 text-primary font-medium text-xs">
          <HelpCircle className="size-4" />
          <span>意图澄清与确认</span>
        </div>
        <ToolFallbackTrigger toolName={toolName} status={status} className="py-0 text-xs" />
      </div>

      <ToolFallbackContent className="p-3.5 space-y-3">
        {/* 问题文本 */}
        <div className="text-sm font-medium text-foreground bg-muted/30 p-2.5 rounded-lg border border-border/40">
          {question}
        </div>

        {/* 提交后展示已选择结果 */}
        {submittedAnswer ? (
          <div className="text-xs text-muted-foreground flex items-center gap-1.5 p-2 rounded bg-primary/5 text-primary">
            <Check className="size-3.5" />
            <span>已提交确认：{submittedAnswer}</span>
          </div>
        ) : (
          <>
            {/* 候选项列表 */}
            {options.length > 0 && (
              <div className="space-y-1.5">
                <span className="text-[11px] text-muted-foreground font-medium">候选项：</span>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {options.map((opt, idx) => {
                    const isSelected = selectedOption === opt;
                    return (
                      <button
                        key={idx}
                        type="button"
                        onClick={() => handleSelect(opt)}
                        className={`flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium border text-left transition cursor-pointer ${
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
              <form onSubmit={handleSubmit} className="space-y-1.5 pt-1">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-muted-foreground font-medium">
                    或输入自定义回答：
                  </span>
                  {selectedOption && (
                    <button
                      type="button"
                      onClick={() => setSelectedOption(null)}
                      className="text-[11px] text-muted-foreground hover:text-foreground underline cursor-pointer"
                    >
                      清除选择
                    </button>
                  )}
                </div>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={customText}
                    onChange={(e) => {
                      setCustomText(e.target.value);
                      if (e.target.value.trim()) setSelectedOption(null);
                    }}
                    placeholder="输入具体说明..."
                    className="flex-1 bg-background border border-border rounded-lg px-2.5 py-1.5 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-ring/50"
                  />
                  <Button
                    type="submit"
                    size="sm"
                    disabled={!canSubmit}
                    className="text-xs h-auto py-1.5 px-3 cursor-pointer shrink-0"
                  >
                    <Send className="size-3 mr-1" />
                    <span>提交</span>
                  </Button>
                </div>
              </form>
            )}

            {/* 如果选了选项但没有提交表单 */}
            {selectedOption && !allowCustomInput && (
              <div className="flex justify-end pt-1">
                <Button
                  type="button"
                  size="sm"
                  onClick={() => handleSubmit()}
                  className="text-xs cursor-pointer"
                >
                  确认提交
                </Button>
              </div>
            )}
          </>
        )}
      </ToolFallbackContent>
    </ToolFallbackRoot>
  );
}
