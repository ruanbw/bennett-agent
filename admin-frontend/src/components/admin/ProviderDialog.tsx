import { useCallback, useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
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
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectGroupLabel,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../ui/select';
import { ToggleSwitch } from './ToggleSwitch';
import {
  CUSTOM_PRESET_KEY,
  buildProviderPayload,
  createProvider,
  describeFailure,
  isPresetKey,
  listProviderPresets,
  updateProvider,
  type AdminProvider,
  type ProviderDraft,
  type ProviderPreset,
} from '../../lib/admin';
import { providerDraftSchema, type ProviderFormValues } from '../../lib/schemas';

export interface ProviderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` 表示新增；否则编辑该提供商。 */
  provider: AdminProvider | null;
  onSaved: () => void;
}

/** 预设分组在界面上的顺序与标题；未知分组兜底到最后。 */
const GROUP_ORDER = ['overseas', 'cn', 'local'] as const;
const GROUP_LABELS: Record<string, string> = {
  overseas: '海外厂商',
  cn: '国内厂商',
  local: '本机网关',
};

function draftFromProvider(provider: AdminProvider | null): ProviderFormValues {
  return {
    name: provider?.name ?? '',
    baseUrl: provider?.base_url ?? '',
    // 新增从「选择厂商…」（空串）开始；编辑没有厂商下拉，地址按原值回填
    presetKey: provider ? (provider.preset_key ?? CUSTOM_PRESET_KEY) : '',
    // 密钥只进不出：后端只回掩码，编辑时输入框永远从空开始
    apiKey: '',
    enabled: provider?.enabled ?? true,
    clearApiKey: false,
  };
}

/**
 * 新增 / 编辑模型提供商的弹窗。
 *
 * ## 表单交给 react-hook-form + zod
 *
 * 字段校验、错误定位、提交中状态、错误聚焦全部由 RHF 处理：`providerDraftSchema`
 * （`lib/schemas.ts`）是唯一校验来源，`zodResolver` 把它的 issue 直接映射到
 * `formState.errors.<field>`——这里不再手写 `setError` + `ref.focus()`。
 *
 * ## 厂商预设
 *
 * 官方厂商的地址、认证头都是公开常识，让管理员手抄只会制造「少一个 /v1」的 404。
 * 选中预设后名称与地址自动填好（仍可改，走代理的场景用），只需再补密钥；
 * 自建 vLLM / LiteLLM / 中转网关的地址不固定，选「自定义」回到手填流程。
 *
 * ## 密钥语义
 *
 * 「留空 = 不修改」完全落在 `buildProviderPayload` 里（编辑时不带 `api_key` 字段）。
 * 表单只负责收集草稿，不自己拼请求体，避免创建与编辑两条路径各写一份 trim 规则。
 */
export function ProviderDialog({ open, onOpenChange, provider, onSaved }: ProviderDialogProps) {
  const [presets, setPresets] = useState<ProviderPreset[] | null>(null);
  const [presetsError, setPresetsError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const apiKeyRef = useRef<HTMLInputElement>(null);
  const isEdit = provider !== null;

  const form = useForm<ProviderFormValues>({
    resolver: zodResolver(providerDraftSchema),
    defaultValues: draftFromProvider(provider),
    mode: 'onSubmit',
  });
  const { register, handleSubmit, watch, setValue, clearErrors, reset, formState } = form;

  const presetKey = watch('presetKey');
  const enabled = watch('enabled');
  const clearApiKey = watch('clearApiKey');

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

  // 弹窗每次打开都重置：关闭后仍挂载在树上，不复位会把「上次编辑 A 的名称」带进「新增」
  useEffect(() => {
    if (!open) return;
    reset(draftFromProvider(provider));
    setSubmitError(null);
    setPending(false);
    setShowApiKey(false);
    if (!provider) loadPresets();
  }, [open, provider, reset, loadPresets]);

  const selectedPreset = isPresetKey(presetKey)
    ? (presets?.find((preset) => preset.key === presetKey) ?? null)
    : null;

  /** 换厂商：预设带出名称与地址；换回「自定义」只清地址，名称保留用户输入。 */
  const selectPreset = (key: string) => {
    const preset = isPresetKey(key) ? (presets?.find((p) => p.key === key) ?? null) : null;
    setValue('presetKey', key);
    setValue('name', preset?.name ?? form.getValues('name'));
    setValue('baseUrl', preset?.base_url ?? '');
    setValue('apiKey', '');
    clearErrors(['name', 'baseUrl']);
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
        else
          groups.push({
            group: preset.group,
            label: GROUP_LABELS[preset.group] ?? preset.group,
            items: [preset],
          });
      }
    }
    return groups;
  })();

  const onSubmit = handleSubmit(async (values) => {
    setPending(true);
    setSubmitError(null);
    try {
      const draft = values as ProviderDraft;
      if (provider) await updateProvider(provider.id, buildProviderPayload(draft, 'edit'));
      else await createProvider(buildProviderPayload(draft, 'create'));
      onSaved();
      onOpenChange(false);
    } catch (err) {
      console.error('保存模型提供商失败:', err);
      setSubmitError(describeFailure(err));
    } finally {
      setPending(false);
    }
  });

  const nameError = formState.errors.name?.message;
  const baseUrlError = formState.errors.baseUrl?.message;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? '编辑提供商' : '新增提供商'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? '修改连接信息；API Key 留空表示保持已保存的密钥不变。'
              : '在「厂商」下拉里选一个上游，名称与地址会自动填好（仍可修改）；自建网关选「自定义」。'}
          </DialogDescription>
        </DialogHeader>

        {/* noValidate：错误由 zod 统一给出（中文、带字段路径），不要让浏览器先弹原生气泡 */}
        <form onSubmit={onSubmit} noValidate className="space-y-3">
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
                  <Select
                    items={[
                      ...groupedPresets.flatMap(({ label, items }) =>
                        items.map((preset) => ({ value: preset.key, label: preset.name, group: label })),
                      ),
                      { value: CUSTOM_PRESET_KEY, label: '自定义（手动填写地址）', group: '其他' },
                    ]}
                    value={presetKey || null}
                    onValueChange={(value) => selectPreset(String(value ?? ''))}
                    disabled={pending}
                  >
                    <SelectTrigger id="admin-provider-preset" className="h-9">
                      <SelectValue placeholder="选择厂商…" />
                    </SelectTrigger>
                    <SelectContent>
                      {groupedPresets.map(({ group, label, items }) => (
                        <SelectGroup key={group}>
                          <SelectGroupLabel>{label}</SelectGroupLabel>
                          {items.map((preset) => (
                            <SelectItem key={preset.key} value={preset.key}>
                              {preset.name}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      ))}
                      <SelectGroup>
                        <SelectGroupLabel>其他</SelectGroupLabel>
                        <SelectItem value={CUSTOM_PRESET_KEY}>自定义（手动填写地址）</SelectItem>
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  {presetsError && (
                    <div
                      role="alert"
                      className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive"
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
              id="admin-provider-name"
              placeholder="例如 DeepSeek"
              aria-invalid={nameError ? true : undefined}
              aria-describedby={nameError ? 'admin-provider-name-error' : undefined}
              disabled={pending}
              {...register('name')}
            />
            {nameError && (
              <p id="admin-provider-name-error" role="alert" className="text-[11px] text-destructive">
                {nameError}
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <label htmlFor="admin-provider-base-url" className="text-xs font-medium text-foreground">
              接口地址
            </label>
            <Input
              id="admin-provider-base-url"
              placeholder="https://api.deepseek.com/v1"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={baseUrlError ? true : undefined}
              aria-describedby={baseUrlError ? 'admin-provider-base-url-error' : undefined}
              disabled={pending}
              {...register('baseUrl')}
            />
            {baseUrlError ? (
              <p id="admin-provider-base-url-error" role="alert" className="text-[11px] text-destructive">
                {baseUrlError}
              </p>
            ) : (
              <p className="text-[11px] text-muted-foreground">
                {selectedPreset
                  ? '已按厂商预设填充；留空则使用预设地址。改成代理/镜像地址时认证方式仍按该厂商处理。'
                  : '必须是以 http:// 或 https:// 开头的 OpenAI 兼容地址。'}
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <label htmlFor="admin-provider-api-key" className="text-xs font-medium text-foreground">
              API Key
            </label>
            <div className="relative">
              <KeyRound
                className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                id="admin-provider-api-key"
                type={showApiKey ? 'text' : 'password'}
                placeholder={
                  isEdit
                    ? '留空表示不修改'
                    : selectedPreset && !selectedPreset.needs_api_key
                      ? '本机网关通常无需密钥'
                      : 'sk-...'
                }
                autoComplete="new-password"
                spellCheck={false}
                disabled={pending || (isEdit && clearApiKey)}
                className="px-8 pr-9"
                {...(() => {
                  // RHF 的 register 会带自己的 ref，直接把两个 ref 叠在一起会被覆盖，
                  // 所以在这里合成一个回调 ref：两条都需要（RHF 读值、显隐按钮要聚焦）
                  const { ref: rhfRef, ...rest } = register('apiKey');
                  return {
                    ...rest,
                    ref: (element: HTMLInputElement | null) => {
                      apiKeyRef.current = element;
                      rhfRef(element);
                    },
                  };
                })()}
              />
              <button
                type="button"
                onClick={() => {
                  setShowApiKey((visible) => !visible);
                  // 点击按钮会让输入框失焦；切完还回去，用户可以继续输入/核对
                  apiKeyRef.current?.focus();
                }}
                disabled={pending || (isEdit && clearApiKey)}
                aria-label={showApiKey ? '隐藏 API Key' : '显示 API Key'}
                aria-pressed={showApiKey}
                aria-controls="admin-provider-api-key"
                className="absolute top-1/2 right-1 flex size-7 -translate-y-1/2 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
              >
                {showApiKey ? <EyeOff size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}
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
                  className="size-3.5 cursor-pointer accent-destructive"
                  checked={clearApiKey}
                  onChange={(event) => {
                    setValue('clearApiKey', event.target.checked);
                    if (event.target.checked) setValue('apiKey', '');
                  }}
                />
                清除已保存的密钥
              </label>
            )}
          </div>

          <div className="flex items-center justify-between gap-3 rounded-lg border border-border/70 bg-muted/40 px-3 py-2">
            <div className="min-w-0 space-y-0.5">
              <div className="text-xs font-medium text-foreground">启用</div>
              <p className="text-[11px] text-muted-foreground">
                停用后该提供商的模型不会出现在对话的模型列表里。
              </p>
            </div>
            <ToggleSwitch
              checked={enabled}
              onCheckedChange={(next) => setValue('enabled', next)}
              label={isEdit ? `启用提供商 ${provider?.name ?? ''}` : '启用该提供商'}
              disabled={pending}
            />
          </div>

          {submitError && (
            <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive">
              {submitError}
            </p>
          )}

          {/* 表单在弹窗里，按钮需要 type=submit；放在 form 内部保证回车也能提交 */}
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
            <Button type="submit" size="sm" disabled={pending} className="cursor-pointer gap-1.5">
              {pending && <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />}
              {isEdit ? '保存' : '创建'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
