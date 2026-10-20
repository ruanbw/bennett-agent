/**
 * 模型条目的身份标签。
 *
 * 后端 `/api/models` 的 `name` 来自管理员设置的展示名（同步时取上游 display_name），
 * 上游并没有保证它在同一提供商内唯一：本机就出现过三条展示名一模一样的
 * `Deepseek-V4.1-Flash`（分别是 `auto/`、`workbuddy/`、`workbuddy-ai/` 三条路由）。
 * 只渲染 `name` 时聊天框里的选项完全不可区分，也无法和后台管理里的
 * 「展示名 + model_id」对上号。
 *
 * 因此选择器的第二行固定给出「提供商 · 上游模型 ID」：提供商回答「谁在提供」，
 * 上游 ID 回答「具体是哪一条路由」。两者都缺时返回空串，由调用方决定占位。
 */

export interface ModelIdentity {
  provider?: string | null;
  model_id?: string | null;
}

/** 第二行标签：`默认网关 · auto/deepseek-v4.1-flash`。 */
export function modelSubtitle(model: ModelIdentity): string {
  const provider = model.provider?.trim() ?? '';
  const route = model.model_id?.trim() ?? '';

  if (provider && route) return `${provider} · ${route}`;
  // 旧版网关路径没有 model_id：保持原来「只显示提供商」的样式，不要凭空造分隔符。
  return provider || route;
}

/** 悬停提示用的完整标识：`DeepSeek V4.1 Flash · 默认网关 · opendesign/...`。 */
export function modelFullLabel(name: string, model: ModelIdentity): string {
  const parts = [name.trim(), modelSubtitle(model)].filter(Boolean);
  return parts.join(' · ');
}
