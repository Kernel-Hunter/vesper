export type EffortSupport = 'supported' | 'unavailable' | 'unknown'

/** Conservative capability labels for the controls Vesper actually sends, not all model capabilities. */
export function effortSupport(provider: string, model: string): EffortSupport {
  const id = model.toLowerCase()
  if (provider === 'openai') {
    if (/^gpt-5(?:-(?:mini|nano))?(?:-\d{4}-\d{2}-\d{2})?$/.test(id) ||
        /^gpt-5\.(?:1|2|4)(?:-\d{4}-\d{2}-\d{2})?$/.test(id) ||
        /^gpt-5\.4-mini(?:-\d{4}-\d{2}-\d{2})?$/.test(id) || id === 'gpt-5.3-codex' ||
        /^(?:o3|o3-mini|o4-mini)(?:-\d{4}-\d{2}-\d{2})?$/.test(id)) return 'supported'
    if (/^gpt-4(?:\.1|o)(?:-|$)/.test(id)) return 'unavailable'
  }
  if (provider === 'anthropic') {
    if (/^claude-(?:opus|sonnet)-4-6(?:-\d{8})?$/.test(id) ||
        /^claude-(?:opus|sonnet|haiku)-5-5(?:-\d{8})?$/.test(id)) return 'supported'
    // Vesper currently uses the same fixed thinking budget for all three levels on older Claude models.
    if (/^claude-(?:3|haiku-4|sonnet-4-[05]|opus-4-[015])(?:-|$)/.test(id)) return 'unavailable'
  }
  // Ollama's adapter currently enables reasoning but does not send an effort level.
  if (provider === 'ollama') return 'unavailable'
  return 'unknown'
}

export const effortLabel: Record<EffortSupport, string> = {
  supported: 'Effort supported',
  unavailable: 'No adjustable effort',
  unknown: 'Effort unknown',
}
