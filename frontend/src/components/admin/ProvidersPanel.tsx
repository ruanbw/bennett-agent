import { useCallback, useEffect, useState } from 'react';
import {
  KeyRound,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Star,
  Trash2,
  X,
} from 'lucide-react';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import { ProviderDialog } from './ProviderDialog';
import { ToggleSwitch } from './ToggleSwitch';
import {
  createProviderModel,
  deleteModel,
  deleteProvider,
  describeFailure,
  formatContextWindow,
  listProviderModels,
  listProviders,
  syncProvider,
  testProvider,
  updateModel,
  type AdminModel,
  type AdminProvider,
} from '../../lib/admin';

interface ProviderNotice {
  tone: 'success' | 'error';
  text: string;
}

interface ProviderSnapshot {
  providers: AdminProvider[];
  modelsByProvider: Record<string, AdminModel[]>;
}

/**
 * 一次取回「提供商 + 每个提供商下的模型」。
 *
 * 模型列表没有批量接口，只能按提供商逐个拉；用 `Promise.all` 并发，
 * 任何一个失败都算整体失败——半套数据配上「测试连接/同步」按钮只会让管理员
 * 对着过期的模型数做决定。
 */
async function loadProviderSnapshot(): Promise<ProviderSnapshot> {
  const providers = await listProviders();
  const modelLists = await Promise.all(providers.map((provider) => listProviderModels(provider.id)));

  const modelsByProvider: Record<string, AdminModel[]> = {};
  providers.forEach((provider, index) => {
    modelsByProvider[provider.id] = modelLists[index];
  });

  return { providers, modelsByProvider };
}

const noticeToneClass: Record<ProviderNotice['tone'], string> = {
  success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  error: 'border-destructive/40 bg-destructive/10 text-destructive',
};

