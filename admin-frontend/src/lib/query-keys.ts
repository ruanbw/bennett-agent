/**
 * 查询键：集中一处，失效时不会把字符串写错。
 *
 * 单独一个文件（而不是跟 Provider 放一起）：组件文件只导出组件，Fast Refresh 才不会
 * 因为「混着导出常量」而整页重挂。
 */
export const queryKeys = {
  providers: ['admin', 'providers'] as const,
  providerModels: (providerId: string) => ['admin', 'providers', providerId, 'models'] as const,
  providerPresets: ['admin', 'provider-presets'] as const,
  users: (params: { query: string; status: string; limit: number; offset: number }) =>
    ['admin', 'users', params] as const,
};
