import { useCallback, useEffect, useRef, useState } from 'react';
import { Eye, EyeOff, ExternalLink, KeyRound, Loader2 } from 'lucide-react';

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
  CUSTOM_PRESET_KEY,
  buildProviderPayload,
  createProvider,
  describeFailure,
  isPresetKey,
  listProviderPresets,
  updateProvider,
  validateProviderDraft,
  type AdminProvider,
  type ProviderDraft,
  type ProviderDraftField,
  type ProviderPreset,
} from '../../lib/admin';

export interface ProviderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` 表示新增；否则编辑该提供商。 */
  provider: AdminProvider | null;
  onSaved: () => void;
}

const ERROR_ID = 'provider-dialog-error';

/** 预设分组在界面上的顺序与标题；未知分组兜底到最后。 */
const GROUP_ORDER = ['overseas', 'cn', 'local'] as const;
const GROUP_LABELS: Record<string, string> = {
  overseas: '海外厂商',
  cn: '国内厂商',
  local: '本机网关',
};

function draftFromProvider(provider: AdminProvider | null): ProviderDraft {
  return {
    name: provider?.name ?? '',
    baseUrl: provider?.base_url ?? '',
    // 新增从「选择厂商…」占位开始；编辑没有厂商下拉，地址按原值回填
    presetKey: provider ? (provider.preset_key ?? CUSTOM_PRESET_KEY) : '',
    // 密钥只进不出：后端只回掩码，编辑时输入框永远从空开始
    apiKey: '',
    enabled: provider?.enabled ?? true,
    clearApiKey: false,
  };
}

/**
 * 新增 / 编辑模型提供商的弹窗。厂商用**下拉列表**选：官方厂商的地址、认证头
 * 都是公开常识，让管理员手抄只会制造少一个 /v1 的 404。选中后名称与地址按预设
 * 填充（仍可改，走代理的场景用），只需再填密钥。自建 vLLM / LiteLLM / 中转网关
 * 的地址本来就不固定，选「自定义」回到手填流程。
 *
 * 「密钥留空 = 不修改」这条语义完全落在 `buildProviderPayload` 里，这里只负责
 * 收集草稿；表单不自己拼请求体，避免创建与编辑两条路径各写一份 trim 规则。
 *
 * 预设清单拉取失败不阻塞「自定义」：给出重试按钮，管理员始终可以手动填地址创建。
 */
export function ProviderDialog({ open, onOpenChange, provider, onSaved }: ProviderDialogProps) {
  const [draft, setDraft] = useState<ProviderDraft>(() => draftFromProvider(provider));
  const [presets, setPresets] = useState<ProviderPreset[] | null>(null);
  const [presetsError, setPresetsError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);
  const [error, setError] = useState<{ message: string; field?: ProviderDraftField } | null>(null);
  const [wasOpen, setWasOpen] = useState(open);

  const nameRef = useRef<HTMLInputElement>(null);
  const baseUrlRef = useRef<HTMLInputElement>(null);
  const apiKeyRef = useRef<HTMLInputElement>(null);

  const loadPresets = useCallback(() => {
    setPresets(null);
    setPresetsError(null);
    listProviderPresets()
      .then((list) => setPresets(list))
      .catch((err) => {
        console.error('加载厂商预设失败:', err);
        setPresets([]);
        setPresetsError(describeFailure(err));
      });
  }, []);

  // 预设目录是静态清单，弹窗打开期间取一次就够
  useEffect(() => {
    if (!open || provider) return;
    loadPresets();
  }, [open, provider, loadPresets]);

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
  const selectedPreset = isPresetKey(draft.presetKey)
    ? (presets?.find((p) => p.key === draft.presetKey) ?? null)
    : null;

  const updateDraft = (patch: Partial<ProviderDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
    // 用户已经在改内容（或服务端错误只给了一句话），旧提示不再成立
    setError(null);
  };

  /** 下拉换厂商：预设带出名称与地址，换回「自定义/未选」只清地址，名称保留用户输入。 */
  const selectPreset = (key: string) => {
    const preset = isPresetKey(key) ? (presets?.find((p) => p.key === key) ?? null) : null;
    setDraft((prev) => ({
      ...prev,
      presetKey: key,
      name: preset?.name ?? prev.name,
      baseUrl: preset?.base_url ?? '',
      apiKey: '',
    }));
    setError(null);
  };

  const groupedPresets = (() => {
    const list = presets ?? [];
    const groups: { group: string; label: string; items: ProviderPreset[] }[] = [];
    for (const group of GROUP_ORDER) {
      const items = list.filter((preset) => preset.group === group);
      if (items.length > 0) groups.push({ group, label: GROUP_LABELS[group] ?? group, items });
    }
    // 目录里出现未知分组时兜底展示，不至于静默丢厂商
    for (const preset of list) {
      if (!GROUP_ORDER.includes(preset.group as (typeof GROUP_ORDER)[number])) {
        const bucket = groups.find((entry) => entry.group === preset.group);
        if (bucket) bucket.items.push(preset);
        else groups.push({ group: preset.group, label: GROUP_LABELS[preset.group] ?? preset.group, items: [preset] });
      }
    }
    return groups;
  })();

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

  const dialogTitle = isEdit ? '编辑提供商' : '新增提供商';
  const dialogDescription = isEdit
    ? '修改连接信息；API Key 留空表示保持已保存的密钥不变。'
    : '在「厂商」下拉里选一个上游，名称与地址会自动填好（仍可修改）；自建网关选「自定义」。';

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{dialogTitle}</DialogTitle>
          <DialogDescription>{dialogDescription}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {!isEdit && (
            <div className="space-y-1.5">
              <label htmlFor="admin-provider-preset" className="text-xs font-medium text-foreground">
                厂商
              </label>
              {presets === null && !presetsError ? (
                <div className="flex items-center gap-2 px-1 py-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
                  正在加载厂商预设…
                </div>
              ) : (
                <>
                  <select
                    id="admin-provider-preset"
                    value={draft.presetKey}
                    onChange={(event) => selectPreset(event.target.value)}
                    disabled={pending}
                    autoFocus
                    className="h-9 w-full cursor-pointer rounded-lg border border-transparent bg-muted/60 px-2.5 py-1 text-base transition-colors outline-none md:text-sm focus-visible:border-ring focus-visible:bg-background focus-visible:ring-1 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <option value="" disabled>
                      选择厂商…
                    </option>
                    {groupedPresets.map(({ group, label, items }) => (
                      <optgroup key={group} label={label}>
                        {items.map((preset) => (
                          <option key={preset.key} value={preset.key}>
                            {preset.name}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                    <option value={CUSTOM_PRESET_KEY}>自定义（手动填写地址）</option>
                  </select>
                  {presetsError && (
                    <div
                      role="alert"
                      className="flex items-center gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive"
                    >
                      <span className="flex-1">预设加载失败：{presetsError}</span>
                      <Button variant="outline" size="xs" onClick={loadPresets} className="cursor-pointer">
                        重试
                      </Button>
                    </div>
                  )}
                  {selectedPreset && (
                    <p className="text-[11px] text-muted-foreground">
                      {selectedPreset.note}
                      {selectedPreset.needs_api_key && (
                        <>
                          {' '}
                          <a
                            href={selectedPreset.api_key_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-0.5 text-primary hover:underline"
                          >
                            获取 API Key
                            <ExternalLink className="size-3" aria-hidden="true" />
                          </a>
                        </>
                      )}
                    </p>
                  )}
                </>
              )}
            </div>
          )}

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
              autoFocus={isEdit}
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
              placeholder="https://api.deepseek.com/v1"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={error?.field === 'baseUrl' || undefined}
              aria-describedby={fieldError('baseUrl')}
              disabled={pending}
            />
            <p className="text-[11px] text-muted-foreground">
              {selectedPreset
                ? '已按厂商预设填充；留空则使用预设地址。改成代理/镜像地址时认证方式仍按该厂商处理。'
                : '必须是以 http:// 或 https:// 开头的 OpenAI 兼容地址。'}
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="admin-provider-api-key" className="text-xs font-medium text-foreground">
              API Key
            </label>
            <div className="relative">
              <KeyRound
                className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                ref={apiKeyRef}
                id="admin-provider-api-key"
                type={showApiKey ? 'text' : 'password'}
                value={draft.apiKey}
                onChange={(event) => updateDraft({ apiKey: event.target.value })}
                placeholder={
                  isEdit
                    ? '留空表示不修改'
                    : selectedPreset && !selectedPreset.needs_api_key
                      ? '本机网关通常无需密钥'
                      : 'sk-...'
                }
                autoComplete="new-password"
                spellCheck={false}
                disabled={pending || (isEdit && draft.clearApiKey)}
                className="px-8 pr-9"
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
