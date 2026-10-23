import {
  AlignLeft,
  Activity,
  Check,
  ChevronsUpDown,
  FoldVertical,
  HardDrive,
  Info,
  KeyRound,
  Laptop,
  ListTree,
  Moon,
  Rows3,
  Sun,
  UnfoldVertical,
  User,
} from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import { Button } from './ui/button';
import { Avatar, AvatarFallback } from './ui/avatar';
import { useTheme, type ThemePreference } from '../context/theme-context';
import { useDetailLevel } from '../context/detail-level-context';
import { usePerformanceUsageState } from '../context/performance-usage-context';
import {
  PERFORMANCE_USAGE_OPTIONS,
  type PerformanceUsage,
} from '../lib/performance-usage';
import { useLocalDataStats } from '../hooks/use-local-stats';
import { KnowledgePanel } from './KnowledgePanel';
import { LocalDataStatsSection } from './LocalDataStatsSection';
import { PasswordChangeForm } from './PasswordChangeForm';
import { ROLE_LABELS } from '../lib/admin';
import { APP_VERSION } from '../lib/app-info';
import {
  COLLAPSE_TIMING_OPTIONS,
  DETAIL_LEVEL_OPTIONS,
  type CollapseTiming,
  type DetailLevel,
} from '../lib/detail-level';
import {
  describeMcpCapability,
  describeMcpServers,
  describeToolCount,
  describeWebSearchCapability,
} from '../lib/capabilities';
import type { Capabilities } from '../types';

export interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userEmail: string | null;
  /** 当前账号角色（`user` / `admin` / `superadmin`）。未知/未传时不显示徽标。 */
  userRole?: string | null;
  /** 密码修改成功后调用：服务端已失效所有会话，App 负责退回登录页。 */
  onPasswordChanged?: () => void;
  /** 运行时能力快照；null = 还没拿到 / 拿不到（面板据此显示「未知」）。 */
  capabilities?: Capabilities | null;
  /** 上一次能力探测是否失败：失败且没有快照时给出「重新探测」入口。 */
  capabilitiesFailed?: boolean;
  /** 重新探测运行时能力（管理员配好搜索 Key 后不必刷新页面）。 */
  onRefreshCapabilities?: () => void;
}

export interface SettingsPanelProps {
  userEmail: string | null;
  userRole?: string | null;
  onPasswordChanged?: () => void;
  /** 运行时能力快照；null = 还没拿到 / 拿不到（面板据此显示「未知」）。 */
  capabilities?: Capabilities | null;
  /** 上一次能力探测是否失败。 */
  capabilitiesFailed?: boolean;
  /** 重新探测运行时能力。 */
  onRefreshCapabilities?: () => void;
  /**
   * 面板是否可见。默认 true（直接渲染 `SettingsPanel` 的场景）。
   * 本机数据只在打开时读一次，不是实时监控。
   */
  open?: boolean;
}

const THEME_OPTIONS: Array<{
  value: ThemePreference;
  label: string;
  description: string;
  Icon: typeof Sun;
}> = [
  {
    value: 'light',
    label: '浅色',
    description: '保持明亮清爽的界面',
    Icon: Sun,
  },
  {
    value: 'dark',
    label: '深色',
    description: '降低暗光环境下的视觉疲劳',
    Icon: Moon,
  },
  {
    value: 'system',
    label: '跟随系统',
    description: '自动同步操作系统主题',
    Icon: Laptop,
  },
];

/**
 * 「工作步骤展示」四档的图标。
 *
 * 图标的选择表达**信息密度递增**：收紧的箭头（简洁）→ 列表（标准）→ 展开的列表
 * （详细）→ 全展开箭头（完全展开）。用文字无法一眼看出四档的差别，图标可以。
 * 文案与顺序来自 `lib/detail-level`（纯数据，可单测）。
 */
const DETAIL_LEVEL_ICONS: Record<DetailLevel, typeof Sun> = {
  compact: FoldVertical,
  standard: Rows3,
  detailed: ListTree,
  verbose: UnfoldVertical,
};

/** 「工作步骤收起时机」两档的图标。 */
const COLLAPSE_TIMING_ICONS: Record<CollapseTiming, typeof Sun> = {
  completion: AlignLeft,
  'next-input': ChevronsUpDown,
};

