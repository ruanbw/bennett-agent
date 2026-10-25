/**
 * 统一请求层：基址拼接、cookie 携带、超时。
 *
 * 这是管理后台自己的简化版（`frontend/src/lib/api.ts` 是用户端的完整版）：不带有
 * 401 全局监听与认证世代（epoch）——那套机制服务于用户端「任何请求 401 都退回登录页 +
 * 多标签同步」的场景；管理后台的请求面很小（auth + admin 两组接口），401 的处理由
 * `App.tsx` 的探测/守卫与各面板的 403 渲染各自承接，引入全局广播反而多一套要维护的状态。
 */

/** 后端 API 基址，来自 `admin-frontend/.env` 的 `VITE_API_BASE_URL`（见 `.env.example`）。 */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

export interface ApiFetchOptions extends RequestInit {
  /**
   * 超时（毫秒）。到点中止请求（fetch 抛 AbortError）。
   *
   * 认证请求必须带它：`/me` 探测挂起会让登录页永远停在 loading。调用方自己传了
   * `signal` 时不再叠加超时——需要合并两个信号的调用点目前不存在。
   */
  timeoutMs?: number;
}

/**
 * 发起请求。
 *
 * `credentials: 'include'` 在**同源**（Vite 代理 / 生产反代）下与默认行为一致
 * （cookie 照常带上），在**跨域**下则是唯一会带上 cookie 的模式。所以它对两种部署
 * 都正确——前提是跨域时后端配了 `allow_credentials=True`，见 `app/server.py`。
 */
export async function apiFetch(path: string, options: ApiFetchOptions = {}): Promise<Response> {
  const { timeoutMs, ...init } = options;

  return fetch(`${API_BASE_URL}${path}`, {
    credentials: 'include',
    ...init,
    signal: init.signal ?? (timeoutMs ? timeoutSignal(timeoutMs) : undefined),
  });
}

/**
 * `AbortSignal.timeout` 的降级包装：老 WebView 没有这个静态方法，直接调用会让
 * 启动探测和登录同步抛错（表现为「无法连接服务器」）。缺 API 时退回
 * AbortController + setTimeout，语义一致（超时以 TimeoutError 中止）。
 */
function timeoutSignal(ms: number): AbortSignal {
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    return AbortSignal.timeout(ms);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(new DOMException('TimeoutError', 'TimeoutError')), ms);
  return controller.signal;
}

/**
 * 从错误响应里提取可读说明。
 *
 * 三种 `detail` 形状都要处理，否则用户只会看到「HTTP 错误: 422」而不知道该改什么：
 *
 *   - 字符串：业务错误码，如 `LOGIN_BAD_CREDENTIALS`；
 *   - 数组：FastAPI 的请求校验错误，`loc` 形如 `['body','messages',0,'content']`；
 *   - 对象：fastapi-users 的 `{code, reason}`，如注册密码不合规。
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
