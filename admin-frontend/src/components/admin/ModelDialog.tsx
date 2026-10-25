import { useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';

import { Button } from '../ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../ui/dialog';
import { Input } from '../ui/input';
import { ToggleSwitch } from './ToggleSwitch';
import {
  MAX_MODEL_DISPLAY_NAME_CHARS,
  buildModelPayload,
  describeFailure,
  updateModel,
  validateModelDraft,
  type AdminModel,
  type ModelDraft,
  type ModelDraftField,
} from '../../lib/admin';

export interface ModelDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 要编辑的模型；`null` 时弹窗内容按空值渲染。 */
  model: AdminModel | null;
  providerName: string;
  onSaved: () => void;
}

const ERROR_ID = 'model-dialog-error';

function draftFromModel(model: AdminModel | null): ModelDraft {
  return {
    displayName: model?.display_name ?? '',
    contextWindow: model?.context_window?.toString() ?? '',
    maxOutputTokens: model?.max_output_tokens?.toString() ?? '',
    reasoning: Boolean(model?.reasoning),
  };
}

/**
 * 模型元数据编辑弹窗。
 *
 * 此前后台只能开关模型 / 设默认 / 删除：手动添加的模型 `context_window` 永远是 null，
 * 展示成「窗口未知」，管理员没有入口补上它。上游同步会刷新元数据，但不是所有网关都
 * 返回窗口，手动模型也不会被同步覆盖。
 */
export function ModelDialog({ open, onOpenChange, model, providerName, onSaved }: ModelDialogProps) {
  const [draft, setDraft] = useState<ModelDraft>(() => draftFromModel(model));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<{ message: string; field?: ModelDraftField } | null>(null);
  const [wasOpen, setWasOpen] = useState(open);

  const displayNameRef = useRef<HTMLInputElement>(null);
  const contextWindowRef = useRef<HTMLInputElement>(null);
  const maxOutputRef = useRef<HTMLInputElement>(null);

  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) {
      setDraft(draftFromModel(model));
      setError(null);
      setPending(false);
    }
  }

  const updateDraft = (patch: Partial<ModelDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    setError(null);
  };

  const handleSubmit = async () => {
    if (!model) return;
    const issue = validateModelDraft(draft);
    if (issue) {
      setError(issue);
      const refs = {
        displayName: displayNameRef,
        contextWindow: contextWindowRef,
        maxOutputTokens: maxOutputRef,
      };
      refs[issue.field].current?.focus();
      return;
    }

    setPending(true);
    setError(null);
    try {
      await updateModel(model.id, buildModelPayload(draft));
      onSaved();
      onOpenChange(false);
    } catch (err) {
      console.error('保存模型元数据失败:', err);
      setError({ message: describeFailure(err) });
    } finally {
      setPending(false);
    }
  };

  const fieldError = (field: ModelDraftField) => (error?.field === field ? ERROR_ID : undefined);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>编辑模型</DialogTitle>
          <DialogDescription>
            {model ? `${model.model_id} · ${providerName}` : providerName}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="admin-model-display-name" className="text-xs font-medium text-foreground">
              显示名称
            </label>
            <Input
              ref={displayNameRef}
              id="admin-model-display-name"
              value={draft.displayName}
              onChange={(event) => updateDraft({ displayName: event.target.value })}
              maxLength={MAX_MODEL_DISPLAY_NAME_CHARS}
              aria-invalid={error?.field === 'displayName' || undefined}
              aria-describedby={fieldError('displayName')}
              disabled={pending}
              autoFocus
            />
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor="admin-model-context-window" className="text-xs font-medium text-foreground">
                上下文窗口
              </label>
              <Input
                ref={contextWindowRef}
                id="admin-model-context-window"
                inputMode="numeric"
                value={draft.contextWindow}
                onChange={(event) => updateDraft({ contextWindow: event.target.value })}
                placeholder="如 128000"
                aria-invalid={error?.field === 'contextWindow' || undefined}
                aria-describedby={fieldError('contextWindow')}
                disabled={pending}
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="admin-model-max-output" className="text-xs font-medium text-foreground">
                最大输出 tokens
              </label>
              <Input
                ref={maxOutputRef}
                id="admin-model-max-output"
                inputMode="numeric"
                value={draft.maxOutputTokens}
                onChange={(event) => updateDraft({ maxOutputTokens: event.target.value })}
                placeholder="如 8192"
                aria-invalid={error?.field === 'maxOutputTokens' || undefined}
                aria-describedby={fieldError('maxOutputTokens')}
                disabled={pending}
              />
            </div>
          </div>
          <p className="text-[11px] text-muted-foreground">
            两个上限都可留空表示未知；填了必须是正整数。修改只影响本机数据库，上游同步可能覆盖。
          </p>

          <div className="flex items-center justify-between gap-3 rounded-xl border border-border/70 bg-muted/40 px-3 py-2">
            <div className="min-w-0 space-y-0.5">
              <div className="text-xs font-medium text-foreground">推理模型</div>
              <p className="text-[11px] text-muted-foreground">
                决定对话里是否展示思考等级选择器。
              </p>
            </div>
            <ToggleSwitch
              checked={draft.reasoning}
              onCheckedChange={(reasoning) => updateDraft({ reasoning })}
              label={`推理模型 ${model?.display_name ?? ''}`}
              disabled={pending}
            />
          </div>

          {error && (
            <p
              id={ERROR_ID}
              role="alert"
              className="rounded-xl border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive"
            >
              {error.message}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={pending}
            className="cursor-pointer"
          >
            取消
          </Button>
          <Button
            size="sm"
            onClick={() => void handleSubmit()}
            disabled={pending || !model}
            className="cursor-pointer gap-1.5"
          >
            {pending && <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />}
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
