import { useRef, useState } from 'react';
import { Eye, EyeOff, Loader2 } from 'lucide-react';

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
  buildProviderPayload,
  createProvider,
  describeFailure,
  updateProvider,
  validateProviderDraft,
  type AdminProvider,
  type ProviderDraft,
  type ProviderDraftField,
} from '../../lib/admin';

export interface ProviderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` 表示新增；否则编辑该提供商。 */
  provider: AdminProvider | null;
  onSaved: () => void;
}

const ERROR_ID = 'provider-dialog-error';

function draftFromProvider(provider: AdminProvider | null): ProviderDraft {
  return {
    name: provider?.name ?? '',
    baseUrl: provider?.base_url ?? '',
    // 密钥只进不出：后端只回掩码，编辑时输入框永远从空开始
    apiKey: '',
    enabled: provider?.enabled ?? true,
    clearApiKey: false,
  };
}

/**
 * 新增 / 编辑模型提供商的弹窗。
 *
 * 「密钥留空 = 不修改」这条语义完全落在 `buildProviderPayload` 里，这里只负责
 * 收集草稿；表单不自己拼请求体，避免创建与编辑两条路径各写一份 trim 规则。
 *
 * 本地校验失败会把焦点移到出错字段并挂 `aria-invalid` / `aria-describedby`：
 * 服务端 422 的文案带着 `body.base_url` 与英文 `Value error`，对管理员不友好。
 * API Key 提供显隐按钮——粘贴一长串密钥后能核对一眼，比保存后才发现截错强。
 */
export function ProviderDialog({ open, onOpenChange, provider, onSaved }: ProviderDialogProps) {
  const [draft, setDraft] = useState<ProviderDraft>(() => draftFromProvider(provider));
  const [pending, setPending] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);
  const [error, setError] = useState<{ message: string; field?: ProviderDraftField } | null>(null);
  const [wasOpen, setWasOpen] = useState(open);

  const nameRef = useRef<HTMLInputElement>(null);
  const baseUrlRef = useRef<HTMLInputElement>(null);
  const apiKeyRef = useRef<HTMLInputElement>(null);

  // 每次打开都按当前 provider 重置草稿：弹窗关闭后仍挂载在树上，
  // 不复位就会出现「上次编辑 A 的名称」被带到「新增」里；密钥的显隐也复位。
  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) {
      setDraft(draftFromProvider(provider));
      setError(null);
      setPending(false);
      setShowApiKey(false);
    }
  }

  const isEdit = provider !== null;

  const updateDraft = (patch: Partial<ProviderDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    // 用户已经在改内容（或服务端错误只给了一句话），旧提示不再成立
    setError(null);
  };

  const toggleApiKeyVisibility = () => {
    setShowApiKey((visible) => !visible);
    // 点击按钮会让输入框失焦；切完把焦点还回去，用户可以继续输入/核对
    apiKeyRef.current?.focus();
  };

  const handleSubmit = async () => {
    const issue = validateProviderDraft(draft);
    if (issue) {
      setError(issue);
      const refs = { name: nameRef, baseUrl: baseUrlRef, apiKey: apiKeyRef };
      refs[issue.field].current?.focus();
      return;
    }

    setPending(true);
    setError(null);
    try {
      if (provider) {
        await updateProvider(provider.id, buildProviderPayload(draft, 'edit'));
      } else {
        await createProvider(buildProviderPayload(draft, 'create'));
      }
      onSaved();
      onOpenChange(false);
    } catch (err) {
      console.error('保存模型提供商失败:', err);
      setError({ message: describeFailure(err) });
    } finally {
      setPending(false);
    }
  };

  const fieldError = (field: ProviderDraftField) => (error?.field === field ? ERROR_ID : undefined);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? '编辑提供商' : '新增提供商'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? '修改连接信息；API Key 留空表示保持已保存的密钥不变。'
              : '填写上游 OpenAI 兼容接口的地址与密钥。'}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="admin-provider-name" className="text-xs font-medium text-foreground">
              名称
            </label>
            <Input
              ref={nameRef}
              id="admin-provider-name"
              value={draft.name}
              onChange={(event) => updateDraft({ name: event.target.value })}
              placeholder="例如 DeepSeek"
              autoFocus
              aria-invalid={error?.field === 'name' || undefined}
              aria-describedby={fieldError('name')}
              disabled={pending}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="admin-provider-base-url" className="text-xs font-medium text-foreground">
              接口地址
            </label>
            <Input
              ref={baseUrlRef}
              id="admin-provider-base-url"
              value={draft.baseUrl}
              onChange={(event) => updateDraft({ baseUrl: event.target.value })}
              placeholder="https://api.deepseek.com"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={error?.field === 'baseUrl' || undefined}
              aria-describedby={fieldError('baseUrl')}
              disabled={pending}
            />
            <p className="text-[11px] text-muted-foreground">必须是以 http:// 或 https:// 开头的地址。</p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="admin-provider-api-key" className="text-xs font-medium text-foreground">
              API Key
            </label>
            <div className="relative">
              <Input
                ref={apiKeyRef}
                id="admin-provider-api-key"
                type={showApiKey ? 'text' : 'password'}
                value={draft.apiKey}
                onChange={(event) => updateDraft({ apiKey: event.target.value })}
                placeholder={isEdit ? '留空表示不修改' : 'sk-...'}
                autoComplete="new-password"
                spellCheck={false}
                disabled={pending || (isEdit && draft.clearApiKey)}
                className="pr-9"
              />
              <button
                type="button"
                onClick={toggleApiKeyVisibility}
                disabled={pending || (isEdit && draft.clearApiKey)}
                aria-label={showApiKey ? '隐藏 API Key' : '显示 API Key'}
                aria-pressed={showApiKey}
                aria-controls="admin-provider-api-key"
                title={showApiKey ? '隐藏 API Key' : '显示 API Key'}
                className="absolute right-1 top-1/2 flex size-7 -translate-y-1/2 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {showApiKey ? (
                  <EyeOff size={14} aria-hidden="true" />
                ) : (
                  <Eye size={14} aria-hidden="true" />
                )}
              </button>
            </div>
            {provider && (
              <p className="text-[11px] text-muted-foreground">
                已保存的密钥：<span className="font-mono">{provider.api_key_hint ?? '未设置'}</span>
              </p>
            )}
            {isEdit && (
              <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={draft.clearApiKey}
                  onChange={(event) =>
                    updateDraft({ clearApiKey: event.target.checked, apiKey: '' })
                  }
                  className="size-3.5 cursor-pointer accent-destructive"
                />
                清除已保存的密钥
              </label>
            )}
          </div>

          <div className="flex items-center justify-between gap-3 rounded-xl border border-border/70 bg-muted/40 px-3 py-2">
            <div className="min-w-0 space-y-0.5">
              <div className="text-xs font-medium text-foreground">启用</div>
              <p className="text-[11px] text-muted-foreground">
                停用后该提供商的模型不会出现在对话的模型列表里。
              </p>
            </div>
            <ToggleSwitch
              checked={draft.enabled}
              onCheckedChange={(enabled) => updateDraft({ enabled })}
              label={isEdit ? `启用提供商 ${provider?.name ?? ''}` : '启用该提供商'}
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
            disabled={pending}
            className="cursor-pointer gap-1.5"
          >
            {pending && <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />}
            {isEdit ? '保存' : '创建'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
