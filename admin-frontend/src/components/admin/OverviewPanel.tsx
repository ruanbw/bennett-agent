/**
 * 概览页：进后台先看「现在是什么状况」，而不是直接落进一个可编辑的列表。
 *
 * 内容都是**已有接口的派生**，没有新增后端端点：
 *
 * - 统计卡：提供商总数 / 已启用、模型总数 / 可路由（提供商与模型都启用）、用户总数、
 *   被封禁账号数。数字来自两个列表接口的组合，一次查询算完。
 * - 需要处理：没有提供商的模型、没有设置默认模型、被封禁账号、无法路由的提供商
 *   （启用但零可用模型：对话里选不到任何模型，属于「看起来正常但实际坏了」）。
 * - 提供商一览：只读的小表，点进去才是管理页。
 *
 * 刻意不做图表：这个后台的数据量级是个位数到几十，折线图只会把「3 个提供商」画成
 * 一条直线。要看的异常用清单列出来，比图更直接。
 */

import { useMemo } from 'react';
import { Link } from 'react-router';
import { useQueries, useQuery } from '@tanstack/react-query';
import {
  ArrowRight,
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  Server,
  Users,
  Boxes,
  UserX,
} from 'lucide-react';

import { buttonVariants } from '@/components/ui/button';
import { queryKeys } from '@/lib/query-keys';
import {
  describeFailure,
  listProviderModels,
  listProviders,
  listUsers,
  type AdminModel,
  type AdminProvider,
} from '@/lib/admin';
import { Badge, InlineAlert, PageShell, TableWrap, TD_CLASS, TH_CLASS, TR_CLASS } from './table-parts';

export interface OverviewPanelProps {
  canManageRoles: boolean;
}

