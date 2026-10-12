import { describe, expect, it } from 'vitest'

import type { ModelItem } from '../types'
import { DEFAULT_REASONING_EFFORT } from './constants'
import {
  DEFAULT_LEVELS,
  OFF,
  PREFERRED_LEVEL,
  isOff,
  levelsFor,
  normalizeEffort,
  reconcileEffort,
  shouldShowEffortSelector,
} from './reasoning-effort'

/**
 * 用例会刻意塞入后端不该返回的坏值（字符串等级、混入数字），验证归一化的健壮性；
 * 因此 overrides 放宽到 unknown 收口，只在返回处断言成线上契约形状。
 */
type ModelOverrides = Partial<Omit<ModelItem, 'supported_reasoning_levels'>> & {
  supported_reasoning_levels?: unknown
}

const model = (overrides: ModelOverrides = {}): ModelItem =>
  ({
    id: 'openai/gpt-4o',
    reasoning: true,
    supported_reasoning_levels: ['low', 'medium', 'high'],
    ...overrides,
  }) as ModelItem

describe('normalizeEffort', () => {
  it('把关闭的各种写法统一成 OFF', () => {
    for (const raw of ['off', 'none', 'false', 'OFF', 'None', ' off ', 'disabled']) {
      expect(normalizeEffort(raw)).toBe(OFF)
    }
  })

  it('等级名小写并去空白', () => {
    expect(normalizeEffort('HIGH')).toBe('high')
    expect(normalizeEffort('  XHigh ')).toBe('xhigh')
  })

  it('无效输入返回 null', () => {
    for (const raw of ['', '   ', null, undefined, 42, {}]) {
      expect(normalizeEffort(raw)).toBeNull()
    }
  })
})

describe('isOff', () => {
  it('只对关闭别名成立', () => {
    expect(isOff('off')).toBe(true)
    expect(isOff('none')).toBe(true)
    expect(isOff('high')).toBe(false)
    expect(isOff(undefined)).toBe(false)
  })
})

describe('levelsFor', () => {
  it('模型明确声明不支持思考时返回空', () => {
    // model_service 的兜底模型正是 reasoning:false + 空等级，它这样做是为了
    // 「避免用户选择后请求失败」——前端必须尊重它，而不是自造等级
    expect(levelsFor(model({ reasoning: false, supported_reasoning_levels: [] }))).toEqual([])
  })

  it('用后端枚举的等级，并补上 OFF', () => {
    expect(levelsFor(model())).toEqual([OFF, 'low', 'medium', 'high'])
  })

  it('只说支持思考但没枚举等级时用保守默认集', () => {
    expect(levelsFor(model({ supported_reasoning_levels: [] }))).toEqual([OFF, ...DEFAULT_LEVELS])
  })

  it('字段缺失（很多网关不返回）时同样用保守默认集', () => {
    expect(levelsFor(model({ supported_reasoning_levels: undefined }))).toEqual([OFF, ...DEFAULT_LEVELS])
  })

  it('模型信息未知时也不给空集合（否则用户无法关闭思考）', () => {
    expect(levelsFor(undefined)).toEqual([OFF, ...DEFAULT_LEVELS])
    expect(levelsFor(null)).toEqual([OFF, ...DEFAULT_LEVELS])
  })

  it('去重、丢弃后端给的非等级值', () => {
    expect(levelsFor(model({ supported_reasoning_levels: ['low', 'low', 'off', '', 42] }))).toEqual([
      OFF,
      'low',
    ])
  })

  it('非数组的等级字段不会炸', () => {
    expect(levelsFor(model({ supported_reasoning_levels: 'high' }))).toEqual([OFF, ...DEFAULT_LEVELS])
  })

  it('只说「只能关闭」时只给关闭，不回退默认集', () => {
    // 网关用关闭别名表达「这个模型没有可选等级」。若归一化后当成「没枚举」而回退
    // DEFAULT_LEVELS，用户就能选到网关没声明的等级——正是为避免「选了却请求失败」
    // 而写这套规则的初衷。
    expect(levelsFor(model({ supported_reasoning_levels: ['off'] }))).toEqual([OFF])
    expect(levelsFor(model({ supported_reasoning_levels: ['none', 'off'] }))).toEqual([OFF])
  })
})

describe('shouldShowEffortSelector', () => {
  it('不支持思考的模型不展示选择器', () => {
    expect(shouldShowEffortSelector(model({ reasoning: false }))).toBe(false)
  })

  it('支持思考或信息未知时展示', () => {
    expect(shouldShowEffortSelector(model({ reasoning: true }))).toBe(true)
    expect(shouldShowEffortSelector(undefined)).toBe(true)
  })
})

describe('reconcileEffort', () => {
  it('当前等级被支持时原样保留', () => {
    expect(reconcileEffort('high', model())).toBe('high')
  })

  it('用户主动关闭思考的意图不被模型切换抹掉', () => {
    expect(reconcileEffort(OFF, model())).toBe(OFF)
    expect(reconcileEffort('none', model())).toBe(OFF)
  })

  it('当前等级不被支持时回退到 medium', () => {
    expect(reconcileEffort('xhigh', model({ supported_reasoning_levels: ['low', 'medium'] }))).toBe(
      'medium',
    )
  })

  it('没有 medium 时取第一个非关闭等级', () => {
    expect(reconcileEffort('xhigh', model({ supported_reasoning_levels: ['low', 'high'] }))).toBe('low')
  })

  it('绝不返回模型未声明的等级', () => {
    const target = model({ supported_reasoning_levels: ['low'] })
    expect(reconcileEffort('high', target)).toBe('low')
    expect(levelsFor(target)).toContain(reconcileEffort('high', target))
  })

  it('模型不支持思考时解析为关闭', () => {
    // 否则会把 reasoning_effort 发给一个不支持的模型，正是 model_service
    // 注释里担心的「用户选择后请求失败」
    expect(reconcileEffort('medium', model({ reasoning: false }))).toBe(OFF)
  })

  it('空值也能解析出一个合法等级', () => {
    expect(reconcileEffort(null, model())).toBe('medium')
    expect(reconcileEffort('', model())).toBe('medium')
  })

  it('只能关闭的模型上，任何输入都收敛到关闭', () => {
    const onlyOff = model({ supported_reasoning_levels: ['none'] })

    expect(reconcileEffort('high', onlyOff)).toBe(OFF)
    expect(reconcileEffort(undefined, onlyOff)).toBe(OFF)
  })
})

describe('默认等级只定义一处', () => {
  it('会话默认等级与回退等级是同一个值', () => {
    // 两处各写一遍 'medium' 时改一处不会有任何报错，只能靠这条用例兜住
    expect(DEFAULT_REASONING_EFFORT).toBe(PREFERRED_LEVEL)
    expect(reconcileEffort(null, null)).toBe(DEFAULT_REASONING_EFFORT)
  })
})
