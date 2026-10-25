import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ChevronDown,
  ChevronRight,
  KeyRound,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Search,
  Star,
  Trash2,
  X,
} from 'lucide-react';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import { ProviderDialog } from './ProviderDialog';
import { ModelDialog } from './ModelDialog';
import { ToggleSwitch } from './ToggleSwitch';
import { Badge } from './table-parts';
import {
  PageShell,
  TableStatusRow,
  TableWrap,
  TD_CLASS,
  TH_CLASS,
  Toolbar,
} from './table-parts';
import { formatDateTime } from '../../lib/format';
import {
  createProviderModel,
  deleteModel,
  deleteProvider,
  describeFailure,
  describeProviderSync,
  filterAdminModels,
  VISIBLE_MODELS_COLLAPSED,
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

const PROVIDER_COL_COUNT = 6;

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

  /** 展开模型清单的提供商；同一时间展开一行，管理动作都针对一个提供商。 */
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingProvider, setEditingProvider] = useState<AdminProvider | null>(null);
  const [editingModel, setEditingModel] = useState<AdminModel | null>(null);
  const [pendingDeleteProvider, setPendingDeleteProvider] = useState<AdminProvider | null>(null);
  const [pendingDeleteModel, setPendingDeleteModel] = useState<AdminModel | null>(null);

  /** 提供商过滤（名称 / slug，客户端过滤：提供商数量级很小）。 */
  const [providerQuery, setProviderQuery] = useState('');

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
          // 「连不上」是测试的预期结果之一，原因留在该行下方比挂到页面顶部更贴近操作
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
          try {
            const result = await syncProvider(provider.id);
            setNotice(provider.id, { tone: 'success', text: describeProviderSync(result) });
          } catch (err) {
            // 与「测试连接」一致：失败原因贴着该行显示；页面顶部只留给
            // 不属于任何一行的问题（例如列表加载失败）。
            setNotice(provider.id, { tone: 'error', text: describeFailure(err) });
          }
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

  const filteredProviders = useMemo(() => {
    if (providers === null) return null;
    const needle = providerQuery.trim().toLowerCase();
    if (!needle) return providers;
    return providers.filter(
      (provider) =>
        provider.name.toLowerCase().includes(needle) || provider.slug.toLowerCase().includes(needle),
    );
  }, [providers, providerQuery]);

  return (
    <PageShell
      title="模型提供商"
      description="维护上游接口、密钥与可路由的模型清单。展开一行可以管理该提供商下的模型（启停、设默认、手动添加）。"
      actions={
        <Button
          size="sm"
          onClick={openCreateDialog}
          disabled={isBusy}
          className="cursor-pointer gap-1.5"
        >
          <Plus className="size-3.5" aria-hidden="true" />
          新增提供商
        </Button>
      }
    >
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

      <Toolbar aria-label="提供商筛选">
        <div className="relative w-full max-w-xs min-w-[12rem] flex-1">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            value={providerQuery}
            onChange={(event) => setProviderQuery(event.target.value)}
            placeholder="按名称或 slug 过滤"
            aria-label="按名称或 slug 过滤提供商"
            autoComplete="off"
            className="h-8 pl-8 md:text-xs"
          />
          {providerQuery && (
            <button
              type="button"
              onClick={() => setProviderQuery('')}
              aria-label="清除过滤"
              className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
            >
              <X className="size-3.5" aria-hidden="true" />
            </button>
          )}
        </div>
        {filteredProviders !== null && (
          <span className="text-[11px] text-muted-foreground">
            共 {filteredProviders.length} 个{providerQuery.trim() ? `（总 ${providers?.length ?? 0} 个）` : ''}
          </span>
        )}
      </Toolbar>

      <TableWrap minWidth="62rem">
        <thead>
          <tr>
            <th className={`${TH_CLASS} w-10`} aria-label="展开" />
            <th className={TH_CLASS}>提供商</th>
            <th className={TH_CLASS}>状态</th>
            <th className={TH_CLASS}>模型</th>
            <th className={TH_CLASS}>密钥指纹</th>
            <th className={TH_CLASS}>创建时间</th>
            <th className={`${TH_CLASS} text-right`}>操作</th>
          </tr>
        </thead>
        <tbody>
          {filteredProviders === null ? (
            !loadError && <TableStatusRow colSpan={PROVIDER_COL_COUNT + 1} loading text="正在加载提供商…" />
          ) : filteredProviders.length === 0 ? (
            <TableStatusRow
              colSpan={PROVIDER_COL_COUNT + 1}
              text={
                providerQuery.trim()
                  ? `没有匹配「${providerQuery.trim()}」的提供商`
                  : '还没有配置任何提供商，点右上角「新增提供商」开始。'
              }
            />
          ) : (
            filteredProviders.map((provider) => {
              const models = modelsByProvider[provider.id] ?? [];
              const enabledCount = models.filter((model) => model.enabled).length;
              const expanded = expandedId === provider.id;
              const notice = notices[provider.id];

              return (
                <ProviderRowGroup
                  key={provider.id}
                  provider={provider}
                  models={models}
                  modelCount={modelCountOf(provider)}
                  enabledCount={enabledCount}
                  expanded={expanded}
                  notice={notice}
                  busy={busy}
                  newModelId={newModelIds[provider.id] ?? ''}
                  onToggleExpand={() => setExpandedId(expanded ? null : provider.id)}
                  onNewModelIdChange={(value) =>
                    setNewModelIds((prev) => ({ ...prev, [provider.id]: value }))
                  }
                  onTest={() => handleTest(provider)}
                  onSync={() => handleSync(provider)}
                  onEdit={() => openEditDialog(provider)}
                  onRequestDelete={() => setPendingDeleteProvider(provider)}
                  onToggleModel={handleToggleModel}
                  onSetDefault={handleSetDefault}
                  onEditModel={setEditingModel}
                  onRequestDeleteModel={setPendingDeleteModel}
                  onAddModel={() => handleAddModel(provider.id)}
                  onDismissNotice={() => setNotice(provider.id, null)}
                />
              );
            })
          )}
        </tbody>
      </TableWrap>

      <ProviderDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        provider={editingProvider}
        onSaved={reload}
      />

      <ModelDialog
        open={editingModel !== null}
        onOpenChange={(open) => {
          if (!open) setEditingModel(null);
        }}
        model={editingModel}
        providerName={
          providers?.find((provider) => provider.id === editingModel?.provider_id)?.name ?? ''
        }
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
    </PageShell>
  );
}