export function OverviewPanel({ canManageRoles }: OverviewPanelProps) {
  const providersQuery = useQuery({ queryKey: queryKeys.providers, queryFn: listProviders });

  const modelQueries = useQueries({
    queries: (providersQuery.data ?? []).map((provider) => ({
      queryKey: queryKeys.providerModels(provider.id),
      queryFn: () => listProviderModels(provider.id),
      enabled: providersQuery.isSuccess,
    })),
  });

  const bannedQuery = useQuery({
    queryKey: queryKeys.users({ query: '', status: 'banned', limit: 1, offset: 0 }),
    queryFn: () => listUsers({ status: 'banned', limit: 1, offset: 0 }),
  });

  const allUsersQuery = useQuery({
    queryKey: queryKeys.users({ query: '', status: 'all', limit: 1, offset: 0 }),
    queryFn: () => listUsers({ status: 'all', limit: 1, offset: 0 }),
  });

  // 用 memo 固定引用：下面三个 useMemo 都以它为依赖，每次渲染换新数组会让它们全部失效
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data]);
  const modelsByProvider = useMemo(() => {
    const map = new Map<string, AdminModel[]>();
    providers.forEach((provider, index) => {
      const result = modelQueries[index];
      if (result?.data) map.set(provider.id, result.data);
    });
    return map;
  }, [providers, modelQueries]);

  const modelsLoading = providers.length > 0 && modelQueries.some((query) => query.isPending);
  const models = Array.from(modelsByProvider.values()).flat();

  const stats = useMemo(() => {
    const enabledProviders = providers.filter((provider) => provider.enabled);
    const routable = models.filter(
      (model) => model.enabled && providers.find((p) => p.id === model.provider_id)?.enabled,
    );
    return {
      providers: providers.length,
      enabledProviders: enabledProviders.length,
      models: models.length,
      routable: routable.length,
    };
  }, [providers, models]);

  /** 「需要处理」清单：每一条都是一个具体、可点的下一步。 */
  const issues = useMemo(() => {
    const list: Array<{ id: string; tone: 'warn' | 'danger'; title: string; detail: string; to: string; cta: string }> = [];

    if (providersQuery.isSuccess && providers.length === 0) {
      list.push({
        id: 'no-provider',
        tone: 'danger',
        title: '还没有配置任何模型提供商',
        detail: '对话里选不到任何模型，用户无法开始提问。',
        to: '/providers',
        cta: '去添加',
      });
    }

    const silentProviders = providers.filter(
      (provider) => provider.enabled && (modelsByProvider.get(provider.id)?.filter((m) => m.enabled).length ?? 0) === 0,
    );
    if (silentProviders.length > 0) {
      list.push({
        id: 'silent-provider',
        tone: 'warn',
        title: `${silentProviders.length} 个已启用的提供商没有可用模型`,
        detail: `${silentProviders.map((p) => p.name).join('、')}：对话里选不到它们的模型，点「同步模型」拉一次上游清单。`,
        to: '/providers',
        cta: '去同步',
      });
    }

    if (models.length > 0 && !models.some((model) => model.is_default && model.enabled)) {
      list.push({
        id: 'no-default',
        tone: 'warn',
        title: '没有设置默认模型',
        detail: '新会话的初始模型会取路由后的第一个可用模型，结果不可预测。',
        to: '/providers',
        cta: '去设置',
      });
    }

    const banned = bannedQuery.data?.total ?? 0;
    if (banned > 0) {
      list.push({
        id: 'banned',
        tone: 'warn',
        title: `${banned} 个账号处于封禁状态`,
        detail: '它们无法登录；确认是不是预期的。',
        to: '/users',
        cta: '查看账号',
      });
    }

    return list;
  }, [providers, models, modelsByProvider, providersQuery.isSuccess, bannedQuery.data?.total]);

  const error = providersQuery.error ?? bannedQuery.error ?? allUsersQuery.error;

  return (
    <PageShell
      title="概览"
      description="当前配置的总体状况，以及需要处理的问题。点卡片进对应的管理页。"
      actions={
        <Link to="/providers" className={buttonVariants({ variant: 'outline', size: 'sm' })}>
          管理提供商
          <ArrowRight className="size-3.5" aria-hidden="true" />
        </Link>
      }
    >
      {error && <InlineAlert>{describeFailure(error)}</InlineAlert>}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={Server}
          label="提供商"
          value={stats.providers}
          hint={stats.providers === 0 ? '尚未配置' : `已启用 ${stats.enabledProviders} 个`}
          to="/providers"
          loading={providersQuery.isPending}
        />
        <StatCard
          icon={Boxes}
          label="模型总数"
          value={stats.models}
          hint={modelsLoading ? '正在统计…' : `可路由 ${stats.routable} 个`}
          to="/providers"
          loading={providersQuery.isPending || modelsLoading}
        />
        <StatCard
          icon={Users}
          label="用户"
          value={allUsersQuery.data?.total ?? 0}
          hint={canManageRoles ? '可在此封禁与管理角色' : '角色管理需超级管理员'}
          to="/users"
          loading={allUsersQuery.isPending}
        />
        <StatCard
          icon={UserX}
          label="已封禁"
          value={bannedQuery.data?.total ?? 0}
          hint="无法登录，会话已失效"
          to="/users"
          loading={bannedQuery.isPending}
        />
      </div>

      <section className="space-y-2" aria-labelledby="issues-heading">
        <h3 id="issues-heading" className="text-xs font-medium tracking-wide text-muted-foreground">
          需要处理
        </h3>
        {issues.length === 0 ? (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-3 text-xs text-emerald-700 dark:text-emerald-300">
            <CheckCircle2 className="size-4 shrink-0" aria-hidden="true" />
            {providersQuery.isPending || modelsLoading
              ? '正在检查…'
              : '没有发现需要处理的问题。'}
          </div>
        ) : (
          <ul className="space-y-2">
            {issues.map((issue) => (
              <li
                key={issue.id}
                className="flex flex-wrap items-start gap-2 rounded-lg border border-border/70 bg-card px-3 py-2.5"
              >
                <span
                  className={
                    issue.tone === 'danger'
                      ? 'mt-0.5 shrink-0 text-destructive'
                      : 'mt-0.5 shrink-0 text-amber-600 dark:text-amber-400'
                  }
                >
                  {issue.tone === 'danger' ? (
                    <CircleSlash className="size-4" aria-hidden="true" />
                  ) : (
                    <AlertTriangle className="size-4" aria-hidden="true" />
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-foreground">{issue.title}</div>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{issue.detail}</p>
                </div>
                <Link to={issue.to} className={buttonVariants({ variant: 'outline', size: 'xs' })}>
                  {issue.cta}
                  <ArrowRight className="size-3" aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-2" aria-labelledby="providers-heading">
        <div className="flex items-center justify-between">
          <h3 id="providers-heading" className="text-xs font-medium tracking-wide text-muted-foreground">
            提供商一览
          </h3>
          <Link to="/providers" className="text-[11px] text-primary hover:underline">
            查看全部
          </Link>
        </div>

        <TableWrap minWidth="40rem">
          <thead>
            <tr>
              <th className={TH_CLASS}>提供商</th>
              <th className={TH_CLASS}>状态</th>
              <th className={TH_CLASS}>可用模型</th>
              <th className={TH_CLASS}>默认模型</th>
            </tr>
          </thead>
          <tbody>
            {providersQuery.isPending ? (
              <tr>
                <td colSpan={4} className="px-3 py-10 text-center text-xs text-muted-foreground">
                  正在加载提供商…
                </td>
              </tr>
            ) : providers.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-3 py-10 text-center text-xs text-muted-foreground">
                  还没有配置任何提供商。
                </td>
              </tr>
            ) : (
              providers.map((provider) => {
                const list = modelsByProvider.get(provider.id) ?? [];
                const enabled = list.filter((model) => model.enabled);
                const defaultModel = enabled.find((model) => model.is_default);
                return (
                  <tr key={provider.id} className={TR_CLASS}>
                    <td className={TD_CLASS}>
                      <div className="text-xs font-medium">{provider.name}</div>
                      <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{provider.slug}</div>
                    </td>
                    <td className={TD_CLASS}>
                      <ProviderStatus provider={provider} enabledModels={enabled.length} />
                    </td>
                    <td className={`${TD_CLASS} text-xs tabular-nums`}>
                      {modelsLoading && !modelsByProvider.has(provider.id) ? (
                        <span className="text-muted-foreground">统计中…</span>
                      ) : (
                        <>
                          {provider.enabled ? enabled.length : <span className="text-muted-foreground">—</span>}
                          <span className="text-muted-foreground"> / {list.length}</span>
                        </>
                      )}
                    </td>
                    <td className={`${TD_CLASS} text-xs`}>
                      {defaultModel ? (
                        defaultModel.display_name
                      ) : (
                        <span className="text-muted-foreground">未设置</span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </TableWrap>
      </section>
    </PageShell>
  );
}

function ProviderStatus({ provider, enabledModels }: { provider: AdminProvider; enabledModels: number }) {
  if (!provider.enabled) return <Badge tone="muted">已停用</Badge>;
  if (enabledModels === 0) return <Badge tone="warn">无可用模型</Badge>;
  return <Badge tone="ok">正常</Badge>;
}

function StatCard({
  icon: Icon,
  label,
  value,
  hint,
  to,
  loading,
}: {
  icon: typeof Server;
  label: string;
  value: number;
  hint: string;
  to: string;
  loading: boolean;
}) {
  return (
    <Link
      to={to}
      className="group flex items-start gap-3 rounded-lg border border-border/70 bg-card px-4 py-3.5 transition-colors hover:border-primary/40 hover:bg-accent/30 focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
    >
      <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Icon className="size-4" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[11px] text-muted-foreground">{label}</span>
        <span className="mt-0.5 block text-xl font-semibold tabular-nums">
          {loading ? <span className="text-muted-foreground">—</span> : value}
        </span>
        <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">{hint}</span>
      </span>
      <ArrowRight
        className="mt-1 size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"
        aria-hidden="true"
      />
    </Link>
  );
}
