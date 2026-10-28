/**
 * 「我的电脑」——隔离执行环境的状态与产物面板。
 *
 * ## 它解决什么
 *
 * 沙箱是**每用户一台、跨会话共用**的机器（后端 `lease.py`），闲置会被自动回收。此前
 * 前端只有心跳在跑、状态查询函数全仓零调用：用户完全不知道自己的机器还在不在、跑出来的
 * 文件在哪。这个指示器是那两条已写好的请求（`fetchSandboxStatus` /
 * `fetchSandboxArtifacts`）唯一的界面消费者。
 *
 * ## 为什么叫「我的电脑」
 *
 * 机器属于**用户**，不属于某个会话：关掉会话机器还在，开个新会话也还是同一台。写成
 * 「本会话的电脑」会让用户误以为关掉会话就会丢机器（或反之）。这里 `sessionId` 只用于
 * 「在哪个会话里发请求」，不参与任何文案。
 *
 * ## 为什么后端没开沙箱就不渲染
 *
 * 端点会 404，指示器会永远显示「我的电脑 · 未知」。对着一个不存在的功能给用户留一个
 * 永远灰着的图标，只会让人以为系统坏了。整块由 `sandboxAvailable` 把门（`/api/capabilities`
 * 的 `sandbox` 字段）——这个字段此前也只是被起步建议消费。
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { ChevronDown, Download, HardDrive, Loader2, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverDescription, PopoverTitle, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { formatSandboxArtifactSize, type SandboxArtifact } from '../../lib/sandbox';
import {
  SANDBOX_LABEL,
  artifactExpired,
  artifactsExpired,
  describeArtifactCount,
  describeArtifactExpiry,
  describeSandboxStatus,
  type SandboxStatusView,
  type SandboxTone,
} from './view';

/** 状态点配色：与同步状态按钮同一套语义（绿=好、灰=没有、黄=要注意、红=坏了）。 */
const TONE_DOT: Record<SandboxTone, string> = {
  muted: 'bg-muted-foreground/40',
  ok: 'bg-emerald-500',
  busy: 'bg-sky-500',
  warning: 'bg-amber-500',
  error: 'bg-destructive',
};

/** 触发器上的文字配色：只有异常态才染色，正常态保持中性（不要满屏绿字）。 */
const TONE_TEXT: Record<SandboxTone, string> = {
  muted: 'text-muted-foreground',
  ok: 'text-muted-foreground',
  busy: 'text-muted-foreground',
  warning: 'text-amber-600 dark:text-amber-500',
  error: 'text-destructive',
};

export interface SandboxStatusProps {
  /** 由 `useSandboxStatus` 提供。 */
  status: {
    state: string;
    expires_at: string | null;
    artifact_uri: string | null;
  } | null;
  artifacts: readonly SandboxArtifact[];
  failed: boolean;
  loading?: boolean;
  /** 手动刷新（产物链接会过期，刷新是唯一的恢复办法）。 */
  onRefresh?: () => void;
  /**
   * 开合的**受控覆盖**。
   *
   * 真实页面由 `SandboxStatusIndicator` 受控传入（它要据展开状态调轮询节拍）；
   * 不传时组件自己管（测试 / 单独使用时更省事）。
   */
  open?: boolean;
  onOpenChange?: (next: boolean) => void;
}

/**
 * 单个产物行。
 *
 * 用 `<a download>` 而不是 `window.open`：预签名 URL 指向的是对象存储，跳转过去浏览器
 * 会**离开当前页面**；`download` 让它在原地触发下载，对话与草稿都还在。`rel="noopener
 * noreferrer"` 是外链的标配——目标域不是本站，不该让它拿到 `window.opener`。
 */
function ArtifactRow({
  artifact,
  now,
}: {
  artifact: SandboxArtifact;
  now: number;
}) {
  const expired = artifactExpired(artifact.expires_at, now);

  return (
    <li className="flex items-center gap-2 rounded-md border border-border/60 bg-muted/20 px-2 py-1.5">
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-[11px] text-foreground" title={artifact.name}>
          {artifact.name}
        </div>
        <div className="text-[10px] text-muted-foreground">
          {formatSandboxArtifactSize(artifact.size_bytes)}
          {expired ? ' · 链接已失效' : ''}
        </div>
      </div>
      {/* 过期后不给可点的假链接：点了只会拿到 403 页面，比灰掉更让人困惑 */}
      <a
        href={artifact.download_url}
        download={artifact.name}
        target="_blank"
        rel="noopener noreferrer"
        aria-disabled={expired || undefined}
        aria-label={`下载 ${artifact.name}`}
        className={cn(
          'inline-flex h-6 shrink-0 items-center gap-1 rounded-md border border-border/70 px-1.5 text-[10.5px] font-medium transition-colors',
          expired
            ? 'pointer-events-none text-muted-foreground/50'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        )}
      >
        <Download className="size-3" aria-hidden="true" />
        下载
      </a>
    </li>
  );
}

/**
 * 面板内容。
 *
 * **单独导出**而不是内联在 `SandboxStatus` 里：它挂在 Base UI 的 Portal 上，而
 * `renderToStaticMarkup` 不渲染 portal——内联的话，这个仓库（node 环境、没有 jsdom）
 * 里就一条断言都写不了。拆出来之后测试可以直接渲染它。
 *
 * `title` / `description` 是**插槽**：真实页面传 `PopoverTitle` / `PopoverDescription`
 * （Base UI 靠它们给弹层挂 `aria-labelledby` / `aria-describedby`），但这两个原语必须
 * 在 `Popover.Root` 之内才能渲染——所以不能写死在这里，否则这个组件就没法被单独测。
 * 不传时退回语义等价的 `h3` / `p`。
 */