export function ProvidersPanel() {
  const [providers, setProviders] = useState<AdminProvider[] | null>(null);
  const [modelsByProvider, setModelsByProvider] = useState<Record<string, AdminModel[]>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  /** 同一时间只允许一个异步操作，值是操作 key（`test:<id>`、`model-toggle:<id>`…）。 */
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notices, setNotices] = useState<Record<string, ProviderNotice>>({});
  const [newModelIds, setNewModelIds] = useState<Record<string, string>>({});

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingProvider, setEditingProvider] = useState<AdminProvider | null>(null);
  const [pendingDeleteProvider, setPendingDeleteProvider] = useState<AdminProvider | null>(null);
  const [pendingDeleteModel, setPendingDeleteModel] = useState<AdminModel | null>(null);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  useEffect(() => {
    // 忽略过期响应：连点「重试」时先发的请求可能后到
    let active = true;

    void (async () => {
      try {
        const snapshot = await loadProviderSnapshot();
        if (!active) return;
        setProviders(snapshot.providers);
        setModelsByProvider(snapshot.modelsByProvider);
        setLoadError(null);
      } catch (err) {
        if (!active) return;
        console.error('加载模型提供商失败:', err);
        setLoadError(describeFailure(err));
      }
    })();

    return () => {
      active = false;
    };
  }, [reloadToken]);

  /** 统一挂 busy、收集失败文案；返回是否成功，调用方据此决定要不要刷新。 */
  const run = useCallback(
    async (key: string, action: () => Promise<void>): Promise<boolean> => {
      setBusy(key);
      setActionError(null);
      try {
        await action();
        return true;
      } catch (err) {
        console.error(`管理操作失败 (${key}):`, err);
        setActionError(describeFailure(err));
        return false;
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const setNotice = useCallback((providerId: string, notice: ProviderNotice | null) => {
    setNotices((prev) => {
      const next = { ...prev };
      if (notice) next[providerId] = notice;
      else delete next[providerId];
      return next;
    });
  }, []);

  const openCreateDialog = useCallback(() => {
    setEditingProvider(null);
    setDialogOpen(true);
  }, []);

  const openEditDialog = useCallback((provider: AdminProvider) => {
    setEditingProvider(provider);
    setDialogOpen(true);
  }, []);

  const handleTest = useCallback(
    (provider: AdminProvider) => {
      setNotice(provider.id, null);
      void run(`test:${provider.id}`, async () => {
        try {
          const result = await testProvider(provider.id);
          setNotice(
            provider.id,
            result.ok
              ? {
                  tone: 'success',
                  text: `连接成功，发现 ${result.model_count} 个模型`,
                }
              : { tone: 'error', text: result.error ?? '连接失败，上游未返回原因' },
          );
        } catch (err) {
          // 「连不上」是测试的预期结果之一，原因留在卡片里比挂到面板顶部更贴近操作
          setNotice(provider.id, { tone: 'error', text: describeFailure(err) });
        }
      });
    },
    [run, setNotice],
  );

  const handleSync = useCallback(
    (provider: AdminProvider) => {
      setNotice(provider.id, null);
      void (async () => {
        const ok = await run(`sync:${provider.id}`, async () => {
          const result = await syncProvider(provider.id);
          setNotice(provider.id, {
            tone: 'success',
            text: `同步完成：新增 ${result.created} / 更新 ${result.updated}`,
          });
        });
        if (ok) reload();
      })();
    },
    [run, reload, setNotice],
  );

  const handleDeleteProvider = useCallback(
    (provider: AdminProvider) => {
      void (async () => {
        const ok = await run(`provider-delete:${provider.id}`, () => deleteProvider(provider.id));
        if (ok) reload();
      })();
    },
    [run, reload],
  );

  const handleToggleModel = useCallback(
    (model: AdminModel) => {
      void (async () => {
        // 停用默认模型时后端会自动取消默认，局部点改会让星标残留，所以统一重载
        const ok = await run(`model-toggle:${model.id}`, async () => {
          await updateModel(model.id, { enabled: !model.enabled });
        });
        if (ok) reload();
      })();
    },
    [run, reload],
  );

  const handleSetDefault = useCallback(
    (model: AdminModel) => {
      void (async () => {
        // 默认只能有一个：重载后其他行的星标才会消失
        const ok = await run(`model-default:${model.id}`, async () => {
          await updateModel(model.id, { is_default: true });
        });
        if (ok) reload();
      })();
    },
    [run, reload],
  );

  const handleDeleteModel = useCallback(
    (model: AdminModel) => {
      void (async () => {
        const ok = await run(`model-delete:${model.id}`, () => deleteModel(model.id));
        if (ok) reload();
      })();
    },
    [run, reload],
  );

  const handleAddModel = useCallback(
    (providerId: string) => {
      const modelId = (newModelIds[providerId] ?? '').trim();
      if (!modelId) {
        setActionError('请先填写上游模型 ID');
        return;
      }

      void (async () => {
        const ok = await run(`model-create:${providerId}`, async () => {
          await createProviderModel(providerId, { model_id: modelId });
        });
        if (ok) {
          setNewModelIds((prev) => ({ ...prev, [providerId]: '' }));
          reload();
        }
      })();
    },
    [newModelIds, run, reload],
  );

  const isBusy = busy !== null;
  // 展示用的模型数与模型行来自同一次快照，避免「模型数 5、列表 3 行」的自相矛盾
  const modelCountOf = (provider: AdminProvider) =>
    modelsByProvider[provider.id]?.length ?? provider.model_count;

  return (
    <section className="space-y-3" aria-labelledby="admin-providers-heading">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id="admin-providers-heading" className="text-sm font-semibold text-foreground">
            模型提供商
          </h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            维护上游接口、密钥与可路由的模型清单。
          </p>
        </div>
        <Button
          size="sm"
          onClick={openCreateDialog}
          disabled={isBusy}
          className="cursor-pointer gap-1.5"
        >
          <Plus className="size-3.5" aria-hidden="true" />
          新增提供商
        </Button>
      </div>

      {actionError && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <span className="flex-1 break-words">{actionError}</span>
          <button
            type="button"
            onClick={() => setActionError(null)}
            aria-label="关闭错误提示"
            className="shrink-0 cursor-pointer hover:opacity-70"
          >
            <X className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      )}

      {loadError && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <span className="min-w-0 flex-1 break-words">{loadError}</span>
          <Button variant="outline" size="xs" onClick={reload} className="cursor-pointer">
            重试
          </Button>
        </div>
      )}

      {providers === null ? (
        !loadError && (
          <div
            role="status"
            className="flex items-center justify-center gap-2 rounded-xl border border-border/70 bg-card/50 py-10 text-xs text-muted-foreground"
          >
            <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            正在加载提供商…
          </div>
        )
      ) : providers.length === 0 ? (
        <div className="rounded-xl border border-border/70 bg-card/50 px-4 py-10 text-center text-xs text-muted-foreground">
          还没有配置任何提供商，点右上角「新增提供商」开始。
        </div>
      ) : (
        <div className="space-y-3">
          {providers.map((provider) => (
            <ProviderCard
              key={provider.id}
              provider={provider}
              models={modelsByProvider[provider.id] ?? []}
              modelCount={modelCountOf(provider)}
              busy={busy}
              notice={notices[provider.id]}
              newModelId={newModelIds[provider.id] ?? ''}
              onNewModelIdChange={(value) =>
                setNewModelIds((prev) => ({ ...prev, [provider.id]: value }))
              }
              onTest={() => handleTest(provider)}
              onSync={() => handleSync(provider)}
              onEdit={() => openEditDialog(provider)}
              onRequestDelete={() => setPendingDeleteProvider(provider)}
              onToggleModel={handleToggleModel}
              onSetDefault={handleSetDefault}
              onRequestDeleteModel={setPendingDeleteModel}
              onAddModel={() => handleAddModel(provider.id)}
              onDismissNotice={() => setNotice(provider.id, null)}
            />
          ))}
        </div>
      )}

      <ProviderDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        provider={editingProvider}
        onSaved={reload}
      />

      <ConfirmDialog
        open={pendingDeleteProvider !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDeleteProvider(null);
        }}
        title="删除提供商"
        description={
          pendingDeleteProvider
            ? `「${pendingDeleteProvider.name}」及其下的全部模型将被删除，且无法恢复。`
            : undefined
        }
        confirmLabel="删除"
        destructive
        onConfirm={() => {
          if (pendingDeleteProvider) void handleDeleteProvider(pendingDeleteProvider);
        }}
      />

      <ConfirmDialog
        open={pendingDeleteModel !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDeleteModel(null);
        }}
        title="删除模型"
        description={
          pendingDeleteModel
            ? `模型「${pendingDeleteModel.display_name}」将从列表中移除；下次同步仍可能重新出现。`
            : undefined
        }
        confirmLabel="删除"
        destructive
        onConfirm={() => {
          if (pendingDeleteModel) void handleDeleteModel(pendingDeleteModel);
        }}
      />
    </section>
  );
}

