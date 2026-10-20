/**
 * 侧边栏搜索无结果时的空态。
 *
 * 此前只有一句「未找到相关对话」：用户不知道搜的是什么、为什么没命中（多词是 AND，
 * 还要落在同一片段），也没有原地重试的入口（只能手动清空输入框）。
 * 这里把当前词表回显出来，并明确搜索覆盖的范围，再给一个「清除搜索」。
 */

export interface SidebarSearchEmptyProps {
  /** 已解析的词表（与输入同步，不是防抖后的结果）。 */
  tokens: string[];
  /** 清空输入但保留搜索面板与焦点，方便立刻重输。 */
  onClearSearch: () => void;
}

export function SidebarSearchEmpty({ tokens, onClearSearch }: SidebarSearchEmptyProps) {
  return (
    <div
      data-slot="sidebar-search-empty"
      className="px-4 py-10 text-center space-y-2"
      role="status"
    >
      <p className="text-xs text-muted-foreground">
        没有同时包含{' '}
        {tokens.map((token, index) => (
          <span key={`${index}-${token}`} className="font-medium text-foreground/80">
            「{token}」{index < tokens.length - 1 ? ' ' : ''}
          </span>
        ))}{' '}
        的对话
      </p>
      <p className="text-[11px] text-muted-foreground/70">
        搜索会匹配标题、消息正文和工具调用；多个关键词需要同时命中。
      </p>
      <button
        type="button"
        onClick={onClearSearch}
        className="text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground cursor-pointer"
      >
        清除搜索
      </button>
    </div>
  );
}
