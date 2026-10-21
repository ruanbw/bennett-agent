/**
 * 登录 / 注册表单的字段级状态。
 *
 * 此前两个输入框共用一句 `aria-invalid={invalid}`，而 `invalid` 是「密码太短 **或**
 * 服务端报错」：服务端说「该邮箱已注册」时，密码框也被标成无效——读屏用户会被指到
 * 一个其实没问题的字段。这里把「本地能确定哪个字段有问题」与「服务端只给了一句结论」
 * 分开：
 *
 * - 密码太短是**确定**的字段问题（只发生在注册模式）；
 * - 服务端错误（邮箱或密码错误、该邮箱已注册…）是对整个表单的结论，不冒充某个字段的
 *   校验结果，只用 `aria-describedby` 把它挂到两个输入框上，读屏仍能听到；
 * - `aria-describedby` 也分字段：邮箱不该被指向「密码至少 8 个字符」。
 *
 * 纯函数：登录页在 node 环境里没有 jsdom，只能靠渲染出的静态 HTML 做断言。
 */

export interface AuthFieldState {
  emailInvalid: boolean;
  passwordInvalid: boolean;
  /** 邮箱输入框的 `aria-describedby`；空串表示没有。 */
  emailDescribedBy: string;
  /** 密码输入框的 `aria-describedby`；空串表示没有。 */
  passwordDescribedBy: string;
}

export interface AuthFieldStateInput {
  mode: 'login' | 'register';
  password: string;
  /** 服务端或 useAuth 给出的结论；null 表示没有错误。 */
  error: string | null;
  minPasswordLength: number;
  /** 注册模式下密码提示（「至少 8 个字符」）的元素 id。 */
  passwordHintId?: string;
  /** 错误提示的元素 id。 */
  errorId?: string;
}

export function readAuthFieldState({
  mode,
  password,
  error,
  minPasswordLength,
  passwordHintId = 'auth-password-hint',
  errorId = 'auth-error',
}: AuthFieldStateInput): AuthFieldState {
  const tooShort = mode === 'register' && password.length > 0 && password.length < minPasswordLength;

  const emailIds: string[] = [];
  const passwordIds: string[] = [];
  if (error && errorId) {
    emailIds.push(errorId);
    passwordIds.push(errorId);
  }
  // 密码长度提示只对密码框成立：挂到邮箱上会让读屏念一段与之无关的要求
  if (mode === 'register' && passwordHintId) passwordIds.push(passwordHintId);

  return {
    emailInvalid: false,
    passwordInvalid: tooShort,
    emailDescribedBy: emailIds.join(' '),
    passwordDescribedBy: passwordIds.join(' '),
  };
}
