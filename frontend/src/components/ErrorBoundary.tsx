import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from './ui/button';

export interface ErrorBoundaryProps {
  children: ReactNode;
  fallbackTitle?: string;
  onReset?: () => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
  };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('ErrorBoundary 捕获到组件异常:', error, errorInfo);
  }

  handleRetry = () => {
    this.props.onReset?.();
    this.setState({ hasError: false, error: null });
  };

  override render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[240px] h-full w-full p-6 text-center select-none bg-background text-foreground">
          <div className="rounded-full bg-destructive/10 p-3 text-destructive mb-3">
            <AlertTriangle className="size-6" />
          </div>
          <h3 className="text-base font-semibold tracking-tight mb-1">
            {this.props.fallbackTitle ?? '页面出现异常'}
          </h3>
          <p className="text-xs text-muted-foreground max-w-md mb-4 break-words">
            {this.state.error?.message || '组件渲染发生未预期的错误，请重试或刷新页面。'}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={this.handleRetry}
            className="cursor-pointer gap-1.5"
          >
            <RefreshCw className="size-3.5" />
            重试
          </Button>
        </div>
      );
    }

    return this.props.children;
  }
}
