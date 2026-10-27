/**
 * 后台内的 404。
 *
 * 不静默重定向回首页：手抄 URL、过期书签、删掉的菜单项都会落到这里，
 * 重定向会让人以为「那个页面被改名了」，而不是「地址写错了」。
 * 给出回到概览的入口，并列出当前可用的菜单（来自同一张导航表）。
 */

import { Link, useLocation } from 'react-router';
import { Compass, SearchX } from 'lucide-react';

import { buttonVariants } from '@/components/ui/button';
import { NAV_ITEMS, HOME_PATH } from '@/lib/nav';

export function NotFoundPanel() {
  const location = useLocation();

  return (
    <section className="flex flex-col items-center justify-center py-16 text-center">
      <SearchX className="mb-3 size-10 text-muted-foreground/60" aria-hidden="true" />
      <h2 className="text-base font-semibold">没有这个页面</h2>
      <p className="mt-1.5 max-w-md text-xs leading-relaxed text-muted-foreground">
        地址 <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">{location.pathname}</code>{' '}
        不在管理后台的路由里。可能是链接过期，或者这一页已经被合并到别处。
      </p>

      <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
        <Link to={HOME_PATH} className={buttonVariants({ size: 'sm' })}>
          <Compass className="size-3.5" aria-hidden="true" />
          回到概览
        </Link>
        {NAV_ITEMS.filter((item) => item.to !== HOME_PATH).map(({ to, label }) => (
          <Link key={to} to={to} className={buttonVariants({ variant: 'outline', size: 'sm' })}>
            {label}
          </Link>
        ))}
      </div>
    </section>
  );
}
