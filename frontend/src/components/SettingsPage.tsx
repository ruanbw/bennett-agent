/**
 * 设置页面。
 *
 * ## 为什么是「居中的一小块」，而不是「左菜单 + 右大页」
 *
 * 设置里真正能调的东西只有三组（账号、主题、对话显示），把菜单钉在屏幕最左侧、
 * 右边铺满整页，结果就是一条窄内容浮在一大片空白里——像没写完的页面。所以这里
 * 把整块设置收成一个居中的 672px 卡片：页头（返回 + 标题）+ 左侧分区 + 右侧内容，
 * 宽屏居中留白，窄屏自然下落成一列。
 *
 * 知识库、本机数据、关于系统不在设置里：它们是**信息展示**，不是设置项；进来只会
 * 把这一页撑成大表。
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
  Check,
  ChevronsUpDown,
  FoldVertical,
  KeyRound,
  Laptop,
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
 * ## 为什么是行列表，不是一排大卡片
 *
 * 卡片要么把「图标 + 名称 + 一句说明」撑在 200px 见方的盒子里（屏上大片空白），
 * 要么在 390px 的窄屏上被挤成三栏、说明文折成三行还高低不齐。行列表把同样的信息
 * 装进一条容器：每行自带「图标 / 名称＋说明 / 勾选」的横排，宽屏窄屏一套布局就够。
 *
 * ## 语义用 radiogroup/radio，不用一排 aria-pressed
 *
 * 这确实是「多选一」：读屏会读出「第 2 项，共 4 项」，方向键能在组内移动；
 * 而按钮组要点 Tab 四次才走得完，还得自己记住哪些按钮是一组。
 */
