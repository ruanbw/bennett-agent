/**
 * 侧边栏搜索无结果 / 扫描失败时的空态。
 *
 * 此前只有一句「未找到相关对话」：用户不知道搜的是什么、为什么没命中（多词是 AND，
 * 还要落在同一片段），也没有原地重试的入口（只能手动清空输入框）。
 * 这里把当前词表回显出来，并明确搜索覆盖的范围，再给一个「清除搜索」。
 *
 * 扫描失败不能复用「没有命中」的文案：那会把「数据库读失败」说成「确实没有这条会话」，
 * 用户只会换个词继续白找。错误态单独给原因与重试。
 */

export interface SidebarSearchEmptyProps {
  /** 已解析的词表（与输入同步，不是防抖后的结果）。 */
  tokens: string[];
  /** 清空输入但保留搜索面板与焦点，方便立刻重输。 */
  onClearSearch: () => void;
}

export function SidebarSearchEmpty({ tokens, onClearSearch }: SidebarSearchEmptyProps) {
  return (
    <div data-slot="sidebar-search-empty" className="px-4 py-10 text-center space-y-2">
      <div role="status" className="space-y-2">
        {tokens.length > 0 ? (
          <>
            <p className="text-xs text-muted-foreground">没有找到同时包含这些关键词的对话：</p>
            <p className="flex flex-wrap items-center justify-center gap-x-1 gap-y-0.5">
              {tokens.map((token, index) => (
                <span
                  key={`${index}-${token}`}
                  className="text-xs font-medium text-foreground/80"
                >
                  「{token}」
                </span>
              ))}
            </p>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">没有匹配的对话</p>
        )}
        <p className="text-[11px] text-muted-foreground/70">
          搜索会匹配标题、消息正文和工具调用；多个关键词需要同时命中。
        </p>
      </div>
      <button
        type="button"
        onClick={onClearSearch}
        className="rounded text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
      >
        清除搜索
      </button>
    </div>
  );
}

export interface SidebarSearchErrorProps {
  message: string;
  onRetry: () => void;
  onClearSearch: () => void;
}

/** 搜索扫描失败：给出原因 + 原地重试，而不是伪装成「没有命中」。 */
export function SidebarSearchError({ message, onRetry, onClearSearch }: SidebarSearchErrorProps) {
  return (
    <div data-slot="sidebar-search-error" role="alert" className="px-4 py-10 text-center space-y-2">
      <p className="text-xs text-destructive">{message}</p>
      <p className="text-[11px] text-muted-foreground/70">
        搜索只读本机数据库；可以重新搜索，或清除关键词回到列表。
      </p>
      <div className="flex items-center justify-center gap-3">
        <button
          type="button"
          onClick={onRetry}
          className="rounded text-[11px] font-medium text-foreground underline underline-offset-2 hover:text-primary cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
        >
          重新搜索
        </button>
        <button
          type="button"
          onClick={onClearSearch}
          className="rounded text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
        >
          清除搜索
        </button>
      </div>
    </div>
  );
}
