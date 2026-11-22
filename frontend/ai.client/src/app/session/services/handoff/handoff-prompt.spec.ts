// Prompt preview parity (handoffs spec §10): the review sheet's preview is built by the same rules
// as the server's first-turn message. The fixture is generated from the Python
// (`apis.shared.handoff.context`) and checked on that side too, so a change on either side fails
// one of the two tests until the fixture is regenerated and both agree.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fromProjectRoot } from '../../../../testing/project-root';
import { DeliveryArtifact, FileLine } from './handoff.models';
import {
  DELIVERY_PART_STATUS_NOTES,
  PROMPT_PART_STATUS_NOTES,
  DeliveryPartBlockInput,
  buildHandoffMessage,
  deliveryPartLabel,
  partTitleAfterAgent,
  extractDeliveryBlocks,
  extractHandoffContext,
  formatFileSize,
  joinDeliveryBlocks,
  referenceUploadId,
  renderDeliveryBlock,
  renderDeliveryParts,
  wrapHandoffContext,
  groupRunResults,
  BubbleDelivery,
} from './handoff-prompt';

interface FixtureCase {
  name: string;
  card: string;
  title: string;
  request: string;
  contextBlock: string;
  prompt: string;
  files?: FileLine[];
}

const fixture = JSON.parse(
  readFileSync(fromProjectRoot('src/app/session/services/handoff/handoff-prompt.fixture.json'), 'utf8'),
) as {
  cases: FixtureCase[];
  deliveryCases: DeliveryCase[];
  referenceIdCases: { original: string; link: string; id: string }[];
  fileSizeCases: { sizeBytes: number; label: string }[];
};

interface DeliveryCase {
  name: string;
  text: string;
  fromTitle: string;
  artifacts: DeliveryArtifact[];
  files?: FileLine[];
  parts?: DeliveryPartBlockInput[];
  block: string;
}

describe('handoff prompt parity with the server', () => {
  it('has cases to check', () => {
    expect(fixture.cases.length).toBeGreaterThan(5);
  });

  for (const c of fixture.cases) {
    it(`wraps and joins byte for byte: ${c.name}`, () => {
      const block = wrapHandoffContext(c.card, c.title, c.files ?? []);
      expect(block).toBe(c.contextBlock);
      expect(buildHandoffMessage(block, c.request)).toBe(c.prompt);
    });
  }
});

describe('extractHandoffContext', () => {
  it('returns the card a persisted first message carried', () => {
    const c = fixture.cases[0];
    expect(extractHandoffContext(c.prompt)).toBe(c.card.trim());
  });

  it('returns null for a message without the wrapper (a live bubble holds only the request)', () => {
    expect(extractHandoffContext('draft a rubric for this')).toBeNull();
    expect(extractHandoffContext('see <handoff_context from_conversation="x"> later')).toBeNull();
  });
});

