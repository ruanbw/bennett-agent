import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import {
  ToolFallbackArgs,
  ToolFallbackResult,
  ToolFallbackRoot,
  ToolFallbackTrigger,
} from './tool-fallback.aui'
import { toolResultForDisplay } from '@/lib/tool-call-view'

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

  it('result 超过阈值时同样先折叠，并保留结果头', () => {
    const result = { payload: 'x'.repeat(2500) }
    const html = renderToStaticMarkup(createElement(ToolFallbackResult, { result }))

    expect(html).toContain('结果：')
    expect(html).toContain('展开全部')
    expect(html).not.toContain('x'.repeat(2500))
  })

  it('失败结果用「错误：」而不是「结果：」，不复用成功外观', () => {
    const html = renderToStaticMarkup(
      createElement(ToolFallbackResult, {
        result: toolResultForDisplay('failed', '工具执行失败'),
      }),
    )

    expect(html).toContain('错误：')
    expect(html).toContain('工具执行失败')
    expect(html).not.toContain('结果：')
  })

  it('短文本完整渲染，不出现展开入口', () => {
    const html = renderToStaticMarkup(
      createElement(ToolFallbackArgs, { argsText: '{"q":"短"}' }),
    )

    expect(html).toContain('参数：')
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
        createElement(ToolFallbackTrigger, { toolName: 'read', status, result }),
      ),
    )

  it('没有 result 时不画绿勾，改为琥珀色的「未完成」', () => {
    const html = renderTrigger({ type: 'complete' }, undefined)

    expect(html).toContain('未完成')
    expect(html).toContain('lucide-circle-minus')
    expect(html).toContain('amber')
    expect(html).toContain('aria-label="读取：未完成，工具没有返回结果"')
  })

  it('拿到 result 时不再报「未完成」，行首是动作图标而不是状态图标', () => {
    const html = renderTrigger({ type: 'complete' }, '{"ok":true}')

    // 行首图标改为「这是什么调用」（读取 → 指南针），状态由右侧耗时位表达。
    // 对勾曾经是行首图标，现在只有异常终态才抢这个位置。
    expect(html).not.toContain('未完成')
    expect(html).toContain('lucide-compass')
    expect(html).toContain('aria-label="读取：已完成"')
  })

  it('行首标题是名词短语（读取），不是工具名也不是叙述句', () => {
    const html = renderTrigger({ type: 'complete' }, '{"ok":true}')

    // 组头说「已读取文件」（叙述），这一行说「读取」（标签）；两者分工不同
    expect(html).toContain('data-slot="tool-fallback-trigger-label"')
    expect(html).toContain('>读取<')
    expect(html).not.toContain('已读取文件')
  })
})
