import type { ProviderOutage } from './chat-state.service';
import { Message, isTextContentBlock } from '../models/message.model';

/** What a provider-outage reply needs from a `stream_error` event. */
interface StreamErrorLike {
  code: string;
  metadata?: Record<string, unknown>;
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

/**
 * The provider outage a `stream_error` reports, or null when it reports
 * something else. The backend sends `code: 'provider_unavailable'` with
 * `error_kind` of the same name on `metadata`, plus the model when it knows it.
 */
export function providerOutageFrom(event: StreamErrorLike): ProviderOutage | null {
  const metadata = event.metadata ?? {};
  if (event.code !== 'provider_unavailable' && metadata['error_kind'] !== 'provider_unavailable') {
    return null;
  }
  return {
    modelId: asText(metadata['model_id']),
    modelName: asText(metadata['model_name']),
    provider: asText(metadata['provider']),
  };
}

/** Providers that run on Amazon Bedrock, whose outage is AWS's. */
const AWS_PROVIDERS = new Set(['bedrock', 'bedrock-responses', 'mantle']);

/** Who the fault belongs to, for the label beside the buttons. */
export function outageLabel(provider: string | null): string {
  const value = (provider ?? '').toLowerCase();
  if (AWS_PROVIDERS.has(value)) return 'AWS service issue';
  if (value === 'openai') return 'OpenAI service issue';
  if (value === 'gemini') return 'Google service issue';
  return 'Model provider issue';
}

/** What the retry row under a failed reply shows. */
export interface OutageOffer {
  /** Whose fault it is, e.g. "AWS service issue". */
  label: string;
  /** The model that failed, for the button's accessible name. */
  modelName: string | null;
}

/**
 * The words of the conversation's last user message, as the user typed them:
 * `displayText` when the turn carried a modified prompt, else its text blocks.
 */
export function lastUserText(messages: readonly Message[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role !== 'user') continue;
    const display = message.metadata?.['displayText'];
    if (typeof display === 'string' && display.trim()) return display;
    const text = message.content
      .filter(isTextContentBlock)
      .map((block) => block.text)
      .join('\n\n')
      .trim();
    return text || null;
  }
  return null;
}
