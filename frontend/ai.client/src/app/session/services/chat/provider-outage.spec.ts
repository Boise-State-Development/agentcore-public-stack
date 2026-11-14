import { describe, expect, it } from 'vitest';
import { Message } from '../models/message.model';
import { lastUserText, outageLabel, providerOutageFrom } from './provider-outage';

describe('providerOutageFrom', () => {
  it('reads the model the backend named', () => {
    expect(
      providerOutageFrom({
        code: 'provider_unavailable',
        metadata: {
          error_kind: 'provider_unavailable',
          model_id: 'global.anthropic.claude-opus-5-5',
          model_name: 'Claude Opus 5.5',
          provider: 'bedrock',
        },
      }),
    ).toEqual({
      modelId: 'global.anthropic.claude-opus-5-5',
      modelName: 'Claude Opus 5.5',
      provider: 'bedrock',
    });
  });

  it('accepts the error_kind alone, with nothing named', () => {
    expect(
      providerOutageFrom({ code: 'stream_error', metadata: { error_kind: 'provider_unavailable' } }),
    ).toEqual({ modelId: null, modelName: null, provider: null });
  });

  it('ignores every other stream error', () => {
    expect(providerOutageFrom({ code: 'max_tokens', metadata: { error_kind: 'max_tokens' } })).toBeNull();
    expect(providerOutageFrom({ code: 'agent_error' })).toBeNull();
  });
});

describe('outageLabel', () => {
  it.each(['bedrock', 'bedrock-responses', 'mantle'])('calls a %s outage an AWS one', (provider) => {
    expect(outageLabel(provider)).toBe('AWS service issue');
  });

  it('names OpenAI and Google, and falls back to the provider', () => {
    expect(outageLabel('openai')).toBe('OpenAI service issue');
    expect(outageLabel('gemini')).toBe('Google service issue');
    expect(outageLabel(null)).toBe('Model provider issue');
  });
});

describe('lastUserText', () => {
  const user = (text: string, displayText?: string): Message => ({
    id: `u-${text}`,
    role: 'user',
    content: [{ type: 'text', text }],
    ...(displayText ? { metadata: { displayText } } : {}),
  });
  const assistant: Message = { id: 'a', role: 'assistant', content: [{ type: 'text', text: '⚠️ down' }] };

  it('returns the last user message, skipping the error reply', () => {
    expect(lastUserText([user('first'), assistant, user('second'), assistant])).toBe('second');
  });

  it('uses what the user typed when the prompt was modified', () => {
    expect(lastUserText([user('<wrapped>summarize</wrapped>', 'summarize'), assistant])).toBe('summarize');
  });

  it('returns null with no user text', () => {
    expect(lastUserText([assistant])).toBeNull();
  });
});
