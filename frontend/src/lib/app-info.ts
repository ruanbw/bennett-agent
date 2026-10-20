/**
 * 应用自身的事实（版本号），唯一来源是构建注入。
 *
 * 之前设置面板写死 `v0.1.0`，package.json 却是 `0.0.0`：展示给用户的版本与实际构建毫无
 * 关系。`vite.config.js` 在构建时把 package.json 的 version 注入为 `__APP_VERSION__`，
 * 这里是唯一的读取点——组件不再碰版本字面量。
 */

/** 应用版本（semver，不带 `v` 前缀）。构建未注入时退回 `0.0.0-dev`。 */
export const APP_VERSION: string =
  typeof __APP_VERSION__ === 'string' && __APP_VERSION__ ? __APP_VERSION__ : '0.0.0-dev';