/**
 * 「性能与用量」两档的图标。
 *
 * 与「工作步骤展示」是两件事，图标也要能一眼分开：那一组换的是过程行的形态，
 * 这一组换的是统计读数的详细程度。
 */
const PERFORMANCE_USAGE_ICONS: Record<PerformanceUsage, typeof Sun> = {
  compact: AlignLeft,
  detailed: Activity,
};

/**
 * 设置面板内容组件：支持独立复用与服务端渲染测试。
 */
export function SettingsPanel({
  userEmail,
  userRole = null,
  open = true,
  onPasswordChanged,
  capabilities = null,
  capabilitiesFailed = false,
  onRefreshCapabilities,
}: SettingsPanelProps) {
  const { theme, setTheme } = useTheme();
  const {
    level: detailLevel,
    setLevel: setDetailLevel,
    collapseTiming,
    setCollapseTiming,
  } = useDetailLevel();
  const performanceUsage = usePerformanceUsageState();
  const localStats = useLocalDataStats(open);

  // 从邮箱派生昵称与头像首字母
  const nickname = userEmail ? userEmail.split('@')[0] : '未登录用户';
  const initial = userEmail ? nickname.charAt(0).toUpperCase() : '未';
  // 未知角色（后端新增了一档）不猜，宁可只显示邮箱
  const roleLabel = userRole ? (ROLE_LABELS[userRole] ?? null) : null;

  return (
    <div className="space-y-5 py-1">
      {/* 账号信息区块 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <User className="size-3.5" />
          <span>当前账号</span>
        </h3>
        <div className="flex items-center gap-3 rounded-lg border border-border/70 bg-muted/40 p-3">
          <Avatar size="default" className="border border-border/80">
            <AvatarFallback className="bg-primary/10 text-primary font-medium text-xs">
              {initial}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-medium text-foreground truncate">{nickname}</span>
              {roleLabel && (
                <span className="shrink-0 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                  {roleLabel}
                </span>
              )}
            </div>
            <div className="text-[11px] text-muted-foreground truncate" title={userEmail ?? undefined}>
              {userEmail ?? '未登录'}
            </div>
          </div>
        </div>
      </div>

      {/* 外观设置区块 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <Sun className="size-3.5" />
          <span>外观与主题</span>
        </h3>
        <div role="group" aria-label="外观主题" className="grid grid-cols-3 gap-2">
          {THEME_OPTIONS.map(({ value, label, description, Icon }) => {
            const isActive = theme === value;
            return (
              <button
                key={value}
                type="button"
                onClick={() => setTheme(value)}
                aria-pressed={isActive}
                className={`relative flex flex-col items-center justify-center p-3 rounded-xl border text-center transition cursor-pointer select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                  isActive
                    ? 'border-primary bg-primary/5 text-foreground shadow-xs ring-1 ring-primary/40'
                    : 'border-border/70 hover:border-border hover:bg-muted/50 text-muted-foreground'
                }`}
              >
                {isActive && (
                  <span className="absolute top-2 right-2 text-primary">
                    <Check className="size-3.5" aria-hidden="true" />
                  </span>
                )}
                <Icon
                  className={`size-5 mb-1.5 ${
                    isActive
                      ? value === 'light'
                        ? 'text-amber-500'
                        : value === 'dark'
                          ? 'text-sky-400'
                          : 'text-foreground'
                      : 'text-muted-foreground'
                  }`}
                />
                <span className="text-xs font-medium text-foreground">{label}</span>
                <span className="text-[10px] text-muted-foreground mt-0.5 leading-tight line-clamp-1">
                  {description}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* 工作步骤展示 + 收起时机：思考与工具调用露多少细节。放在外观之后，因为它同样是
          「界面长什么样」的偏好，但改的是对话区，不是配色。
          两块共用「工作步骤」这个说法：它们改的是同一件事的两个维度
          （露多少 / 什么时候收），分成两个不相干的标题会让人以为要配两遍。 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <AlignLeft className="size-3.5" />
          <span>工作步骤展示</span>
        </h3>
        <div role="group" aria-label="工作步骤展示" className="grid grid-cols-2 gap-2">
          {DETAIL_LEVEL_OPTIONS.map(({ value, label, description }) => {
            const Icon = DETAIL_LEVEL_ICONS[value];
            const isActive = detailLevel === value;
            return (
              <button
                key={value}
                type="button"
                onClick={() => setDetailLevel(value)}
                aria-pressed={isActive}
                className={`relative flex flex-col items-center justify-center p-3 rounded-xl border text-center transition cursor-pointer select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                  isActive
                    ? 'border-primary bg-primary/5 text-foreground shadow-xs ring-1 ring-primary/40'
                    : 'border-border/70 hover:border-border hover:bg-muted/50 text-muted-foreground'
                }`}
              >
                {isActive && (
                  <span className="absolute top-2 right-2 text-primary">
                    <Check className="size-3.5" aria-hidden="true" />
                  </span>
                )}
                <Icon
                  className={`size-5 mb-1.5 ${isActive ? 'text-foreground' : 'text-muted-foreground'}`}
                  aria-hidden="true"
                />
                <span className="text-xs font-medium text-foreground">{label}</span>
                <span className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  {description}
                </span>
              </button>
            );
          })}
        </div>
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          四档都能点开看细节；这里只决定默认露多少。改动立即生效，不影响已有对话的内容。
        </p>
      </div>

      {/* 收起时机：与上面四档**正交**（所以是独立一块而不是第五档）。默认「回答结束后」，
          长回答里想回看刚跑完的过程才需要改成「下次有新消息时」。 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <ChevronsUpDown className="size-3.5" />
          <span>工作步骤收起时机</span>
        </h3>
        <div role="group" aria-label="工作步骤收起时机" className="grid grid-cols-2 gap-2">
          {COLLAPSE_TIMING_OPTIONS.map(({ value, label, description }) => {
            const Icon = COLLAPSE_TIMING_ICONS[value];
            const isActive = collapseTiming === value;
            return (
              <button
                key={value}
                type="button"
                onClick={() => setCollapseTiming(value)}
                aria-pressed={isActive}
                className={`relative flex flex-col items-center justify-center p-3 rounded-xl border text-center transition cursor-pointer select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                  isActive
                    ? 'border-primary bg-primary/5 text-foreground shadow-xs ring-1 ring-primary/40'
                    : 'border-border/70 hover:border-border hover:bg-muted/50 text-muted-foreground'
                }`}
              >
                {isActive && (
                  <span className="absolute top-2 right-2 text-primary">
                    <Check className="size-3.5" aria-hidden="true" />
                  </span>
                )}
                <Icon
                  className={`size-5 mb-1.5 ${isActive ? 'text-foreground' : 'text-muted-foreground'}`}
                  aria-hidden="true"
                />
                <span className="text-xs font-medium text-foreground">{label}</span>
                <span className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  {description}
                </span>
              </button>
            );
          })}
        </div>
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          只影响刚结束的那一轮何时收起；选「下次有新消息时」可以在提问前回看它刚才做了什么。
        </p>
      </div>

      {/* 性能与用量：只影响输入框下方那排统计读数显示多少，不改任何记账与落库。
          放在「工作步骤」两组之后：先决定过程怎么显示，再决定读数显示多少。 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <Activity className="size-3.5" />
          <span>性能与用量</span>
        </h3>
        <div role="group" aria-label="性能与用量" className="grid grid-cols-2 gap-2">
          {PERFORMANCE_USAGE_OPTIONS.map(({ value, label, description }) => {
            const Icon = PERFORMANCE_USAGE_ICONS[value];
            const isActive = performanceUsage.mode === value;
            return (
              <button
                key={value}
                type="button"
                onClick={() => performanceUsage.setMode(value)}
                aria-pressed={isActive}
                className={`relative flex flex-col items-center justify-center p-3 rounded-xl border text-center transition cursor-pointer select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                  isActive
                    ? 'border-primary bg-primary/5 text-foreground shadow-xs ring-1 ring-primary/40'
                    : 'border-border/70 hover:border-border hover:bg-muted/50 text-muted-foreground'
                }`}
              >
                {isActive && (
                  <span className="absolute top-2 right-2 text-primary">
                    <Check className="size-3.5" aria-hidden="true" />
                  </span>
                )}
                <Icon
                  className={`size-5 mb-1.5 ${isActive ? 'text-foreground' : 'text-muted-foreground'}`}
                  aria-hidden="true"
                />
                <span className="text-xs font-medium text-foreground">{label}</span>
                <span className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                  {description}
                </span>
              </button>
            );
          })}
        </div>
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          只影响输入框下方读数显示多少；上下文占用环始终显示，记账与落库也不受影响。
        </p>
      </div>

      {/* 修改密码：成功会让所有设备下线，由 App 统一退回登录页 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <KeyRound className="size-3.5" />
          <span>修改密码</span>
        </h3>
        <PasswordChangeForm onPasswordChanged={onPasswordChanged} />
      </div>

      {/* 知识库区块：上传的文档存服务端，与「本机数据」是两回事，所以分开两块 */}
      <KnowledgePanel open={open} />

      {/* 本机数据区块：数据默认只存浏览器，用户应该能知道自己存了多少 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <HardDrive className="size-3.5" />
          <span>本机数据</span>
        </h3>
        <LocalDataStatsSection
          stats={localStats.stats}
          failed={localStats.failed}
          onRetry={localStats.retry}
        />
      </div>

      {/* 关于 Bennett Agent 区块 */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <Info className="size-3.5" />
          <span>关于系统</span>
        </h3>
        <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-1.5 text-xs">
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">应用版本</span>
            {/* 版本来自 package.json（构建时注入），不再手写 */}
            <span className="font-mono text-[11px] text-foreground">v{APP_VERSION}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">推理内核</span>
            <span className="text-foreground">PydanticAI + assistant-ui</span>
          </div>
          {/* 服务端能力：空对话页的示例是裁剪过的，这里说明裁剪依据，
              否则「没有搜索示例」会被当成功能缺失，而不是服务端没配 */}
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">联网搜索</span>
            <span className="text-foreground">{describeWebSearchCapability(capabilities)}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">已装配工具</span>
            <span className="text-foreground">{describeToolCount(capabilities)}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">MCP 外部工具</span>
            <span className="text-foreground" title={describeMcpServers(capabilities) || undefined}>
              {describeMcpCapability(capabilities)}
            </span>
          </div>
        </div>
        {/* 「配了但没通」必须能看出原因：否则工具从列表里消失会被当成功能缺失，
            而运维也不知道该去查哪个 server。 */}
        {capabilities?.mcp_error && (
          <p className="text-[10.5px] leading-relaxed text-amber-600 dark:text-amber-500">
            部分 MCP server 未能连接：{capabilities.mcp_error}
          </p>
        )}
        {/* 能力清单只拉一次：启动瞬间失败（cookie 刚生效、反代在建连）会让这里永远是
            「未知」，而用户唯一能做的就是刷新整个页面。给一个就地重探的入口。 */}
        {capabilities === null && onRefreshCapabilities && (
          <div className="flex flex-wrap items-center gap-1.5 text-[10.5px] text-muted-foreground/80">
            <span>
              {capabilitiesFailed
                ? '能力探测失败，下面的「未知」不代表服务端没配。'
                : '还没有拿到能力清单。'}
            </span>
            <button
              type="button"
              onClick={onRefreshCapabilities}
              className="cursor-pointer rounded-md border border-border/70 px-1.5 py-0.5 font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            >
              重新探测
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function SettingsDialog({
  open,
  onOpenChange,
  userEmail,
  userRole = null,
  capabilities = null,
  capabilitiesFailed = false,
  onRefreshCapabilities,
  onPasswordChanged,
}: SettingsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        内容比一屏高：此前整块 `overflow-y-auto`，滚动时标题和「完成」一起滚走——
        在小窗口里想关掉它得先滚到底。改成「头尾固定、只有中间滚动」，
        `min-h-0` 是让 flex 子项真的能被压缩并出现滚动条（默认 min-height:auto 会撑开）。
      */}
      <DialogContent className="sm:max-w-md max-h-[85vh] flex flex-col gap-3 overflow-hidden">
        <DialogHeader>
          <DialogTitle>设置</DialogTitle>
          <DialogDescription>账号、外观、本机数据与系统信息</DialogDescription>
        </DialogHeader>

        <div className="-mr-1 min-h-0 flex-1 overflow-y-auto pr-1">
          <SettingsPanel
            userEmail={userEmail}
            userRole={userRole}
            open={open}
            capabilities={capabilities}
            capabilitiesFailed={capabilitiesFailed}
            onRefreshCapabilities={onRefreshCapabilities}
            onPasswordChanged={onPasswordChanged}
          />
        </div>

        <DialogFooter>
          <Button variant="default" size="sm" onClick={() => onOpenChange(false)}>
            完成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