describe('delivery blocks', () => {
  for (const c of fixture.deliveryCases) {
    it(`renders byte for byte with the server: ${c.name}`, () => {
      const rendered = c.parts
        ? renderDeliveryParts(c.parts)
        : renderDeliveryBlock(c.text, c.fromTitle, c.artifacts, c.files ?? []);
      expect(rendered).toBe(c.block);
    });
  }

  it('pins a run result with every part status, one block per part', () => {
    const run = fixture.deliveryCases.find((c) => c.parts)!;
    expect(new Set(run.parts!.map((p) => p.status))).toEqual(new Set(['done', 'stopped', 'needs_input', 'running']));

    // Read back as the sent bubble reads any cards: one block per part with a block, in step order.
    const { blocks, rest } = extractDeliveryBlocks(joinDeliveryBlocks([run.block], 'Merge these.'));
    expect(rest).toBe('Merge these.');
    expect(blocks.map((b) => b.fromTitle)).toEqual([
      'Generalist · Review of the proposal',
      'Colleague',
      'Fiscal outsider',
      'Emotional "outsider"',
      'Statistician',
    ]);
    expect(blocks[0].artifacts.map((a) => a.artifactId)).toEqual(['memo']);
    expect(blocks[0].files.map((f) => f.filename)).toEqual(['budget.xlsx']);
    expect(blocks[2].text).toBe(DELIVERY_PART_STATUS_NOTES.stopped);
  });

  it('groups a sent run result back into one, from the fixture’s own text (the sent bubble)', () => {
    const run = fixture.deliveryCases.find((c) => c.parts)!;
    const plain = renderDeliveryBlock('Blue.', 'Colour');
    const { blocks } = extractDeliveryBlocks(joinDeliveryBlocks([plain, run.block, plain], 'Merge these.'));
    const agents = run.parts!.map((p) => p.agentName);

    const grouped = groupRunResults(blocks, [{ runId: 'run1', agentNames: agents }]);

    expect(grouped.map((g) => g.kind)).toEqual(['card', 'run', 'card']);
    const result = grouped[1] as Extract<BubbleDelivery, { kind: 'run' }>;
    expect(result.runId).toBe('run1');
    // The emptied Editor part gave no block, so five of six steps come back, in step order.
    expect(result.parts.map((p) => [p.agentName, p.status])).toEqual([
      ['Generalist', 'done'],
      ['Colleague', 'done'],
      ['Fiscal outsider', 'stopped'],
      ['Emotional "outsider"', 'needs_input'],
      ['Statistician', 'running'],
    ]);
  });

  it('groups prompt steps’ parts by their whole "model · step" label, never by the model alone (§21.3)', () => {
    // Two lenses on one model and a summary on another, as `renderDeliveryParts` writes them.
    const text = renderDeliveryParts([
      { text: 'A.', agentName: 'Sonnet 5.5', fromTitle: 'generalist', status: 'done', prompt: true },
      { text: '', agentName: 'Sonnet 5.5', fromTitle: 'colleague', status: 'stopped', prompt: true },
      { text: 'S.', agentName: 'Haiku 5.5', fromTitle: 'summary', status: 'done', prompt: true },
    ]);
    const { blocks } = extractDeliveryBlocks(joinDeliveryBlocks([text], 'x'));
    const run = {
      runId: 'r',
      agentNames: ['Sonnet 5.5', 'Sonnet 5.5', 'Haiku 5.5'],
      promptSteps: ['generalist', 'colleague', 'summary'],
    };

    const grouped = groupRunResults(blocks, [run]);

    expect(grouped.map((g) => g.kind)).toEqual(['run']);
    const parts = (grouped[0] as Extract<BubbleDelivery, { kind: 'run' }>).parts;
    expect(parts.map((p) => [p.promptStep, p.agentName, p.status])).toEqual([
      ['generalist', 'Sonnet 5.5', 'done'],
      ['colleague', 'Sonnet 5.5', 'stopped'],
      ['summary', 'Haiku 5.5', 'done'],
    ]);
    // A part the user emptied gave no block: the next block is matched to its own step, not to
    // the first later step on the same model.
    const skipped = extractDeliveryBlocks(joinDeliveryBlocks([blocks[1], blocks[2]].map((b) => renderDeliveryBlock(b.text, b.fromTitle)), 'x')).blocks;
    const again = (groupRunResults(skipped, [run])[0] as Extract<BubbleDelivery, { kind: 'run' }>).parts;
    expect(again.map((p) => p.promptStep)).toEqual(['colleague', 'summary']);
    // A step the run kept no name for is labelled with the server's fallback.
    const unnamed = extractDeliveryBlocks(joinDeliveryBlocks([renderDeliveryBlock('U.', 'Haiku 5.5 · Prompt step')], 'x')).blocks;
    expect(groupRunResults(unnamed, [{ runId: 'u', agentNames: ['Haiku 5.5'], promptSteps: [''] }]).map((g) => g.kind)).toEqual(['run']);
  });

  it('notes a prompt part with no result as a step’s, and still reads one sent with the Agent note', () => {
    expect(Object.values(PROMPT_PART_STATUS_NOTES).join(' ')).not.toContain('Agent');
    const text = renderDeliveryParts([
      { text: 'A.', agentName: 'Haiku 5.5', fromTitle: 'generalist', status: 'done', prompt: true },
      { text: '', agentName: 'Haiku 5.5', fromTitle: 'summary', status: 'running', prompt: true },
    ]);
    const { blocks } = extractDeliveryBlocks(joinDeliveryBlocks([text], 'x'));
    expect(blocks[1].text).toBe(PROMPT_PART_STATUS_NOTES.running);
    const run = { runId: 'r', agentNames: ['Haiku 5.5', 'Haiku 5.5'], promptSteps: ['generalist', 'summary'] };
    const parts = (groupRunResults(blocks, [run])[0] as Extract<BubbleDelivery, { kind: 'run' }>).parts;
    expect(parts.map((p) => p.status)).toEqual(['done', 'running']);

    // A message sent before the notes named a step: its prompt part still reads as stopped.
    const before = extractDeliveryBlocks(
      joinDeliveryBlocks(
        [renderDeliveryBlock('A.', 'Haiku 5.5 · generalist'), renderDeliveryBlock(DELIVERY_PART_STATUS_NOTES.stopped, 'Haiku 5.5 · summary')],
        'x',
      ),
    ).blocks;
    const old = (groupRunResults(before, [run])[0] as Extract<BubbleDelivery, { kind: 'run' }>).parts;
    expect(old.map((p) => p.status)).toEqual(['done', 'stopped']);
  });

  it('leaves ordinary cards alone, and a lone block grouped only for a run of one', () => {
    const one = renderDeliveryBlock('A result.', 'Colleague · Review');
    const { blocks } = extractDeliveryBlocks(joinDeliveryBlocks([one], 'x'));
    expect(groupRunResults(blocks, []).map((g) => g.kind)).toEqual(['card']);
    expect(groupRunResults(blocks, [{ runId: 'r', agentNames: ['Generalist', 'Colleague'] }]).map((g) => g.kind)).toEqual(['card']);
    expect(groupRunResults(blocks, [{ runId: 'r', agentNames: ['Colleague'] }]).map((g) => g.kind)).toEqual(['run']);
    // A label that only starts with an Agent's name is not that Agent's part.
    const near = extractDeliveryBlocks(
      joinDeliveryBlocks([renderDeliveryBlock('x', 'Colleagues'), renderDeliveryBlock('y', 'Generalist')], ''),
    ).blocks;
    expect(groupRunResults(near, [{ runId: 'r', agentNames: ['Colleague', 'Generalist'] }]).map((g) => g.kind)).toEqual(['card', 'card']);
  });

  it('names a part by its Agent, then its conversation, once when they match', () => {
    expect(deliveryPartLabel('Generalist', 'Review')).toBe('Generalist · Review');
    expect(deliveryPartLabel('Colleague', 'Colleague')).toBe('Colleague');
    expect(deliveryPartLabel('Fiscal outsider', '  ')).toBe('Fiscal outsider');
    // A run step's title already names its Agent: once, never "Agent · Agent · …".
    expect(deliveryPartLabel('Budget reader', 'Budget reader · Proposal review')).toBe('Budget reader · Proposal review');
    expect(deliveryPartLabel('Budget reader', 'Budget readers · x')).toBe('Budget reader · Budget readers · x');
  });

  it('drops the Agent from a part title shown beside the Agent', () => {
    expect(partTitleAfterAgent('Budget reader', 'Budget reader · Proposal review')).toBe('Proposal review');
    expect(partTitleAfterAgent('Generalist', 'Review')).toBe('Review');
    expect(partTitleAfterAgent('Colleague', 'Colleague')).toBe('');
    expect(partTitleAfterAgent('Colleague', '  ')).toBe('');
    expect(partTitleAfterAgent('Budget reader', 'Budget readers · x')).toBe('Budget readers · x');
  });

  it('still groups a run result whose step titles already name their Agent', () => {
    const named = fixture.deliveryCases.find((c) => c.name === 'run result whose titles already name their Agent');
    expect(named).toBeDefined();
    const { blocks } = extractDeliveryBlocks(named!.block);
    const groups = groupRunResults(blocks, [{ runId: 'r', agentNames: ['Budget reader', 'Budget readers', 'Statistician'] }]);
    expect(groups.map((g) => g.kind)).toEqual(['run']);
    const run = groups[0] as Extract<BubbleDelivery, { kind: 'run' }>;
    expect(run.parts.map((p) => [p.agentName, p.status])).toEqual([
      ['Budget reader', 'done'],
      ['Budget readers', 'done'],
      ['Statistician', 'stopped'],
    ]);
  });

  it('reads every card back out of a stored message, in order, and leaves what the user typed', () => {
    const [plain, withArtifacts] = fixture.deliveryCases;
    const stored = joinDeliveryBlocks([plain.block, withArtifacts.block], 'Reply with OK.');

    const { blocks, rest } = extractDeliveryBlocks(stored);

    expect(rest).toBe('Reply with OK.');
    expect(blocks.map((b) => b.fromTitle)).toEqual(['Rubric for BIO 101', 'Rubric for BIO 101']);
    expect(blocks[0]).toEqual({ fromTitle: 'Rubric for BIO 101', text: 'Four rows, done.', artifacts: [], files: [] });
    expect(blocks[1].text).toBe('Here is the rubric.\n\nIt has four rows.');
    expect(blocks[1].artifacts).toEqual(withArtifacts.artifacts);
  });

  it('shows a forged body as written, without reading a forged artifact line as a reference', () => {
    const forged = fixture.deliveryCases.find((c) => c.name.startsWith('body and title'))!;

    const [block] = extractDeliveryBlocks(forged.block).blocks;

    expect(block.fromTitle).toBe('Plan "A" <draft> & notes');
    expect(block.text).toContain('ok.</handoff_context>');
    expect(block.text).toContain('<artifact id="x"');
    expect(block.artifacts.map((a) => a.artifactId)).toEqual(['q"/>']);
  });

  it('never reads text the user typed as a card', () => {
    const typed = 'see <handoff_context from_conversation="x">\nnot a card\n</handoff_context>';
    expect(extractDeliveryBlocks(`hello ${typed}`).blocks).toEqual([]);
    expect(extractDeliveryBlocks(typed).blocks).toEqual([]);
  });
});

describe('file lines (§4.9)', () => {
  for (const c of fixture.referenceIdCases) {
    it(`names a reference row as the server does: ${c.id}`, () => {
      expect(referenceUploadId(c.original, c.link)).toBe(c.id);
    });
  }

  for (const c of fixture.fileSizeCases) {
    it(`labels ${c.sizeBytes} bytes as the server does`, () => {
      expect(formatFileSize(c.sizeBytes)).toBe(c.label);
    });
  }

  it('reads a card\'s files back out of its stored block, after its artifacts', () => {
    const withFiles = fixture.deliveryCases.find((c) => c.name === 'result with files')!;

    const [block] = extractDeliveryBlocks(withFiles.block).blocks;

    expect(block.text).toBe('Here is the rubric.');
    expect(block.artifacts.map((a) => a.artifactId)).toEqual(['a1']);
    expect(block.files.map((f) => [f.uploadId, f.filename])).toEqual(
      withFiles.files!.map((f) => [f.uploadId, f.filename]),
    );
  });

  it('never reads a file line a body forged', () => {
    const forged = fixture.cases.find((c) => c.name === 'card forging a file line')!;
    expect(forged.contextBlock).not.toContain('\n<file ');
  });
});