function PreferenceGroup<T extends string>({
  ariaLabel,
  options,
  value,
  onChange,
}: {
  ariaLabel: string;
  options: ReadonlyArray<OptionCard<T>>;
  value: T;
  onChange: (next: T) => void;
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

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="overflow-hidden rounded-xl border border-border/70 bg-muted/20"
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
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={cn(
              'flex w-full cursor-pointer items-center gap-2.5 px-3 py-2.5 text-left transition select-none',
              'focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring/60 focus-visible:outline-none',
              index > 0 && 'border-t border-border/60',
              checked ? 'bg-primary/5 hover:bg-primary/10' : 'hover:bg-muted/60'
            )}
          >
            <option.Icon
              className={cn(
                'size-4 shrink-0',
                checked ? (option.activeIconClassName ?? 'text-primary') : 'text-muted-foreground'
              )}
              aria-hidden="true"
            />
            <span className="min-w-0 flex-1">
              <span
                className={cn(
                  'block text-xs text-foreground',
                  checked ? 'font-medium' : 'font-normal'
                )}
              >
                {option.label}
              </span>
              <span className="mt-0.5 block text-[10.5px] leading-relaxed text-muted-foreground">
                {option.description}
              </span>
            </span>
            {/* 未选中时也占位：否则每行右侧宽窄不一，勾会左右跳 */}
            <span className="flex size-3.5 shrink-0 items-center justify-center">
              {checked && <Check className="size-3.5 text-primary" aria-hidden="true" />}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** 区块小标题：`h3`（页面标题 `h1`、分区标题 `h2` 都在它上面，层级不跳）。 */
function SectionHeading({ Icon, children }: { Icon: LucideIcon; children: ReactNode }) {
  return (
    <h3 className="flex items-center gap-1.5 text-xs font-semibold tracking-wider text-muted-foreground uppercase">
      <Icon className="size-3.5" aria-hidden="true" />
      <span>{children}</span>
    </h3>
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
    <div className="space-y-5">
      <div className="space-y-2">
        <SectionHeading Icon={User}>登录身份</SectionHeading>
        <div className="flex items-center gap-3 rounded-xl border border-border/70 bg-muted/20 px-3 py-2.5">
          <Avatar size="default" className="border border-border/80">
            <AvatarFallback className="bg-primary/10 text-xs font-medium text-primary">
              {initial}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="truncate text-xs font-medium text-foreground">{nickname}</span>
              {roleLabel && (
                <span className="shrink-0 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                  {roleLabel}
                </span>
              )}
            </div>
            <div
              className="truncate text-[11px] text-muted-foreground"
              title={userEmail ?? undefined}
            >
              {userEmail ?? '未登录'}
            </div>
          </div>
        </div>
      </div>

      {/* 修改密码：成功会让所有设备下线，由 App 统一退回登录页 */}
      <div className="space-y-2">
        <SectionHeading Icon={KeyRound}>修改密码</SectionHeading>
        <PasswordChangeForm onPasswordChanged={onPasswordChanged} />
      </div>
    </div>
  );
}

/** 外观与主题。 */
export function AppearanceSection() {
  const { theme, setTheme } = useTheme();

  return (
    <div className="space-y-2">
      <SectionHeading Icon={Sun}>主题</SectionHeading>
      <PreferenceGroup
        ariaLabel="外观主题"
        options={THEME_CARDS}
        value={theme}
        onChange={setTheme}
      />
      <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
        「跟随系统」会在操作系统切换深浅色时自动同步，不必再回来改一次。
      </p>
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

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <SectionHeading Icon={AlignLeft}>工作步骤展示</SectionHeading>
        <PreferenceGroup
          ariaLabel="工作步骤展示"
          options={DETAIL_LEVEL_CARDS}
          value={level}
          onChange={setLevel}
        />
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          四档都能点开看细节；这里只决定默认露多少。改动立即生效，不影响已有对话的内容。
        </p>
      </div>

      {/* 与上面四档**正交**（所以是独立一组而不是第五档）。默认「回答结束后」，
          长回答里想回看刚跑完的过程才需要改成「下次有新消息时」。 */}
      <div className="space-y-2">
        <SectionHeading Icon={ChevronsUpDown}>工作步骤收起时机</SectionHeading>
        <PreferenceGroup
          ariaLabel="工作步骤收起时机"
          options={COLLAPSE_TIMING_CARDS}
          value={collapseTiming}
          onChange={setCollapseTiming}
        />
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          只影响刚结束的那一轮何时收起；选「下次有新消息时」可以在提问前回看它刚才做了什么。
        </p>
      </div>

      {/* 性能与用量：只影响输入框下方那排统计读数显示多少，不改任何记账与落库 */}
      <div className="space-y-2">
        <SectionHeading Icon={Activity}>性能与用量</SectionHeading>
        <PreferenceGroup
          ariaLabel="性能与用量"
          options={PERFORMANCE_USAGE_CARDS}
          value={performanceUsage.mode}
          onChange={performanceUsage.setMode}
        />
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          只影响输入框下方读数显示多少；上下文占用环始终显示，记账与落库也不受影响。
        </p>
      </div>
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
 * 设置页：居中一列，左边分区菜单、右边当前分区的内容。
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
    // 整块设置收成一列居中：内容本来就少，卡片随内容长，不拉满屏高
    <div className="flex h-full w-full justify-center overflow-y-auto bg-muted/30 text-foreground">
      <div className="flex w-full max-w-2xl flex-col px-3 py-4 md:px-4 md:py-8">
        <header className="flex items-center justify-between gap-2 px-1 pb-3">
          <h1 className="text-sm font-semibold tracking-tight">设置</h1>
          {/* 只留一个图标：文字版「返回对话」在页头是多余的重量，退出本该一眼可见 */}
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="关闭设置"
            className="-mr-1 cursor-pointer text-muted-foreground hover:text-foreground"
          >
            <X className="size-4" aria-hidden="true" />
          </Button>
        </header>

        <div className="flex flex-col overflow-hidden rounded-2xl border border-border/70 bg-card shadow-xs md:flex-row">
          {/* 左：分区菜单。窄屏横排成一条可横滑的标签行 */}
          <nav
            aria-label="设置分区"
            className="shrink-0 border-b border-border/60 p-2 md:w-44 md:border-r md:border-b-0 md:p-2.5"
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
                      'flex shrink-0 cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs whitespace-nowrap transition focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none',
                      selected
                        ? 'bg-primary/10 font-medium text-foreground'
                        : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground'
                    )}
                  >
                    <section.Icon
                      className={cn('size-4 shrink-0', selected ? 'text-primary' : undefined)}
                      aria-hidden="true"
                    />
                    <span>{section.label}</span>
                  </button>
                );
              })}
            </div>
          </nav>

          {/* 右：当前分区的展示与操作 */}
          <section className="flex min-w-0 flex-1 flex-col">
            <div className="shrink-0 border-b border-border/60 px-4 py-3">
              <h2 className="text-xs font-semibold tracking-tight">{activeSection.label}</h2>
              <p className="mt-0.5 text-[10.5px] leading-relaxed text-muted-foreground">
                {activeSection.description}
              </p>
            </div>

            <div className="px-4 py-4">
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
          </section>
        </div>
      </div>
    </div>
  );
}
