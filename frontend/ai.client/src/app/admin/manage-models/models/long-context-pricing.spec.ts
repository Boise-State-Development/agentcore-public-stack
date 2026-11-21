import {
  CURATED_BEDROCK_MODELS,
  CURATED_BEDROCK_RESPONSES_MODELS,
  CURATED_MANTLE_MODELS,
  CuratedModel,
} from './curated-models';

/**
 * A curated row's long-context fields are what cost metering multiplies a call
 * by once its prompt passes the line (`CostCalculator.rates_for_call`), so a
 * wrong or missing value under- or over-charges every long turn silently.
 * Verified 2026-11-20 against the Price List API's `*_long_ctx_*` usage types
 * and, for Haiku 5.5, the account's Cost Explorer.
 */
const ALL: CuratedModel[] = [
  ...CURATED_BEDROCK_MODELS,
  ...CURATED_BEDROCK_RESPONSES_MODELS,
  ...CURATED_MANTLE_MODELS,
];

function byModelId(prefix: string): CuratedModel[] {
  return ALL.filter((m) => m.template.modelId.includes(prefix));
}

describe('curated long-context pricing', () => {
  it('meters Claude Haiku 5.5 at 5x on every bucket above 100,000 prompt tokens', () => {
    const [haiku] = byModelId('claude-haiku-5-5');
    expect(haiku.template.longContextThresholdTokens).toBe(100_000);
    expect(haiku.template.longContextInputMultiplier).toBe(5);
    expect(haiku.template.longContextOutputMultiplier).toBe(5);
  });

  it('meters every hosted OpenAI row at 2x input and 1.5x output above 272K', () => {
    const openai = byModelId('openai.gpt-');
    expect(openai.length).toBeGreaterThan(0);
    for (const row of openai) {
      expect(row.template.longContextThresholdTokens, row.key).toBe(272_000);
      expect(row.template.longContextInputMultiplier, row.key).toBe(2);
      expect(row.template.longContextOutputMultiplier, row.key).toBe(1.5);
    }
  });

  it('gives no other row a long-context card', () => {
    const withRow = ALL.filter((m) => m.template.longContextThresholdTokens);
    for (const row of withRow) {
      const id = row.template.modelId;
      expect(id.includes('claude-haiku-5-5') || id.includes('openai.gpt-'), id).toBe(true);
    }
  });

  it('keeps compaction at or under the pricing line, so a turn rarely reaches it', () => {
    for (const row of ALL.filter((m) => m.template.longContextThresholdTokens)) {
      expect(row.template.maxInputTokens, row.key).toBeLessThanOrEqual(
        row.template.longContextThresholdTokens!,
      );
    }
  });
});
