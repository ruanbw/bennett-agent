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

  // 用 memo 固定引用：下面几个 useMemo 都以它为依赖，每次渲染换新数组会让它们全部失效
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data]);

  /**
   * 每个提供商的模型清单状态。
   *
   * `models === null` 且无 `error` = 还在加载。**必须与「有清单、但全部停用」区分开**：
   * 两者在下面「没有可用模型」那条告警里含义完全相反（一个是不确定，一个是真有问题）。
   */
  const modelStateByProvider = useMemo(() => {
    const map = new Map<string, { models: AdminModel[] | null; error: unknown }>();
    providers.forEach((provider, index) => {
      const result = modelQueries[index];
      map.set(provider.id, {
        models: result?.data ?? null,
        error: result?.isError ? result.error : null,
      });
    });
    return map;
  }, [providers, modelQueries]);

  /** 所有提供商的模型清单都到齐了吗（告警必须等这个为真才能下结论）。 */
  const allModelsLoaded =
    providers.length > 0 && providers.every((p) => modelStateByProvider.get(p.id)?.models != null);
  /** 有清单拉失败的：这一栏就不能说「没有问题」，那是把「没查到」说成「没毛病」。 */
  const failedModelCount = providers.filter((p) => modelStateByProvider.get(p.id)?.error != null).length;

  const stats = useMemo(() => {
    const enabledProviders = providers.filter((provider) => provider.enabled).length;
    // 模型总数直接用后端的 model_count：不必等 N 个清单请求，也不会在加载过程中
    // 从 0 一路涨到真实值（那样每次进页面数字都变，像在抖）
    const totalModels = providers.reduce((sum, provider) => sum + provider.model_count, 0);

    return {
      providers: providers.length,
      enabledProviders,
      totalModels,
    };
  }, [providers]);

  /** 可路由模型数 = 启用的提供商下启用的模型。清单没齐时给 null，界面上显示「统计中」。 */
  const routableCount = useMemo(() => {
    if (!allModelsLoaded) return null;
    return providers.reduce((sum, provider) => {
      if (!provider.enabled) return sum;
      const models = modelStateByProvider.get(provider.id)?.models ?? [];
      return sum + models.filter((model) => model.enabled).length;
    }, 0);
  }, [allModelsLoaded, providers, modelStateByProvider]);

  const defaultModel = useMemo(() => {
    if (!allModelsLoaded) return undefined;
    for (const provider of providers) {
      const found = modelStateByProvider.get(provider.id)?.models?.find((m) => m.is_default);
      if (found) return found;
    }
    return null;
  }, [allModelsLoaded, providers, modelStateByProvider]);

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

    // 清单拉取失败：此时任何「有没有可用模型 / 有没有默认模型」的结论都不可信，
    // 先把这件事本身报出来
    if (failedModelCount > 0) {
      list.push({
        id: 'model-query-failed',
        tone: 'danger',
        title: `${failedModelCount} 个提供商的模型清单拉取失败`,
        detail: '下面的「没有可用模型」「没有默认模型」两项因此无法判断，请到提供商页重试。',
        to: '/providers',
        cta: '去重试',
      });
    }

    // 只对「清单已到手」的提供商判断：没到手不等于 0 个可用模型
    const silentProviders = providers.filter((provider) => {
      const models = modelStateByProvider.get(provider.id)?.models;
      return provider.enabled && models != null && models.every((m) => !m.enabled);
    });
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

    // 「没有默认模型」必须等全部清单到齐：只看一半时可能漏掉后面那个设了默认的
    if (allModelsLoaded && defaultModel === null) {
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
  }, [providers, modelStateByProvider, allModelsLoaded, defaultModel, failedModelCount, providersQuery.isSuccess, bannedQuery.data?.total]);

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
          value={stats.totalModels}
          hint={
            routableCount === null
              ? '正在统计可路由数量…'
              : `其中可路由 ${routableCount} 个`
          }
          to="/providers"
          loading={providersQuery.isPending}
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
            {providersQuery.isPending || !allModelsLoaded
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
                const state = modelStateByProvider.get(provider.id);
                const list = state?.models ?? null;
                const enabled = list?.filter((model) => model.enabled) ?? [];
                const providerDefault = enabled.find((model) => model.is_default);
                return (
                  <tr key={provider.id} className={TR_CLASS}>
                    <td className={TD_CLASS}>
                      <div className="text-xs font-medium">{provider.name}</div>
                      <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{provider.slug}</div>
                    </td>
                    <td className={TD_CLASS}>
                      {/* 清单没到手时不下「无可用模型」的结论：那可能只是没查到 */}
                      <ProviderStatus provider={provider} enabledModels={enabled.length} loaded={list != null} failed={state?.error != null} />
                    </td>
                    <td className={`${TD_CLASS} text-xs tabular-nums`}>
                      {list == null ? (
                        <span className={state?.error != null ? 'text-destructive' : 'text-muted-foreground'}>
                          {state?.error != null ? '拉取失败' : '统计中…'}
                        </span>
                      ) : (
                        <>
                          {provider.enabled ? enabled.length : <span className="text-muted-foreground">—</span>}
                          <span className="text-muted-foreground"> / {list.length}</span>
                        </>
                      )}
                    </td>
                    <td className={`${TD_CLASS} text-xs`}>
                      {providerDefault ? (
                        providerDefault.display_name
                      ) : (
                        <span className="text-muted-foreground">{list == null ? '—' : '未设置'}</span>
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

function ProviderStatus({
  provider,
  enabledModels,
  loaded,
  failed,
}: {
  provider: AdminProvider;
  enabledModels: number;
  /** 模型清单是否已到手；没到手就不下「无可用模型」的结论。 */
  loaded: boolean;
  failed: boolean;
}) {
  if (!provider.enabled) return <Badge tone="muted">已停用</Badge>;
  if (failed) return <Badge tone="muted">清单拉取失败</Badge>;
  if (!loaded) return <Badge tone="muted">检查中</Badge>;
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