interface ProviderRowGroupProps {
  provider: AdminProvider;
  models: AdminModel[];
  modelCount: number;
  enabledCount: number;
  expanded: boolean;
  notice: ProviderNotice | undefined;
  busy: string | null;
  newModelId: string;
  onToggleExpand: () => void;
  onNewModelIdChange: (value: string) => void;
  onTest: () => void;
  onSync: () => void;
  onEdit: () => void;
  onRequestDelete: () => void;
  onToggleModel: (model: AdminModel) => void;
  onSetDefault: (model: AdminModel) => void;
  onEditModel: (model: AdminModel) => void;
  onRequestDeleteModel: (model: AdminModel) => void;
  onAddModel: () => void;
  onDismissNotice: () => void;
}

/** 一个提供商 = 主行 + 可选提示行 + 展开行（模型子表）。 */
function ProviderRowGroup({
  provider,
  models,
  modelCount,
  enabledCount,
  expanded,
  notice,
  busy,
  newModelId,
  onToggleExpand,
  onNewModelIdChange,
  onTest,
  onSync,
  onEdit,
  onRequestDelete,
  onToggleModel,
  onSetDefault,
  onEditModel,
  onRequestDeleteModel,
  onAddModel,
  onDismissNotice,
}: ProviderRowGroupProps) {
  const isBusy = busy !== null;
  const isTesting = busy === `test:${provider.id}`;
  const isSyncing = busy === `sync:${provider.id}`;
  // 对话侧的模型列表只包含「提供商启用 + 模型启用」的行。后台展示全部（要能管理停用项），
  // 所以这里把两个数一起写出来，否则「后台 6 个 / 聊天框 5 款」看起来就像两个列表不同步。
  const modelSummary = provider.enabled
    ? `${enabledCount} / ${modelCount}`
    : `${modelCount}（提供商已停用）`;

  return (
    <>
      <tr className="hover:bg-accent/30">
        <td className={TD_CLASS}>
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            aria-label={expanded ? `收起 ${provider.name} 的模型清单` : `展开 ${provider.name} 的模型清单`}
            className="flex size-6 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {expanded ? (
              <ChevronDown className="size-4" aria-hidden="true" />
            ) : (
              <ChevronRight className="size-4" aria-hidden="true" />
            )}
          </button>
        </td>
        <td className={TD_CLASS}>
          <div className="text-sm font-medium">{provider.name}</div>
          <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{provider.slug}</div>
          <div className="mt-0.5 max-w-72 truncate text-[11px] text-muted-foreground" title={provider.base_url}>
            {provider.base_url}
          </div>
        </td>
        <td className={TD_CLASS}>
          <Badge tone={provider.enabled ? 'ok' : 'muted'}>{provider.enabled ? '已启用' : '已停用'}</Badge>
        </td>
        <td className={TD_CLASS}>
          <span title="只有已启用的模型才会出现在对话的模型选择器里">{modelSummary}</span>
        </td>
        <td className={TD_CLASS}>
          <span className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground">
            <KeyRound className="size-3" aria-hidden="true" />
            {provider.api_key_hint ?? '未设置'}
          </span>
        </td>
        <td className={`${TD_CLASS} whitespace-nowrap text-xs text-muted-foreground`}>
          {formatDateTime(provider.created_at)}
        </td>
        <td className={`${TD_CLASS} text-right`}>
          <div className="inline-flex items-center gap-1">
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
              variant="ghost"
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
              variant="ghost"
              size="xs"
              onClick={onRequestDelete}
              disabled={isBusy}
              aria-label={`删除 ${provider.name}`}
              className="cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="size-3" aria-hidden="true" />
            </Button>
          </div>
        </td>
      </tr>

      {notice && (
        <tr>
          <td colSpan={PROVIDER_COL_COUNT + 1} className="px-3 py-1.5">
            <div
              role={notice.tone === 'error' ? 'alert' : 'status'}
              className={`flex items-start gap-2 rounded-lg border px-2.5 py-1.5 text-[11px] ${noticeToneClass[notice.tone]}`}
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
          </td>
        </tr>
      )}

      {expanded && (
        <tr>
          <td colSpan={PROVIDER_COL_COUNT + 1} className="bg-muted/20 px-3 py-3 md:pl-14">
            <ModelsArea
              provider={provider}
              models={models}
              busy={busy}
              newModelId={newModelId}
              onNewModelIdChange={onNewModelIdChange}
              onToggleModel={onToggleModel}
              onSetDefault={onSetDefault}
              onEditModel={onEditModel}
              onRequestDeleteModel={onRequestDeleteModel}
              onAddModel={onAddModel}
            />
          </td>
        </tr>
      )}
    </>
  );
}

