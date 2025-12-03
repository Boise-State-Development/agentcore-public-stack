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

    // Apply syntax highlighting
    const highlighted = escaped
      // Highlight keys (property names)
      .replace(
        /"([^"]+)"(?=\s*:)/g,
        '<span class="json-key">"$1"</span>'
      )
      // Highlight string values
      .replace(
        /:\s*"([^"]*)"/g,
        ': <span class="json-string">"$1"</span>'
      )
      // Highlight numbers
      .replace(
        /:\s*(\d+\.?\d*)/g,
        ': <span class="json-number">$1</span>'
      )
      // Highlight booleans
      .replace(
        /:\s*(true|false)/g,
        ': <span class="json-boolean">$1</span>'
      )
      // Highlight null
      .replace(
        /:\s*(null)/g,
        ': <span class="json-null">$1</span>'
      )
      // Highlight brackets
      .replace(
        /([{}\[\]])/g,
        '<span class="json-bracket">$1</span>'
      );

    return this.sanitizer.bypassSecurityTrustHtml(highlighted);
  }
}
