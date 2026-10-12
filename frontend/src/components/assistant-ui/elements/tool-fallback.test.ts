import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import {
  ToolFallbackArgs,
  ToolFallbackResult,
  ToolFallbackRoot,
  ToolFallbackTrigger,
} from './tool-fallback.aui'

/**
 * 组件层没有 jsdom，但 args/result 文本块不依赖任何运行时上下文，
 * 可以直接用 react-dom/server 渲染出静态 HTML 做断言。
 */
describe('ToolFallbackArgs / ToolFallbackResult 的超长文本处理', () => {
  it('args 超过阈值时只渲染预览，并给出展开入口', () => {
    const argsText = `{"query":"${'长'.repeat(2500)}"}`
    const html = renderToStaticMarkup(createElement(ToolFallbackArgs, { argsText }))

    expect(html).toContain('展开全部')
    expect(html).toContain('max-h-64')
    expect(html).not.toContain(argsText)
  })

  it('result 超过阈值时同样先折叠，并保留 Result 头', () => {
    const result = { payload: 'x'.repeat(2500) }
    const html = renderToStaticMarkup(createElement(ToolFallbackResult, { result }))

    expect(html).toContain('Result:')
    expect(html).toContain('展开全部')
    expect(html).not.toContain('x'.repeat(2500))
  })

  it('短文本完整渲染，不出现展开入口', () => {
    const html = renderToStaticMarkup(
      createElement(ToolFallbackArgs, { argsText: '{"q":"短"}' }),
    )

    expect(html).toContain('{&quot;q&quot;:&quot;短&quot;}')
    expect(html).not.toContain('展开全部')
  })

  it('result 为 undefined 时不渲染任何结果块（完成态必须由 result 支撑）', () => {
    const html = renderToStaticMarkup(createElement(ToolFallbackResult, { result: undefined }))
    expect(html).toBe('')
  })
})

describe('ToolFallbackTrigger 的状态展示', () => {
  const renderTrigger = (status: { type: 'complete' }, result?: unknown) =>
    renderToStaticMarkup(
      createElement(
        ToolFallbackRoot,
        null,
        createElement(ToolFallbackTrigger, { toolName: 'search', status, result }),
      ),
    )

  it('没有 result 时不画绿勾，改为琥珀色的「未完成」', () => {
    const html = renderTrigger({ type: 'complete' }, undefined)

    expect(html).toContain('未完成')
    expect(html).toContain('lucide-circle-minus')
    expect(html).not.toContain('lucide-check')
    expect(html).toContain('amber')
    expect(html).toContain('aria-label="search：未完成，工具没有返回结果"')
  })

  it('拿到 result 才显示完成态的绿勾', () => {
    const html = renderTrigger({ type: 'complete' }, '{"ok":true}')

    expect(html).toContain('lucide-check')
    expect(html).not.toContain('未完成')
  })
})
