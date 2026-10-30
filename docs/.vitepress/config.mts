import { defineConfig } from 'vitepress'

// 线上部署在 https://ruanbw.github.io/bennett-agent/，本地 dev/preview 用 `/`。
// 用环境变量而不是 `NODE_ENV`：vitepress build 与 dev 的 NODE_ENV 行为不该被这里依赖。
const base = process.env.VITEPRESS_BASE ?? '/bennett-agent/'

export default defineConfig({
  lang: 'zh-CN',
  title: 'Bennett Agent',
  titleTemplate: ':title | Bennett Agent 文档',
  description: '自托管的多租户 AI 对话助手：PydanticAI + LiteLLM、FastAPI SSE、React 19、沙箱执行与独立管理后台。',
  base,
  cleanUrls: true,
  lastUpdated: true,
  sitemap: {
    hostname: 'https://ruanbw.github.io/bennett-agent/',
  },
  head: [
    ['meta', { name: 'theme-color', content: '#10b981' }],
    ['meta', { name: 'author', content: 'Bennett Agent contributors' }],
  ],
  transformHead({ pageData }) {
    return [
      ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}logo.svg` }],
      ['meta', { property: 'og:type', content: 'website' }],
      ['meta', { property: 'og:title', content: pageData.frontmatter.title ?? pageData.title ?? 'Bennett Agent' }],
    ]
  },
  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'Bennett Agent',
    nav: [
      { text: '指南', link: '/guide/', activeMatch: '/guide/' },
      { text: '功能', link: '/features/', activeMatch: '/features/' },
      { text: '设计', link: '/design/', activeMatch: '/design/' },
      { text: '运维', link: '/ops/', activeMatch: '/ops/' },
      // 演示页是 public/ 下的独立静态 HTML，不是 VitePress 页面；
      // 不加 target 会被客户端路由器接管并按 404 处理。
      { text: '演示', link: '/demo/', target: '_blank' },
    ],
    sidebar: {
      '/guide/': [
        {
          text: '上手',
          items: [
            { text: '文档导览', link: '/guide/' },
            { text: '环境准备', link: '/guide/prerequisites' },
            { text: '本地运行', link: '/guide/quickstart' },
            { text: '配置手册', link: '/guide/configuration' },
            { text: '前端开发与构建', link: '/guide/frontend' },
            { text: '生产部署', link: '/guide/deployment' },
          ],
        },
      ],
      '/features/': [
        {
          text: '功能',
          items: [
            { text: '功能总览', link: '/features/' },
            { text: '对话与流式事件', link: '/features/chat' },
            { text: '工具系统', link: '/features/tools' },
            { text: '沙箱执行', link: '/features/sandbox' },
            { text: '上下文预算与压缩', link: '/features/context' },
            { text: '模型接入与容灾', link: '/features/models' },
            { text: '账号、认证与 RBAC', link: '/features/accounts' },
            { text: '跨设备同步', link: '/features/sync' },
            { text: '管理后台', link: '/features/admin' },
          ],
        },
      ],
      '/design/': [
        {
          text: '设计',
          items: [
            { text: '架构总览', link: '/design/' },
            { text: '一轮对话的链路', link: '/design/request-lifecycle' },
            { text: '后端分层与装配', link: '/design/backend' },
            { text: '数据模型', link: '/design/data-model' },
            { text: 'API 与 SSE 契约', link: '/design/api' },
            { text: '安全设计', link: '/design/security' },
            { text: '质量平台', link: '/design/quality-platform' },
          ],
        },
      ],
      '/ops/': [
        {
          text: '运维',
          items: [
            { text: '运维总览', link: '/ops/' },
            { text: '测试体系', link: '/ops/testing' },
            { text: '可观测性', link: '/ops/observability' },
            { text: '故障排查', link: '/ops/troubleshooting' },
          ],
        },
      ],
    },
    socialLinks: [{ icon: 'github', link: 'https://github.com/ruanbw/bennett-agent' }],
    editLink: {
      pattern: 'https://github.com/ruanbw/bennett-agent/edit/master/docs/:path',
      text: '在 GitHub 上编辑此页',
    },
    lastUpdated: {
      text: '最后更新',
      formatOptions: { dateStyle: 'medium', timeStyle: 'short' },
    },
    search: {
      provider: 'local',
      options: {
        translations: {
          button: {
            buttonText: '搜索文档',
            buttonAriaLabel: '搜索文档',
          },
          modal: {
            noResultsText: '没有找到结果',
            resetButtonTitle: '清空查询',
            footer: {
              selectText: '选择',
              navigateText: '切换',
              closeText: '关闭',
            },
          },
        },
      },
    },
    outline: {
      level: [2, 3],
      label: '本页目录',
    },
    docFooter: {
      prev: '上一篇',
      next: '下一篇',
    },
    darkModeSwitchLabel: '主题',
    lightModeSwitchTitle: '切换到浅色模式',
    darkModeSwitchTitle: '切换到深色模式',
    sidebarMenuLabel: '菜单',
    returnToTopLabel: '回到顶部',
    footer: {
      message: '基于 MIT License 发布',
      copyright: 'Bennett Agent contributors',
    },
  },
})
