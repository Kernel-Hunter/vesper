import { describe, expect, it } from 'vitest'
import { filterModels, formatContext, formatCost, formatPrice, type ModelInfo } from './catalog'

const model = (over: Partial<ModelInfo>): ModelInfo => ({
  id: 'x', name: '', context: null, tools: null, input: null, output: null, free: null, price_source: null, ...over,
})

describe('model catalog helpers', () => {
  it('formats prices per million tokens and says when a model is free', () => {
    expect(formatPrice(model({ input: 0.15, output: 0.6, free: false, price_source: 'provider' }))).toBe('$0.15 / $0.6')
    expect(formatPrice(model({ input: 3, output: 15, free: false }))).toBe('$3 / $15')
    expect(formatPrice(model({ input: 0, output: 0, free: true, price_source: 'provider' }))).toBe('Free')
    expect(formatPrice(model({ input: 0, output: 0, free: true, price_source: 'local' }))).toBe('Local')
    expect(formatPrice(model({}))).toBe('')
  })

  it('shows costs with enough digits for small amounts', () => {
    expect(formatCost(0)).toBe('$0')
    expect(formatCost(0.00042)).toBe('$0.0004')
    expect(formatCost(0.0123)).toBe('$0.012')
    expect(formatCost(1.234)).toBe('$1.23')
  })

  it('shortens context sizes', () => {
    expect(formatContext(128000)).toBe('128k')
    expect(formatContext(1048576)).toBe('1M')
    expect(formatContext(2000000)).toBe('2M')
    expect(formatContext(null)).toBe('')
  })

  it('filters by words, free models and tool support', () => {
    const list = [
      model({ id: 'meta-llama/llama-3.3-70b-instruct:free', name: 'Llama 3.3 70B (free)', free: true, tools: true }),
      model({ id: 'openai/gpt-4.1', name: 'OpenAI: GPT-4.1', free: false, tools: true }),
      model({ id: 'some/model', name: 'No tools', free: true, tools: false }),
    ]
    expect(filterModels(list, 'llama 70b', 'all').map(m => m.id)).toEqual(['meta-llama/llama-3.3-70b-instruct:free'])
    expect(filterModels(list, 'GPT', 'all')).toHaveLength(1)
    expect(filterModels(list, '', 'free')).toHaveLength(2)
    expect(filterModels(list, '', 'tools').map(m => m.id)).toEqual(['meta-llama/llama-3.3-70b-instruct:free', 'openai/gpt-4.1'])
  })
})
