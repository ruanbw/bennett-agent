/**
 * 表单校验模型（zod）。
 *
 * 校验规则与错误文案集中在这里，而不是散在组件的事件处理里：
 *
 * - **文案只有一份**：`validateProviderDraft` 之前手写了一套 if-else，对话框又各自
 *   再写一遍 focus 逻辑，两边容易漂。现在 schema 是唯一事实来源，对话框只消费
 *   `issues`。
 * - **能被单测直接跑**：这些 schema 是纯数据，vitest 的 node 环境即可覆盖，不需要
 *   渲染组件。
 * - **与后端对齐**：`base_url` 的协议判断与后端同样是大小写敏感的 `startswith`——
 *   本地放行一个服务端必拒的值（`HTTPS://…`）会让管理员在提交后才看到英文 422。
 */

import { z } from 'zod';

/** 与后端 `MAX_NAME_CHARS` / `MAX_MODEL_ID_CHARS` 对齐的上限。 */
export const MAX_MODEL_DISPLAY_NAME_CHARS = 200;

/**
 * 提供商草稿。
 *
 * 选了厂商预设时地址可留空（后端用预设地址），但填了仍要检查协议。
 * 用 `superRefine` 而不是两个字段各自 `.refine`：地址的「可以空」取决于名称之外的
 * `presetKey`，字段级校验拿不到兄弟字段。
 */
export const providerDraftSchema = z
  .object({
    name: z.string().trim().min(1, '请填写名称'),
    baseUrl: z.string(),
    presetKey: z.string(),
    apiKey: z.string(),
    enabled: z.boolean(),
    clearApiKey: z.boolean(),
  })
  .superRefine((draft, ctx) => {
    const usingPreset = draft.presetKey !== 'custom' && draft.presetKey !== '';
    const baseUrl = draft.baseUrl.trim();

    if (!baseUrl && !usingPreset) {
      ctx.addIssue({
        code: 'custom',
        path: ['baseUrl'],
        message: '请填写接口地址，或选择一个厂商预设',
      });
      return;
    }
    if (baseUrl && !baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
      ctx.addIssue({
        code: 'custom',
        path: ['baseUrl'],
        message: '接口地址必须以 http:// 或 https:// 开头',
      });
    }
  });

export type ProviderFormValues = z.infer<typeof providerDraftSchema>;

/**
 * 模型元数据草稿。
 *
 * 两个 token 上限可留空（= 未知，提交 null），填了就必须是正整数。字符串 → 数字的
 * 转换放在 `buildModelPayload` 里，不做成 zod transform：表单里编辑到一半的
 * `"12a"` 必须留在输入框里让人改，不能在解析时被悄悄换成别的值。
 */
export const modelDraftSchema = z.object({
  displayName: z
    .string()
    .trim()
    .min(1, '请填写显示名称')
    .max(MAX_MODEL_DISPLAY_NAME_CHARS, `名称最多 ${MAX_MODEL_DISPLAY_NAME_CHARS} 字`),
  contextWindow: z.string().refine((value) => isOptionalPositiveInt(value), {
    message: '上下文窗口必须是正整数，或留空',
  }),
  maxOutputTokens: z.string().refine((value) => isOptionalPositiveInt(value), {
    message: '最大输出 tokens 必须是正整数，或留空',
  }),
  reasoning: z.boolean(),
});

export type ModelFormValues = z.infer<typeof modelDraftSchema>;

function isOptionalPositiveInt(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return true;
  if (!/^\d+$/.test(trimmed)) return false;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed > 0;
}

/**
 * 校验通过返回 `error: null`，否则返回第一个问题。
 *
 * 对话框里用 `zodResolver` 直接把 issue 映射到字段；这个包装是给**单测**用的：
 * 一条断言就能表达「哪个字段、什么文案」，不必逐层翻 `safeParse` 的结果。
 */
export function validate<T>(
  schema: z.ZodType<T>,
  value: unknown,
): { field: string; message: string } | null {
  const result = schema.safeParse(value);
  if (result.success) return null;
  const issue = result.error.issues[0];
  return {
    field: String(issue?.path[0] ?? ''),
    message: issue?.message ?? '填写有误',
  };
}

/** 提供商草稿 → 第一个校验问题（`field` 与表单字段同名）。 */
export function validateProviderDraft(draft: unknown): { field: string; message: string } | null {
  return validate(providerDraftSchema, draft);
}

/** 模型草稿 → 第一个校验问题。 */
export function validateModelDraft(draft: unknown): { field: string; message: string } | null {
  return validate(modelDraftSchema, draft);
}
