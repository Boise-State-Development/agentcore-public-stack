import { Pipe, PipeTransform } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

@Pipe({
  name: 'jsonSyntaxHighlight',
})
export class JsonSyntaxHighlightPipe implements PipeTransform {
  constructor(private sanitizer: DomSanitizer) {}

  transform(json: string): SafeHtml {
    if (!json) {
      return '';
    }

    // Escape HTML entities
    const escaped = json
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // One pass over the whole document, so each token is classified exactly
    // once. Chained per-kind replaces re-scanned text already inside a string
    // and highlighted it again — a timestamp value like "09:30:17" came out as
    // "09: 30: 17", because the number rule matched the `:30` inside it.
    const highlighted = escaped.replace(
      /("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}[\]]/g,
      (match: string, str?: string, colon?: string) => {
        if (str) {
          return colon
            ? `<span class="json-key">${str}</span>${colon}`
            : `<span class="json-string">${str}</span>`;
        }
        if (match === 'true' || match === 'false') {
          return `<span class="json-boolean">${match}</span>`;
        }
        if (match === 'null') return `<span class="json-null">${match}</span>`;
        if (/[{}[\]]/.test(match)) return `<span class="json-bracket">${match}</span>`;
        return `<span class="json-number">${match}</span>`;
      },
    );

    return this.sanitizer.bypassSecurityTrustHtml(highlighted);
  }
}
