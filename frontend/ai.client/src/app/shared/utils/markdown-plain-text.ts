import { Lexer, type Token } from 'marked';

/**
 * The readable text of a markdown snippet, one string per block (heading,
 * paragraph, list item, table row, code block), with the syntax gone: link
 * text without its URL, emphasis without its markers, HTML without its tags.
 *
 * For short previews such as a citation excerpt, where the source is indexed
 * markdown (a crawled web page, a Docling chunk) and may be cut off
 * mid-syntax. Uses a fresh lexer, never the app's configured `marked`
 * instance, so renderer extensions (KaTeX, Mermaid) cannot change it. The
 * strings are plain text for interpolation, not HTML.
 */
export function markdownBlocks(markdown: string): string[] {
  if (!markdown?.trim()) return [];
  let tokens: Token[];
  try {
    tokens = Lexer.lex(markdown, { gfm: true });
  } catch {
    return [tidy(markdown)].filter(Boolean);
  }
  const blocks: string[] = [];
  collectBlocks(tokens, blocks);
  return blocks;
}

/** {@link markdownBlocks} joined into one line, blocks separated by " · ". */
export function markdownToPlainText(markdown: string): string {
  return markdownBlocks(markdown).join(BLOCK_SEPARATOR);
}

export const BLOCK_SEPARATOR = ' · ';

function collectBlocks(tokens: Token[], blocks: string[]): void {
  for (const token of tokens) {
    switch (token.type) {
      case 'space':
      case 'hr':
      case 'def':
        break;
      case 'list':
        for (const item of token.items) collectBlocks(item.tokens, blocks);
        break;
      case 'blockquote':
        collectBlocks(token.tokens ?? [], blocks);
        break;
      case 'table': {
        const rows = [token.header, ...token.rows];
        for (const row of rows) {
          push(blocks, row.map((cell: { tokens: Token[] }) => inlineText(cell.tokens)).join(' '));
        }
        break;
      }
      case 'code':
        push(blocks, token.text);
        break;
      case 'html':
        push(blocks, stripTags(token.text));
        break;
      default:
        // heading, paragraph, a tight list item's text, anything else carrying inline tokens
        push(blocks, 'tokens' in token && token.tokens ? inlineText(token.tokens) : tidy(textOf(token)));
    }
  }
}

function inlineText(tokens: Token[]): string {
  let out = '';
  for (const token of tokens) {
    switch (token.type) {
      case 'codespan':
        out += token.text;
        break;
      case 'br':
        out += ' ';
        break;
      case 'html':
        out += stripTags(token.text);
        break;
      case 'image':
        out += token.text ?? '';
        break;
      case 'text':
        out += token.tokens ? inlineText(token.tokens) : tidy(token.text);
        break;
      default:
        out += 'tokens' in token && token.tokens ? inlineText(token.tokens) : tidy(textOf(token));
    }
  }
  return out;
}

function textOf(token: Token): string {
  return 'text' in token && typeof token.text === 'string' ? token.text : '';
}

function push(blocks: string[], text: string): void {
  // A link cut off before its ")" arrives as separate text tokens, so it is
  // only recognisable here, on the whole block. Truncation is always at the end.
  const line = text
    .replace(/\[([^\]]*)\]\s*\([^)]*$/, '$1')
    .replace(/\s+/g, ' ')
    .trim();
  if (line) blocks.push(line);
}

function stripTags(html: string): string {
  return html.replace(/<[^>]*>?/g, ' ');
}

/**
 * Markers the lexer left as literal text, which in an excerpt almost always
 * means the snippet was cut before the syntax closed: `**bold`, a leading `#`.
 */
function tidy(text: string): string {
  return text
    .replace(/\*{1,3}|_{2,3}|~~|`+/g, '')
    .replace(/^#{1,6}\s+/, '');
}
