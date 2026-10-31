/**
 * 设置页面。
 *
 * ## 布局
 *
 * 采用桌面设置页的经典结构：左侧固定分区菜单 + 右侧大内容区。主题排成一行三张
 * 卡片，其余选项收成一行分段控件；每组按「左标题说明、右控件」排布。窄屏时菜单
 * 变成顶部横滑标签，控件自动落到说明下方。
 *
 * ## 与对话区的关系
 *
 * 页面挂在主界面里与对话区互斥显示，而对话区只是用 `hidden` 收起来、不卸载：
 * 卸载 `ChatProvider` 会掐断正在生成的回答，丢掉草稿与滚动位置。
 *
 * ## 焦点
 *
 * 打开时对话区整块 `display:none`，原来聚焦的入口（胶囊工具栏齿轮 / 侧边栏设置
 * 按钮）随之不可聚焦，焦点会掉回 `<body>`；所以进入时把焦点接到当前分区，
 * 关闭时再还给打开它的那个按钮。
 */

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import {
  Activity,
  AlignLeft,
  ChevronsUpDown,
  Eye,
  FoldVertical,
  KeyRound,
  Laptop,
  ListEnd,
  ListTree,
  Moon,
  Rows3,
  Sun,
  UnfoldVertical,
  User,
  X,
  type LucideIcon,
} from 'lucide-react';

import { Avatar, AvatarFallback } from './ui/avatar';
import { Button } from './ui/button';
import { PasswordChangeForm } from './PasswordChangeForm';
import { useTheme, type ThemePreference } from '../context/theme-context';
import { useDetailLevel } from '../context/detail-level-context';
import { usePerformanceUsageState } from '../context/performance-usage-context';
import { useMessageQueuePref } from '../context/message-queue-pref-context';
import { useIsMobileViewport } from '../hooks/use-media-query';
import { ROLE_LABELS } from '../lib/auth';
import {
  COLLAPSE_TIMING_OPTIONS,
  DETAIL_LEVEL_OPTIONS,
  type CollapseTiming,
  type DetailLevel,
} from '../lib/detail-level';
import { PERFORMANCE_USAGE_OPTIONS, type PerformanceUsage } from '../lib/performance-usage';
import {
  MESSAGE_QUEUE_PREF_OPTIONS,
  type MessageQueuePreference,
} from '../lib/message-queue-pref';
import {
  DEFAULT_SETTINGS_SECTION,
  SETTINGS_SECTIONS,
  type SettingsSectionId,
} from '../lib/settings-sections';
import { cn } from '../lib/utils';

const tabId = (id: SettingsSectionId) => `settings-tab-${id}`;
const panelId = (id: SettingsSectionId) => `settings-panel-${id}`;

/* ------------------------------------------------------------------ *
 * 偏好项
 * ------------------------------------------------------------------ */

/** 一行偏好：图标 + 名称 + 一句说明。 */
interface OptionCard<T extends string> {
  value: T;
  label: string;
  description: string;
  Icon: LucideIcon;
  /** 选中时的图标配色。主题三档各有语义色，其余跟随主色（primary）。 */
  activeIconClassName?: string;
}

/**
 * 单选偏好组。
 *
 * 两种形态，按内容性质分工：
 * - 主题用 `prominent` 卡片：三张并排、图标大、文字少，像选择桌面壁纸一样先看再选；
 * - 其余选项用紧凑分段控件：一行排开，嵌在设置行的右侧，不把每项都撑成大卡片。
 *
 * 语义都用 radiogroup/radio，不用一排 aria-pressed：读屏会读出「第 2 项，共 4 项」，
 * 方向键能在组内移动；而按钮组要点 Tab 四次才走得完，还得自己记住哪些按钮是一组。
 */
