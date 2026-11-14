// Prompt preview parity (handoffs spec §10): the review sheet's preview is built by the same rules
// as the server's first-turn message. The fixture is generated from the Python
// (`apis.shared.handoff.context`) and checked on that side too, so a change on either side fails
// one of the two tests until the fixture is regenerated and both agree.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fromProjectRoot } from '../../../../testing/project-root';
import { DeliveryArtifact, FileLine } from './handoff.models';
import {
  buildHandoffMessage,
  extractDeliveryBlocks,
  extractHandoffContext,
  formatFileSize,
  joinDeliveryBlocks,
  referenceUploadId,
  renderDeliveryBlock,
  wrapHandoffContext,
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
      expect(renderDeliveryBlock(c.text, c.fromTitle, c.artifacts, c.files ?? [])).toBe(c.block);
    });
  }

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