export function SandboxPanelContent({
  view,
  artifacts,
  now,
  loading = false,
  onRefresh,
  title,
  description,
}: {
  view: SandboxStatusView;
  artifacts: readonly SandboxArtifact[];
  now: number;
  loading?: boolean;
  onRefresh?: () => void;
  title?: ReactNode;
  description?: ReactNode;
}) {
  const artifactCount = describeArtifactCount(artifacts);
  const allExpired = artifactsExpired(artifacts, now);

  return (
    <div>
      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            'mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full',
            view.tone === 'error'
              ? 'bg-destructive/10 text-destructive'
              : 'bg-muted text-muted-foreground'
          )}
        >
          <HardDrive size={15} aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          {title ?? (
            <h3 className="text-[13px] leading-5 font-medium">{view.title}</h3>
          )}
          {description ?? (
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              {view.description}
            </p>
          )}
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between border-t border-border/60 pt-2.5">
        <span className="text-[11px] text-muted-foreground">可下载的文件</span>
        <span className="text-[11px] tabular-nums text-foreground/90">{artifactCount}</span>
      </div>

      {artifacts.length > 0 ? (
        <ul className="mt-1.5 space-y-1.5">
          {artifacts.map((artifact) => (
            <ArtifactRow key={`${artifact.name}-${artifact.download_url}`} artifact={artifact} now={now} />
          ))}
        </ul>
      ) : (
        <p className="mt-1.5 text-[10.5px] leading-relaxed text-muted-foreground/80">
          还没有产出文件。机器在任务里生成的文件会出现在这里。
        </p>
      )}

      {artifacts.length > 0 && (
        <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground/70">
          {allExpired
            ? '下载链接都过期了，点「刷新」重新获取。'
            : describeArtifactExpiry(artifacts[0].expires_at, now)}
        </p>
      )}

      <div className="mt-3 flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={onRefresh}
          disabled={!onRefresh}
          className="text-[11px]"
        >
          {loading ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : (
            <RefreshCw className="size-3" aria-hidden="true" />
          )}
          刷新
        </Button>
      </div>

      <p className="mt-2.5 text-[10px] leading-relaxed text-muted-foreground/70">
        这台机器在你所有对话之间共用；闲置一段时间后会被自动回收（文件会先导出到这里）。
      </p>
    </div>
  );
}

/**
 * 指示器本体。
 *
 * 纯粹受控：状态由 `useSandboxStatus` 拉、这里只负责显示与「展开产物」。抽成受控组件
 * 是为了能在 node 环境里用 `renderToStaticMarkup` 断言结构（仓库没有 jsdom）。
 */
export function SandboxStatus({
  status,
  artifacts,
  failed,
  loading = false,
  onRefresh,
  open: controlledOpen,
  onOpenChange: controlledOnOpenChange,
}: SandboxStatusProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  // 打开面板时记下时刻：过期判定与「还有多久失效」都相对于这个时刻，
  // 而不是渲染时不断取 `Date.now()`（那会让每次重渲染的结论都不一样）。
  const [openedAt, setOpenedAt] = useState(() => Date.now());
  // 面板开着时让「N 分钟后失效」自己走起来
  const [now, setNow] = useState(openedAt);

  useEffect(() => {
    if (!open) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, [open]);

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (controlledOnOpenChange) controlledOnOpenChange(next);
      else setUncontrolledOpen(next);
      // 打开时校准时刻：过期判定与「还有多久失效」都以这一刻为基准
      if (next) {
        const at = Date.now();
        setOpenedAt(at);
        setNow(at);
      }
    },
    [controlledOnOpenChange]
  );

  // 首次探测还在路上：显示中性态，而不是先闪一下「还没有启动」。
  // 后者在机器明明已就绪时会变成一次假的「机器没了」。
  const view: SandboxStatusView =
    loading && !status && artifacts.length === 0
      ? {
          tone: 'muted',
          label: SANDBOX_LABEL,
          stateLabel: '正在查询',
          title: `${SANDBOX_LABEL} · 正在查询`,
          description: '正在查询我的电脑的状态',
          pulsing: true,
        }
      : describeSandboxStatus(status, failed);

  const triggerLabel = `${view.description}，查看我的电脑`;

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger
        aria-label={triggerLabel}
        title={view.description}
        className={cn(
          'inline-flex h-7 shrink-0 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-[11px] transition outline-none',
          'hover:bg-muted/60 focus-visible:ring-1 focus-visible:ring-ring/60',
          TONE_TEXT[view.tone]
        )}
      >
        <span
          aria-hidden="true"
          className={cn('size-1.5 shrink-0 rounded-full', TONE_DOT[view.tone], view.pulsing && 'animate-pulse')}
        />
        <HardDrive className="size-3.5" aria-hidden="true" />
        {/* 窄屏只留图标：顶栏空间要留给会话标题 */}
        <span className="hidden sm:inline">{view.stateLabel}</span>
        <ChevronDown className="size-3 opacity-60" aria-hidden="true" />
      </PopoverTrigger>

      <PopoverContent side="bottom" align="end" className="w-72">
        <SandboxPanelContent
          view={view}
          artifacts={artifacts}
          now={now}
          loading={loading}
          onRefresh={onRefresh}
          title={<PopoverTitle className="text-[13px] leading-5 font-medium">{view.title}</PopoverTitle>}
          description={
            <PopoverDescription className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              {view.description}
            </PopoverDescription>
          }
        />
      </PopoverContent>
    </Popover>
  );
}
