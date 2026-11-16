import { describe, expect, it } from 'vitest'
import { percent, tokenCount } from './context'

describe('context numbers', () => {
  it('formats thousands, millions, billions and larger values', () => {
    expect(tokenCount(999)).toBe('999')
    expect(tokenCount(1_200)).toBe('1.2K')
    expect(tokenCount(1_234_567)).toBe('1.23M')
    expect(tokenCount(2_500_000_000)).toBe('2.5B')
    expect(tokenCount(1_000_000_000_000)).toBe('1T')
    expect(tokenCount(999_999)).toBe('1M')
  })
  it('reports small and over-limit percentages without hiding overflow', () => {
    expect(percent(1, 1_000_000)).toBe('<0.1%')
    expect(percent(150, 100)).toBe('150%')
    expect(percent(0, 0)).toBe('0%')
  })
})
