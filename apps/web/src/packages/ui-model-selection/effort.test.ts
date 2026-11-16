import { describe, expect, it } from 'vitest'
import { effortSupport } from './effort'

describe('model effort support', () => {
  it('recognizes models whose effort levels Vesper can send', () => {
    expect(effortSupport('openai', 'gpt-5-mini')).toBe('supported')
    expect(effortSupport('openai', 'gpt-5.4-mini-2026-03-17')).toBe('supported')
    expect(effortSupport('openai', 'gpt-5.3-codex')).toBe('supported')
    expect(effortSupport('openai', 'o3-2025-04-16')).toBe('supported')
    expect(effortSupport('anthropic', 'claude-sonnet-4-6')).toBe('supported')
    expect(effortSupport('anthropic', 'claude-haiku-5-5')).toBe('supported')
  })

  it('does not promise adjustable levels for non-reasoning or fixed-budget adapters', () => {
    expect(effortSupport('openai', 'gpt-4.1-mini')).toBe('unavailable')
    expect(effortSupport('anthropic', 'claude-opus-4-5-20251101')).toBe('unavailable')
    expect(effortSupport('ollama', 'gpt-oss:20b')).toBe('unavailable')
  })

  it('marks unknown providers, custom models and unverified variants as unknown', () => {
    expect(effortSupport('openrouter', 'openai/gpt-5')).toBe('unknown')
    expect(effortSupport('openai', 'gpt-5-pro')).toBe('unknown')
    expect(effortSupport('anthropic', 'claude-opus-7')).toBe('unknown')
  })
})
