import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
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
  type AdminModel,
  type ModelDraft,
} from '../../lib/admin';
import { modelDraftSchema, type ModelFormValues } from '../../lib/schemas';

export interface ModelDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 要编辑的模型；`null` 时弹窗内容按空值渲染。 */
  model: AdminModel | null;
  providerName: string;
  onSaved: () => void;
}

function draftFromModel(model: AdminModel | null): ModelFormValues {
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
 * 校验（名称必填、两个 token 上限可留空但填了必须正整数）由 `modelDraftSchema` 承担，
 * 组件只做「打开时回填、提交时拼请求体」。此前手动添加的模型 `context_window` 永远
 * 是 null、界面上只能显示「窗口未知」，没有入口补上——这个弹窗就是那个入口。
 */
export function ModelDialog({ open, onOpenChange, model, providerName, onSaved }: ModelDialogProps) {
  const [pending, setPending] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const form = useForm<ModelFormValues>({
    resolver: zodResolver(modelDraftSchema),
    defaultValues: draftFromModel(model),
  });
  const { register, handleSubmit, watch, setValue, reset, formState } = form;
  const reasoning = watch('reasoning');

  // 弹窗关闭后仍挂载在树上：每次打开按当前 model 回填，避免带上一轮的草稿
  useEffect(() => {
    if (!open) return;
    reset(draftFromModel(model));
    setSubmitError(null);
    setPending(false);
  }, [open, model, reset]);

  const onSubmit = handleSubmit(async (values) => {
    if (!model) return;
    setPending(true);
    setSubmitError(null);
    try {
      await updateModel(model.id, buildModelPayload(values as ModelDraft));
      onSaved();
      onOpenChange(false);
    } catch (err) {
      console.error('保存模型元数据失败:', err);
      setSubmitError(describeFailure(err));
    } finally {
      setPending(false);
    }
  });

  const displayError = formState.errors.displayName?.message;
  const contextError = formState.errors.contextWindow?.message;
  const outputError = formState.errors.maxOutputTokens?.message;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>编辑模型</DialogTitle>
          <DialogDescription>
            {model ? `${model.model_id} · ${providerName}` : providerName}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="admin-model-display-name" className="text-xs font-medium text-foreground">
              显示名称
            </label>
            <Input
              id="admin-model-display-name"
              maxLength={MAX_MODEL_DISPLAY_NAME_CHARS}
              aria-invalid={displayError ? true : undefined}
              disabled={pending}
              {...register('displayName')}
            />
            {displayError && (
              <p role="alert" className="text-[11px] text-destructive">
                {displayError}
              </p>
            )}
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor="admin-model-context-window" className="text-xs font-medium text-foreground">
                上下文窗口
              </label>
              <Input
                id="admin-model-context-window"
                inputMode="numeric"
                placeholder="如 128000"
                aria-invalid={contextError ? true : undefined}
                disabled={pending}
                {...register('contextWindow')}
              />
              {contextError && (
                <p role="alert" className="text-[11px] text-destructive">
                  {contextError}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <label htmlFor="admin-model-max-output" className="text-xs font-medium text-foreground">
                最大输出 tokens
              </label>
              <Input
                id="admin-model-max-output"
                inputMode="numeric"
                placeholder="如 8192"
                aria-invalid={outputError ? true : undefined}
                disabled={pending}
                {...register('maxOutputTokens')}
              />
              {outputError && (
                <p role="alert" className="text-[11px] text-destructive">
                  {outputError}
                </p>
              )}
            </div>
          </div>
          <p className="text-[11px] text-muted-foreground">
            两个上限都可留空表示未知；填了必须是正整数。修改只影响本机数据库，上游同步可能覆盖。
          </p>

          <div className="flex items-center justify-between gap-3 rounded-lg border border-border/70 bg-muted/40 px-3 py-2">
            <div className="min-w-0 space-y-0.5">
              <div className="text-xs font-medium text-foreground">推理模型</div>
              <p className="text-[11px] text-muted-foreground">决定对话里是否展示思考等级选择器。</p>
            </div>
            <ToggleSwitch
              checked={reasoning}
              onCheckedChange={(next) => setValue('reasoning', next)}
              label={`推理模型 ${model?.display_name ?? ''}`}
              disabled={pending}
            />
          </div>

          {submitError && (
            <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive">
              {submitError}
            </p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
              disabled={pending}
              className="cursor-pointer"
            >
              取消
            </Button>
            <Button
              type="submit"
              size="sm"
              disabled={pending || !model}
              className="cursor-pointer gap-1.5"
            >
              {pending && <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />}
              保存
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
