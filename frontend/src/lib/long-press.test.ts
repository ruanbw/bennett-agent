import { describe, expect, it } from 'vitest';

import {
  LONG_PRESS_MOVE_TOLERANCE_PX,
  isLongPressPointer,
  movedBeyondLongPressTolerance,
} from './long-press';

describe('movedBeyondLongPressTolerance', () => {
  it('原地不动或轻微抖动不算移动', () => {
    expect(movedBeyondLongPressTolerance({ x: 10, y: 10 }, { x: 10, y: 10 })).toBe(false);
    expect(movedBeyondLongPressTolerance({ x: 10, y: 10 }, { x: 14, y: 13 })).toBe(false);
  });

  it('超出容差（含斜向）就算移动', () => {
    expect(
      movedBeyondLongPressTolerance(
        { x: 0, y: 0 },
        { x: LONG_PRESS_MOVE_TOLERANCE_PX + 1, y: 0 },
      ),
    ).toBe(true);
    // 斜向 8px + 8px ≈ 11.3px：单看某一轴会漏掉
    expect(movedBeyondLongPressTolerance({ x: 0, y: 0 }, { x: 8, y: 8 })).toBe(true);
  });

  it('容差可覆盖，便于对精度不同的设备收紧', () => {
    expect(movedBeyondLongPressTolerance({ x: 0, y: 0 }, { x: 5, y: 0 }, 2)).toBe(true);
  });
});

describe('isLongPressPointer', () => {
  it('只有触摸参与长按', () => {
    expect(isLongPressPointer('touch')).toBe(true);
    expect(isLongPressPointer('mouse')).toBe(false);
    expect(isLongPressPointer('pen')).toBe(false);
    expect(isLongPressPointer('')).toBe(false);
  });
});