interface ProviderCardProps {
  provider: AdminProvider;
  models: AdminModel[];
  modelCount: number;
  busy: string | null;
  notice: ProviderNotice | undefined;
  newModelId: string;
  onNewModelIdChange: (value: string) => void;
  onTest: () => void;
  onSync: () => void;
  onEdit: () => void;
  onRequestDelete: () => void;
  onToggleModel: (model: AdminModel) => void;
  onSetDefault: (model: AdminModel) => void;
  onRequestDeleteModel: (model: AdminModel) => void;
  onAddModel: () => void;
  onDismissNotice: () => void;
}

/** 单个提供商：信息头 + 操作区 + 模型清单。模型清单始终展开，省掉「展开才知道有没有数据」的一步。 */
function ProviderCard({
  provider,
  models,
  modelCount,
  busy,
  notice,
  newModelId,
  onNewModelIdChange,
  onTest,
  onSync,
  onEdit,
  onRequestDelete,
  onToggleModel,
  onSetDefault,
  onRequestDeleteModel,
  onAddModel,
  onDismissNotice,
}: ProviderCardProps) {
  const isBusy = busy !== null;
  const isTesting = busy === `test:${provider.id}`;
  const isSyncing = busy === `sync:${provider.id}`;

  return (
    <article className="rounded-xl border border-border/70 bg-card/50">
      <div className="flex flex-wrap items-start justify-between gap-3 p-4">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-sm font-medium text-foreground">{provider.name}</h3>
            <span className="font-mono text-[11px] text-muted-foreground">{provider.slug}</span>
            <span
              className={
                provider.enabled
                  ? 'rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary'
                  : 'rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground'
              }
            >
              {provider.enabled ? '已启用' : '已停用'}
            </span>
          </div>

          <p className="break-all text-xs text-muted-foreground">{provider.base_url}</p>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <KeyRound className="size-3" aria-hidden="true" />
              <span className="font-mono">{provider.api_key_hint ?? '未设置'}</span>
            </span>
            <span>{modelCount} 个模型</span>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            onClick={onTest}
            disabled={isBusy}
            aria-label={`测试 ${provider.name} 的连接`}
            className="cursor-pointer gap-1"
          >
            {isTesting ? (
              <Loader2 className="size-3 animate-spin" aria-hidden="true" />
            ) : (
              <PlugZap className="size-3" aria-hidden="true" />
            )}
            测试连接
          </Button>
          <Button
            variant="outline"
            size="xs"
            onClick={onSync}
            disabled={isBusy}
            aria-label={`同步 ${provider.name} 的模型`}
            className="cursor-pointer gap-1"
          >
            {isSyncing ? (
              <Loader2 className="size-3 animate-spin" aria-hidden="true" />
            ) : (
              <RefreshCw className="size-3" aria-hidden="true" />
            )}
            同步模型
          </Button>
          <Button
            variant="outline"
            size="xs"
            onClick={onEdit}
            disabled={isBusy}
            aria-label={`编辑 ${provider.name}`}
            className="cursor-pointer gap-1"
          >
            <Pencil className="size-3" aria-hidden="true" />
            编辑
          </Button>
          <Button
            variant="destructive"
            size="xs"
            onClick={onRequestDelete}
            disabled={isBusy}
            aria-label={`删除 ${provider.name}`}
            className="cursor-pointer gap-1"
          >
            <Trash2 className="size-3" aria-hidden="true" />
            删除
          </Button>
        </div>
      </div>

      {notice && (
        <div
          role={notice.tone === 'error' ? 'alert' : 'status'}
          className={`mx-4 mb-3 flex items-start gap-2 rounded-lg border px-2.5 py-1.5 text-[11px] ${noticeToneClass[notice.tone]}`}
        >
          <span className="min-w-0 flex-1 break-words">{notice.text}</span>
          <button
            type="button"
            onClick={onDismissNotice}
            aria-label="关闭提示"
            className="shrink-0 cursor-pointer hover:opacity-70"
          >
            <X className="size-3" aria-hidden="true" />
          </button>
        </div>
      )}

      <div className="space-y-2 border-t border-border/70 p-3">
        {models.length === 0 ? (
          <p className="px-1 py-2 text-xs text-muted-foreground">
            暂无模型：点「同步模型」从上游拉取，或在下方手动添加。
          </p>
        ) : (
          <ul className="space-y-1.5">
            {models.map((model) => (
              <li
                key={model.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-muted/40 px-2.5 py-2"
              >
                <div className="min-w-0 flex-1 space-y-0.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate text-xs font-medium text-foreground">
                      {model.display_name}
                    </span>
                    {model.reasoning && (
                      <span className="rounded-full bg-violet-500/10 px-1.5 py-0.5 text-[10px] font-medium text-violet-600 dark:text-violet-300">
                        推理
                      </span>
                    )}
                    {model.is_default && (
                      <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-400">
                        <Star className="size-2.5 fill-current" aria-hidden="true" />
                        默认
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
                    <code className="break-all font-mono">{model.model_id}</code>
                    <span>{formatContextWindow(model.context_window)}</span>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-1.5">
                  <ToggleSwitch
                    checked={model.enabled}
                    onCheckedChange={() => onToggleModel(model)}
                    label={`${model.enabled ? '停用' : '启用'}模型 ${model.display_name}`}
                    disabled={isBusy}
                  />
                  {model.enabled && !model.is_default && (
                    <Button
                      variant="ghost"
                      size="xs"
                      onClick={() => onSetDefault(model)}
                      disabled={isBusy}
                      aria-label={`将 ${model.display_name} 设为默认模型`}
                      className="cursor-pointer"
                    >
                      设为默认
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() => onRequestDeleteModel(model)}
                    disabled={isBusy}
                    aria-label={`删除模型 ${model.display_name}`}
                    className="cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="size-3" aria-hidden="true" />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}

        {/* 用 form 而不是裸按钮：输入框里按回车也能添加，符合表单直觉 */}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onAddModel();
          }}
          className="flex flex-wrap items-center gap-1.5"
        >
          <Input
            value={newModelId}
            onChange={(event) => onNewModelIdChange(event.target.value)}
            placeholder="上游模型 ID，如 deepseek-chat"
            aria-label={`要添加到 ${provider.name} 的上游模型 ID`}
            spellCheck={false}
            autoComplete="off"
            className="h-8 min-w-[12rem] flex-1 text-xs"
          />
          <Button
            type="submit"
            variant="outline"
            size="xs"
            disabled={isBusy}
            aria-label={`添加模型到 ${provider.name}`}
            className="cursor-pointer gap-1"
          >
            <Plus className="size-3" aria-hidden="true" />
            添加
          </Button>
        </form>
      </div>
    </article>
  );
}