function PreferenceGroup<T extends string>({
  ariaLabel,
  options,
  value,
  onChange,
  prominent = false,
}: {
  ariaLabel: string;
  options: ReadonlyArray<OptionCard<T>>;
  value: T;
  onChange: (next: T) => void;
  /** 主题这类需要「预览感」的选项用更大的卡片。 */
  prominent?: boolean;
}) {
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const selectAt = (index: number) => {
    const option = options[index];
    if (!option) return;
    onChange(option.value);
    // 选中即跟随焦点：方向键换项后光标不能留在原处，否则下一次按键会从错的位置算
    optionRefs.current[index]?.focus({ preventScroll: true });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = options.length - 1;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      event.preventDefault();
      selectAt(index === last ? 0 : index + 1);
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      event.preventDefault();
      selectAt(index === 0 ? last : index - 1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      selectAt(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      selectAt(last);
    }
  };

  if (prominent) {
    return (
      <div role="radiogroup" aria-label={ariaLabel} className="grid grid-cols-3 gap-3">
        {options.map((option, index) => {
          const checked = option.value === value;
          return (
            <button
              key={option.value}
              ref={(node) => {
                optionRefs.current[index] = node;
              }}
              type="button"
              role="radio"
              aria-checked={checked}
              tabIndex={checked ? 0 : -1}
              title={option.description}
              onClick={() => onChange(option.value)}
              onKeyDown={(event) => handleKeyDown(event, index)}
              className={cn(
                'flex h-28 cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border transition select-none',
                'focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none',
                checked
                  ? 'border-foreground/20 bg-muted/60'
                  : 'border-border/70 bg-card hover:border-foreground/15 hover:bg-muted/30'
              )}
            >
              <option.Icon
                className={cn(
                  'size-6 shrink-0',
                  checked
                    ? (option.activeIconClassName ?? 'text-primary')
                    : 'text-muted-foreground'
                )}
                aria-hidden="true"
              />
              <span
                className={cn(
                  'text-base text-foreground',
                  checked ? 'font-medium' : 'font-normal'
                )}
              >
                {option.label}
              </span>
            </button>
          );
        })}
      </div>
    );
  }

  const columns =
    options.length >= 4
      ? 'grid-cols-4'
      : options.length === 3
        ? 'grid-cols-3'
        : 'grid-cols-2';

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        'grid w-full max-w-[24rem] gap-1 rounded-lg border border-border/60 bg-muted/60 p-1 xl:ml-auto',
        columns
      )}
    >
      {options.map((option, index) => {
        const checked = option.value === value;
        return (
          <button
            key={option.value}
            ref={(node) => {
              optionRefs.current[index] = node;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            // 组内只有一项能 Tab 进来，剩下靠方向键——与原生 radio 一致
            tabIndex={checked ? 0 : -1}
            title={option.description}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={cn(
              'flex h-8 min-w-0 cursor-pointer items-center justify-center gap-1.5 rounded-md px-1.5 text-sm transition select-none',
              'focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none',
              checked
                ? 'bg-card text-foreground ring-1 ring-border/50'
                : 'text-muted-foreground hover:bg-card/60 hover:text-foreground'
            )}
          >
            <option.Icon className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/** 区块小标题：`h3`（页面标题 `h1`、分区标题 `h2` 都在它上面，层级不跳）。 */
function SectionHeading({ Icon, children }: { Icon: LucideIcon; children: ReactNode }) {
  return (
    <h3 className="flex items-center gap-2 text-base font-medium text-foreground">
      <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
      <span>{children}</span>
    </h3>
  );
}

/**
 * 一行设置：左边是「是什么、改了什么」，右边是控件。
 *
 * 宽屏并排，窄屏自动落到上下两段——和成熟设置页的阅读顺序一致：先读标签，
 * 再操作旁边的控件，而不是让每个选项各占一整行。
 */
function SettingBlock({
  Icon,
  title,
  description,
  children,
}: {
  Icon: LucideIcon;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="grid gap-3 py-8 first:pt-0 last:pb-0 xl:grid-cols-[450px_minmax(0,1fr)] xl:items-start xl:gap-8">
      <div className="xl:pt-1">
        <SectionHeading Icon={Icon}>{title}</SectionHeading>
        <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{description}</p>
      </div>
      <div className="min-w-0 rounded-2xl bg-muted/40 p-4">{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------------ *
 * 各分区内容
 * ------------------------------------------------------------------ */

const THEME_CARDS: ReadonlyArray<OptionCard<ThemePreference>> = [
  {
    value: 'light',
    label: '浅色',
    description: '保持明亮清爽的界面',
    Icon: Sun,
    activeIconClassName: 'text-amber-500',
  },
  {
    value: 'dark',
    label: '深色',
    description: '降低暗光环境下的视觉疲劳',
    Icon: Moon,
    activeIconClassName: 'text-sky-400',
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
const DETAIL_LEVEL_ICONS: Record<DetailLevel, LucideIcon> = {
  compact: FoldVertical,
  standard: Rows3,
  detailed: ListTree,
  verbose: UnfoldVertical,
};

/** 「工作步骤收起时机」两档的图标。 */
const COLLAPSE_TIMING_ICONS: Record<CollapseTiming, LucideIcon> = {
  completion: AlignLeft,
  'next-input': ChevronsUpDown,
};

/**
 * 「性能与用量」两档的图标。
 *
 * 与「工作步骤展示」是两件事，图标也要能一眼分开：那一组换的是过程行的形态，
 * 这一组换的是统计读数的详细程度。
 */
const PERFORMANCE_USAGE_ICONS: Record<PerformanceUsage, LucideIcon> = {
  compact: AlignLeft,
  detailed: Activity,
};

/**
 * 「生成中发送」两档的图标。
 *
 * 与「性能与用量」同理，图标要能一眼分开：禁止档用一个列表截断的形状（生成期间
 * 不再接新消息），排队档用列表末尾追加的形状（新消息排在队尾等本轮结束）。
 */
const MESSAGE_QUEUE_PREF_ICONS: Record<MessageQueuePreference, LucideIcon> = {
  block: Rows3,
  insert: ListEnd,
};

const DETAIL_LEVEL_CARDS: ReadonlyArray<OptionCard<DetailLevel>> = DETAIL_LEVEL_OPTIONS.map(
  ({ value, label, description }) => ({ value, label, description, Icon: DETAIL_LEVEL_ICONS[value] })
);

const COLLAPSE_TIMING_CARDS: ReadonlyArray<OptionCard<CollapseTiming>> =
  COLLAPSE_TIMING_OPTIONS.map(({ value, label, description }) => ({
    value,
    label,
    description,
    Icon: COLLAPSE_TIMING_ICONS[value],
  }));

const PERFORMANCE_USAGE_CARDS: ReadonlyArray<OptionCard<PerformanceUsage>> =
  PERFORMANCE_USAGE_OPTIONS.map(({ value, label, description }) => ({
    value,
    label,
    description,
    Icon: PERFORMANCE_USAGE_ICONS[value],
  }));

const MESSAGE_QUEUE_PREF_CARDS: ReadonlyArray<OptionCard<MessageQueuePreference>> =
  MESSAGE_QUEUE_PREF_OPTIONS.map(({ value, label, description }) => ({
    value,
    label,
    description,
    Icon: MESSAGE_QUEUE_PREF_ICONS[value],
  }));

export interface AccountSectionProps {
  userEmail: string | null;
  /** 当前账号角色（`user` / `admin` / `superadmin`）。未知/未传时不显示徽标。 */
  userRole?: string | null;
  /** 密码修改成功后调用：服务端已失效所有会话，App 负责退回登录页。 */
  onPasswordChanged?: () => void;
}

/** 账号与安全：登录身份 + 修改密码。 */
export function AccountSection({
  userEmail,
  userRole = null,
  onPasswordChanged,
}: AccountSectionProps) {
  // 从邮箱派生昵称与头像首字母
  const nickname = userEmail ? userEmail.split('@')[0] : '未登录用户';
  const initial = userEmail ? nickname.charAt(0).toUpperCase() : '未';
  // 未知角色（后端新增了一档）不猜，宁可只显示邮箱
  const roleLabel = userRole ? (ROLE_LABELS[userRole] ?? null) : null;

  return (
    <div className="divide-y divide-border/60">
      <SettingBlock Icon={User} title="登录身份" description="当前登录的邮箱与账号角色">
        <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-muted/20 px-3 py-2.5">
          <Avatar size="default" className="border border-border/80">
            <AvatarFallback className="bg-primary/10 text-xs font-medium text-primary">
              {initial}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="truncate text-sm font-medium text-foreground">{nickname}</span>
              {roleLabel && (
                <span className="shrink-0 rounded-full bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary">
                  {roleLabel}
                </span>
              )}
            </div>
            <div
              className="truncate text-xs text-muted-foreground"
              title={userEmail ?? undefined}
            >
              {userEmail ?? '未登录'}
            </div>
          </div>
        </div>
      </SettingBlock>

      {/* 修改密码：成功会让所有设备下线，由 App 统一退回登录页 */}
      <SettingBlock Icon={KeyRound} title="修改密码" description="修改成功后会自动退出当前登录，需要重新登录。">
        <PasswordChangeForm onPasswordChanged={onPasswordChanged} />
      </SettingBlock>
    </div>
  );
}

/** 主题预览：把当前配色投到一段真实的对话片段上，改主题时这里同步变。 */
function ThemePreview() {
  return (
    <div className="overflow-hidden rounded-xl border border-border/70 bg-background">
      <div className="flex items-center justify-between border-b border-border/60 px-3 py-2">
        <span className="text-xs font-medium text-foreground">Bennett Agent</span>
        <span className="text-[11px] text-muted-foreground">对话预览</span>
      </div>
      <div className="space-y-3 p-3">
        <div className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-md bg-primary px-3 py-2 text-[11px] leading-relaxed text-primary-foreground">
          把这份文档总结成三条结论，并列出风险
        </div>
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <span className="size-1.5 rounded-full bg-primary" aria-hidden="true" />
            已读取文档 · 3 个步骤
          </div>
          <div className="space-y-1 text-[11px] leading-relaxed text-foreground">
            <p>1. 先确认目标与范围</p>
            <p>2. 再提取关键结论</p>
            <p>3. 最后列出下一步</p>
          </div>
          <div className="rounded-lg bg-muted/50 px-2.5 py-2 text-[11px] leading-relaxed text-muted-foreground">
            风险：第二点依赖旧数据，需要重新核对。
          </div>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border/60 bg-muted/30 px-2.5 py-1.5 text-[11px] text-muted-foreground">
          <span>继续提问…</span>
          <span aria-hidden="true">↵</span>
        </div>
      </div>
    </div>
  );
}

/** 外观与主题。 */
export function AppearanceSection() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  const activeTheme = THEME_CARDS.find((option) => option.value === theme) ?? THEME_CARDS[2];
  const themeDescription = `选择界面配色，也可以让界面跟着系统深浅色自动切换。当前：${activeTheme.label}${
    theme === 'system' ? `（实际显示${resolvedTheme === 'dark' ? '深色' : '浅色'}）` : ''
  }。`;

  return (
    <div className="divide-y divide-border/60">
      <SettingBlock Icon={Sun} title="主题" description={themeDescription}>
        <PreferenceGroup
          ariaLabel="外观主题"
          options={THEME_CARDS}
          value={theme}
          onChange={setTheme}
          prominent
        />
      </SettingBlock>

      <SettingBlock Icon={Eye} title="预览" description="切换主题后，对话窗口会立即显示成下面的效果。">
        <ThemePreview />
      </SettingBlock>
    </div>
  );
}

/**
 * 对话显示：工作步骤露多少 / 什么时候收 / 用量读数显示多少。
 *
 * 三块放在同一个分区，因为它们改的是同一件事的三个维度：过程怎么显示。
 * 分成三个菜单项会让人以为要配三遍。
 */
export function ConversationSection() {
  const { level, setLevel, collapseTiming, setCollapseTiming } = useDetailLevel();
  const performanceUsage = usePerformanceUsageState();
  const messageQueuePref = useMessageQueuePref();

  return (
    <div className="divide-y divide-border/60">
      <SettingBlock
        Icon={AlignLeft}
        title="工作步骤展示"
        description="四档都能点开看细节；这里只决定默认露多少。改动立即生效，不影响已有对话的内容。"
      >
        <PreferenceGroup
          ariaLabel="工作步骤展示"
          options={DETAIL_LEVEL_CARDS}
          value={level}
          onChange={setLevel}
        />
      </SettingBlock>

      {/* 与上面四档**正交**（所以是独立一组而不是第五档）。默认「回答结束后」，
          长回答里想回看刚跑完的过程才需要改成「下次有新消息时」。 */}
      <SettingBlock
        Icon={ChevronsUpDown}
        title="工作步骤收起时机"
        description="只影响刚结束的那一轮何时收起；选「下次有新消息时」可以在提问前回看它刚才做了什么。"
      >
        <PreferenceGroup
          ariaLabel="工作步骤收起时机"
          options={COLLAPSE_TIMING_CARDS}
          value={collapseTiming}
          onChange={setCollapseTiming}
        />
      </SettingBlock>

      {/* 性能与用量：只影响输入框下方那排统计读数显示多少，不改任何记账与落库 */}
      <SettingBlock
        Icon={Activity}
        title="性能与用量"
        description="只影响输入框下方读数显示多少；上下文占用环始终显示，记账与落库也不受影响。"
      >
        <PreferenceGroup
          ariaLabel="性能与用量"
          options={PERFORMANCE_USAGE_CARDS}
          value={performanceUsage.mode}
          onChange={performanceUsage.setMode}
        />
      </SettingBlock>

      {/* 生成中发送：决定回复生成期间输入框的行为——排队还是锁定。
          与「工作步骤展示」等三组正交：那三组改「怎么显示」，这一组改「发送怎么办」。 */}
      <SettingBlock
        Icon={ListEnd}
        title="生成中发送"
        description="回复正在生成时再发消息怎么办：插入排队会在输入框上方列出待发消息，可改可删，也能让它跳过等待立即发送。"
      >
        <PreferenceGroup
          ariaLabel="生成中发送"
          options={MESSAGE_QUEUE_PREF_CARDS}
          value={messageQueuePref.mode}
          onChange={messageQueuePref.setMode}
        />
      </SettingBlock>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 页面
 * ------------------------------------------------------------------ */

export interface SettingsPageProps {
  userEmail: string | null;
  userRole?: string | null;
  /** 返回主界面（关闭设置）。 */
  onClose: () => void;
  onPasswordChanged?: () => void;
  /** 首屏打开哪个分区。默认「账号与安全」。 */
  defaultSection?: SettingsSectionId;
}

/**
 * 设置页：左侧分区菜单、右侧内容区。
 *
 * 所有分区一次性挂载、非当前分区用 `hidden` 收起：切走再切回来时，密码框里
 * 已经敲的内容还在。
 */
export function SettingsPage({
  userEmail,
  userRole = null,
  onClose,
  onPasswordChanged,
  defaultSection = DEFAULT_SETTINGS_SECTION,
}: SettingsPageProps) {
  const [active, setActive] = useState<SettingsSectionId>(defaultSection);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // 窄屏时菜单是横排的：读屏的「上/下一项」手势要跟着方向变（键位两轴都接了）
  const isMobile = useIsMobileViewport();

  const activeIndex = SETTINGS_SECTIONS.findIndex((section) => section.id === active);
  // 分区表与 id 是同一份常量：找不到就是代码写错了，退回第一项而不是渲染空白页
  const activeSection = SETTINGS_SECTIONS[activeIndex] ?? SETTINGS_SECTIONS[0];
  /** 首屏分区（挂载时那一份）：焦点要落在它上面，而不是永远落在第一项。 */
  const initialSectionRef = useRef(active);

  useEffect(() => {
    const opener = document.activeElement;
    const index = SETTINGS_SECTIONS.findIndex((section) => section.id === initialSectionRef.current);
    tabRefs.current[index]?.focus({ preventScroll: true });
    return () => {
      // 关闭后把焦点还给打开它的入口（胶囊工具栏齿轮 / 侧边栏设置按钮）。
      // 延后一拍：cleanup 跑在提交之前，此刻对话区还是 `hidden`，聚焦不会生效。
      setTimeout(() => {
        if (opener instanceof HTMLElement && opener.isConnected) {
          opener.focus({ preventScroll: true });
        }
      }, 0);
    };
  }, []);

  /**
   * Esc 关闭。
   *
   * 页头已经有一个 × ，Esc 只是给键盘用户少走一段路（和弹层的习惯一致）。
   * 页内可能弹出确认框/菜单：那些弹层自己处理 Esc，这里不能抢。
   */
  useEffect(() => {
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const selectAt = useCallback((index: number) => {
    const next = SETTINGS_SECTIONS[index];
    if (!next) return;
    setActive(next.id);
    tabRefs.current[index]?.focus({ preventScroll: true });
  }, []);

  const handleTabKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
      // 竖向菜单的键位约定：↑↓ 换项，Home/End 跳首尾。左右键同样接上——
      // 窄屏（md 以下）菜单是横排的，那时用户按的是左右。
      const last = SETTINGS_SECTIONS.length - 1;
      if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        event.preventDefault();
        selectAt(index === last ? 0 : index + 1);
      } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
        event.preventDefault();
        selectAt(index === 0 ? last : index - 1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        selectAt(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        selectAt(last);
      }
    },
    [selectAt]
  );

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden bg-muted/30 text-foreground md:flex-row">
      {/* 关闭按钮固定在页面右上角：宽屏与侧边栏标题同一水平线，窄屏也不挤占标题 */}
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onClose}
        aria-label="关闭设置"
        className="absolute top-3 right-4 z-10 cursor-pointer text-muted-foreground hover:text-foreground md:top-4 md:right-6"
      >
        <X className="size-4" aria-hidden="true" />
      </Button>

      {/* 左：分区菜单。宽屏固定一列，窄屏横排成一条可横滑的标签行 */}
      <aside className="flex shrink-0 flex-col bg-background md:w-[14.5rem] md:border-r md:border-border/60">
        <div className="flex h-14 shrink-0 items-center px-4 md:h-16 md:px-7">
          <h1 className="text-2xl font-semibold tracking-tight">设置</h1>
        </div>
        <nav
          aria-label="设置分区"
          className="shrink-0 border-b border-border/60 px-2 pb-2 md:min-h-0 md:flex-1 md:overflow-y-auto md:border-b-0 md:px-3 md:py-4"
        >
          <div
            role="tablist"
            aria-label="设置分区"
            aria-orientation={isMobile ? 'horizontal' : 'vertical'}
            className="flex min-w-0 gap-1 overflow-x-auto md:min-h-0 md:flex-col md:overflow-visible"
          >
            {SETTINGS_SECTIONS.map((section, index) => {
              const selected = section.id === active;
              return (
                <button
                  key={section.id}
                  ref={(node) => {
                    tabRefs.current[index] = node;
                  }}
                  type="button"
                  role="tab"
                  id={tabId(section.id)}
                  aria-selected={selected}
                  aria-controls={panelId(section.id)}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setActive(section.id)}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                  className={cn(
                    'flex shrink-0 cursor-pointer items-center gap-3 rounded-lg px-3 py-3 text-left text-sm whitespace-nowrap transition focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none md:w-full',
                    selected
                      ? 'bg-muted font-medium text-foreground'
                      : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground'
                  )}
                >
                  <section.Icon
                    className={cn(
                      'size-4 shrink-0',
                      selected ? 'text-foreground' : 'text-muted-foreground'
                    )}
                    aria-hidden="true"
                  />
                  <span>{section.label}</span>
                </button>
              );
            })}
          </div>
        </nav>
      </aside>

      {/* 右：当前分区的展示与操作，内容区独立滚动 */}
      <section className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-[72rem] flex-col px-5 pt-6 pb-8 md:min-h-full md:px-8 md:pt-9">
            <header className="mb-8">
              <h2 className="text-xl font-semibold tracking-tight">{activeSection.label}</h2>
            </header>

            <div>
              {SETTINGS_SECTIONS.map((section) => (
                <div
                  key={section.id}
                  role="tabpanel"
                  id={panelId(section.id)}
                  aria-labelledby={tabId(section.id)}
                  hidden={section.id !== active}
                >
                  {section.id === 'account' && (
                    <AccountSection
                      userEmail={userEmail}
                      userRole={userRole}
                      onPasswordChanged={onPasswordChanged}
                    />
                  )}
                  {section.id === 'appearance' && <AppearanceSection />}
                  {section.id === 'conversation' && <ConversationSection />}
                </div>
              ))}
            </div>

            <p className="mt-10 text-xs text-muted-foreground md:mt-auto md:pt-10">偏好设置会自动保存，无需手动提交。</p>
          </div>
        </div>
      </section>
    </div>
  );
}
