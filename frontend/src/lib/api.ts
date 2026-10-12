/**
 * 统一请求层：基址拼接、cookie 携带、401 的全局处理。
 *
 * ## 为什么需要它
 *
 * 认证引入后任何请求都可能因「会话过期」返回 401，而 401 的正确反应是**全局的**
 * （退回登录页），不是每个调用点各自处理。此前 `App.tsx`、`chat-transport.ts`、
 * `ChatProvider.tsx` 三处各写一遍 `fetch`，加上 401 判断就是三份重复逻辑，而且
 * 以后新增调用点很容易漏掉。
 *
 * ## 为什么用模块级回调而不是 React Context
 *
 * `chat-transport.ts` 是纯模块（在 node 环境的 vitest 里直接测），不能依赖 React
 * Context。模块级监听器让纯模块也能参与全局 401 处理，同时保持可测。
 */

import { API_BASE_URL } from './constants';

/** 会话失效（401）时的通知。由 `useAuth` 注册，把状态切回未登录。 */
type UnauthorizedListener = () => void;

let unauthorizedListener: UnauthorizedListener | null = null;

/** 注册 401 监听；传 `null` 表示清除（组件卸载时用）。 */
export function setUnauthorizedListener(listener: UnauthorizedListener | null): void {
  unauthorizedListener = listener;
}

export interface ApiFetchOptions extends RequestInit {
  /**
   * 401 时是否触发全局登出回调，默认 `true`。
   *
   * 需要关掉的场合是「401 本身就是预期结果」：启动时探测登录态、以及登录接口
   * 自身的失败——这两处再触发一次「会话失效」没有意义，只会多绕一圈。
   */
  notifyUnauthorized?: boolean;
}

/**
 * 发起请求。
 *
 * `credentials: 'include'` 在**同源**（Vite 代理 / 生产反代）下与默认行为一致
 * （cookie 照常带上），在**跨域**下则是唯一会带上 cookie 的模式。所以它对两种部署
 * 都正确——前提是跨域时后端配了 `allow_credentials=True`，见 `app/server.py`。
 */
export async function apiFetch(path: string, options: ApiFetchOptions = {}): Promise<Response> {
  const { notifyUnauthorized = true, ...init } = options;

  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: 'include',
    ...init,
  });

  if (response.status === 401 && notifyUnauthorized) {
    unauthorizedListener?.();
  }

  return response;
}

/**
 * 从错误响应里提取可读说明。
 *
 * 三种 `detail` 形状都要处理，否则用户只会看到「HTTP 错误: 422」而不知道该改什么：
 *
 *   - 字符串：业务错误码，如 `LOGIN_BAD_CREDENTIALS`；
 *   - 数组：FastAPI 的请求校验错误，`loc` 形如 `['body','messages',0,'content']`；
 *   - 对象：fastapi-users 的 `{code, reason}`，注册密码不合规走这条。
 *
 * 取不到时返回空串，由调用方退回状态码——**不编造文案**。
 */
export async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body?.detail;

    if (typeof detail === 'string') return detail;

    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown; msg?: unknown } | undefined;
      // 去掉首段的 'body'，剩下的就是用户能认的字段路径
      const where = Array.isArray(first?.loc) ? first.loc.slice(1).join('.') : '';
      const message = typeof first?.msg === 'string' ? first.msg : '';
      return where ? `${where}: ${message}` : message;
    }

    if (detail && typeof detail === 'object') {
      const { code, reason } = detail as { code?: unknown; reason?: unknown };
      // reason 是写给人看的说明，code 只是标识；有 reason 就别拿 code 顶替
      if (typeof reason === 'string' && reason) return reason;
      if (typeof code === 'string' && code) return code;
    }

    return '';
  } catch {
    // 响应体不是 JSON（例如反向代理返回 HTML 错误页）：退回状态码
    return '';
  }
}