interface ModelsAreaProps {
  provider: AdminProvider;
  models: AdminModel[];
  busy: string | null;
  newModelId: string;
  onNewModelIdChange: (value: string) => void;
  onToggleModel: (model: AdminModel) => void;
  onSetDefault: (model: AdminModel) => void;
  onEditModel: (model: AdminModel) => void;
  onRequestDeleteModel: (model: AdminModel) => void;
  onAddModel: () => void;
}

/** 展开行里的模型子表：过滤、折叠上限与手动添加都在这里。 */
function ModelsArea({
  provider,
  models,
  busy,
  newModelId,
  onNewModelIdChange,
  onToggleModel,
  onSetDefault,
  onEditModel,
  onRequestDeleteModel,
  onAddModel,
}: ModelsAreaProps) {
  const isBusy = busy !== null;
  const [modelFilter, setModelFilter] = useState('');
  const [modelsExpanded, setModelsExpanded] = useState(false);
  const visibleModels = filterAdminModels(models, modelFilter);
  // 默认折叠：一个 LiteLLM 网关常常暴露几十上百个模型，全部平铺会把页面变成一堵墙，
  // 而管理动作（启停 / 设默认 / 编辑）都是针对某一个模型的。过滤优先，其次才是展开。
  const collapsed =
    modelFilter.trim() === '' && !modelsExpanded && visibleModels.length > VISIBLE_MODELS_COLLAPSED;
  const shownModels = collapsed ? visibleModels.slice(0, VISIBLE_MODELS_COLLAPSED) : visibleModels;

  return (
    <div className="space-y-2">
      {models.length === 0 ? (
        <p className="px-1 py-2 text-xs text-muted-foreground">
          暂无模型：点「同步模型」从上游拉取，或在下方手动添加。
        </p>
      ) : (
        <>
          {/* 模型过滤：模型一多，找某个具体型号只能靠滚动与肉眼扫描 */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-full max-w-xs min-w-[12rem] flex-1 sm:flex-none">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                value={modelFilter}
                onChange={(event) => setModelFilter(event.target.value)}
                placeholder="过滤模型（ID 或名称）"
                aria-label={`过滤 ${provider.name} 的模型`}
                className="h-8 pl-8 md:text-xs"
              />
              {modelFilter && (
                <button
                  type="button"
                  onClick={() => setModelFilter('')}
                  aria-label="清除模型过滤"
                  className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <X className="size-3.5" aria-hidden="true" />
                </button>
              )}
            </div>
            {modelFilter.trim() !== '' && (
              <p role="status" className="text-[11px] text-muted-foreground">
                匹配 {visibleModels.length} / {models.length} 个模型
              </p>
            )}
          </div>

          <div className="overflow-hidden rounded-lg border border-border/70 bg-background">
            <table className="w-full border-collapse text-left" style={{ minWidth: '40rem' }}>
              <thead>
                <tr>
                  <th className={TH_CLASS}>模型</th>
                  <th className={TH_CLASS}>上下文窗口</th>
                  <th className={TH_CLASS}>启用</th>
                  <th className={`${TH_CLASS} text-right`}>操作</th>
                </tr>
              </thead>
              <tbody>
                {visibleModels.length === 0 ? (
                  <TableStatusRow colSpan={4} text={`没有匹配「${modelFilter.trim()}」的模型。`} />
                ) : (
                  shownModels.map((model) => (
                    <tr key={model.id} className="hover:bg-accent/30">
                      <td className={TD_CLASS}>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-xs font-medium">{model.display_name}</span>
                          {model.reasoning && <Badge tone="primary">推理</Badge>}
                          {model.is_default && (
                            <Badge tone="muted">
                              <span className="inline-flex items-center gap-0.5">
                                <Star className="size-2.5 fill-current" aria-hidden="true" />
                                默认
                              </span>
                            </Badge>
                          )}
                        </div>
                        <code className="mt-0.5 block break-all font-mono text-[11px] text-muted-foreground">
                          {model.model_id}
                        </code>
                      </td>
                      <td className={`${TD_CLASS} text-xs text-muted-foreground`}>
                        {formatContextWindow(model.context_window)}
                      </td>
                      <td className={TD_CLASS}>
                        <ToggleSwitch
                          checked={model.enabled}
                          onCheckedChange={() => onToggleModel(model)}
                          label={`${model.enabled ? '停用' : '启用'}模型 ${model.display_name}（${model.model_id}）`}
                          disabled={isBusy}
                        />
                      </td>
                      <td className={`${TD_CLASS} text-right`}>
                        <div className="inline-flex items-center gap-1">
                          {model.enabled && !model.is_default && (
                            <Button
                              variant="ghost"
                              size="xs"
                              onClick={() => onSetDefault(model)}
                              disabled={isBusy}
                              aria-label={`将 ${model.display_name}（${model.model_id}）设为默认模型`}
                              className="cursor-pointer"
                            >
                              设为默认
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="xs"
                            onClick={() => onEditModel(model)}
                            disabled={isBusy}
                            aria-label={`编辑模型 ${model.display_name}（${model.model_id}）`}
                            className="cursor-pointer"
                          >
                            <Pencil className="size-3" aria-hidden="true" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="xs"
                            onClick={() => onRequestDeleteModel(model)}
                            disabled={isBusy}
                            aria-label={`删除模型 ${model.display_name}（${model.model_id}）`}
                            className="cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
                          >
                            <Trash2 className="size-3" aria-hidden="true" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {collapsed && (
            <button
              type="button"
              onClick={() => setModelsExpanded(true)}
              className="w-full cursor-pointer rounded-lg border border-dashed border-border/70 px-2 py-1.5 text-[11px] text-muted-foreground hover:border-primary/40 hover:text-foreground"
            >
              展开全部（还有 {visibleModels.length - VISIBLE_MODELS_COLLAPSED} 个）
            </button>
          )}
          {!collapsed &&
            modelsExpanded &&
            modelFilter.trim() === '' &&
            models.length > VISIBLE_MODELS_COLLAPSED && (
              <button
                type="button"
                onClick={() => setModelsExpanded(false)}
                className="w-full cursor-pointer rounded-lg border border-dashed border-border/70 px-2 py-1.5 text-[11px] text-muted-foreground hover:border-primary/40 hover:text-foreground"
              >
                收起，只看前 {VISIBLE_MODELS_COLLAPSED} 个
              </button>
            )}
        </>
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
          className="h-8 min-w-[12rem] max-w-sm flex-1 md:text-xs"
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
  );
}
