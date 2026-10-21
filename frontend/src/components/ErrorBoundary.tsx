import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw, RotateCw } from 'lucide-react';
import { Button } from './ui/button';
import { describeCrash } from '../lib/error-boundary-text';

export interface ErrorBoundaryProps {
  children: ReactNode;
  fallbackTitle?: string;
  onReset?: () => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  /** 技术原文折叠区是否展开。每次崩溃重置为收起：用户先看到结论。 */
  showDetail: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
    showDetail: false,
  };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error, showDetail: false };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('ErrorBoundary 捕获到组件异常:', error, errorInfo);
  }

  handleRetry = () => {
    this.props.onReset?.();
    this.setState({ hasError: false, error: null, showDetail: false });
  };

  handleReload = () => {
    // 先重置 state：reload 在某些嵌入环境（iframe / 预览沙箱）里会被拦下，
    // 那时至少要让界面回到可尝试的状态，而不是停在错误页
    this.setState({ hasError: false, error: null, showDetail: false });
    window.location.reload();
  };

  handleToggleDetail = () => {
    this.setState((prev) => ({ showDetail: !prev.showDetail }));
  };

  override render() {
    if (!this.state.hasError) return this.props.children;

    // 结论与下一步由 `describeCrash` 给：直接把 `error.message` 贴在页面上时，
    // 用户看到的是 "Cannot read properties of undefined"——读不懂也不知道做什么。
    const report = describeCrash(this.state.error);

    return (
      <div
        role="alert"
        className="flex flex-col items-center justify-center min-h-[240px] h-full w-full p-6 text-center select-none bg-background text-foreground"
      >
        <div className="rounded-full bg-destructive/10 p-3 text-destructive mb-3">
          <AlertTriangle className="size-6" aria-hidden="true" />
        </div>
        <h3 className="text-base font-semibold tracking-tight mb-1">
          {this.props.fallbackTitle ?? '页面出现异常'}
        </h3>
        <p className="text-xs text-muted-foreground max-w-md mb-4 break-words">{report.summary}</p>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={this.handleRetry}
            className="cursor-pointer gap-1.5"
          >
            <RefreshCw className="size-3.5" aria-hidden="true" />
            重试
          </Button>
          {/* 重试只重置边界本身；异常若来自环境/模块级问题，重试必然再炸一次，需要整页重载 */}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={this.handleReload}
            className="cursor-pointer gap-1.5"
          >
            <RotateCw className="size-3.5" aria-hidden="true" />
            重新加载
          </Button>
        </div>

        {/* 技术原文只给「反馈问题时复制」，默认收起：它不是给用户读的结论 */}
        {report.detail && (
          <div className="mt-4 w-full max-w-md">
            <button
              type="button"
              onClick={this.handleToggleDetail}
              aria-expanded={this.state.showDetail}
              className="text-muted-foreground hover:text-foreground cursor-pointer rounded text-[11px] underline-offset-2 transition-colors hover:underline focus-visible:ring-ring/50 focus-visible:ring-1 focus-visible:outline-none"
            >
              {this.state.showDetail ? '收起错误详情' : '查看错误详情'}
            </button>
            {this.state.showDetail && (
              <pre className="bg-muted/60 text-muted-foreground mt-2 max-h-32 overflow-auto rounded-md p-2 text-left text-[11px] leading-relaxed break-all whitespace-pre-wrap">
                {report.detail}
              </pre>
            )}
          </div>
        )}
      </div>
    );
  }
}
