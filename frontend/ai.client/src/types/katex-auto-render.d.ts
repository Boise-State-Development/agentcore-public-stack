/**
 * KaTeX ships `contrib/auto-render` as JavaScript with no bundled typings.
 * In the browser it reaches the app as the `renderMathInElement` global via
 * angular.json's `scripts`, so nothing imports it in application code — only
 * the KaTeX render specs, which publish it onto the global scope themselves.
 */
declare module 'katex/contrib/auto-render' {
  import type { KatexOptions } from 'katex';

  export interface AutoRenderDelimiter {
    left: string;
    right: string;
    display: boolean;
  }

  export interface AutoRenderOptions extends KatexOptions {
    delimiters?: AutoRenderDelimiter[];
    ignoredTags?: string[];
    ignoredClasses?: string[];
    preProcess?: (math: string) => string;
  }

  export default function renderMathInElement(
    element: HTMLElement,
    options?: AutoRenderOptions,
  ): void;
}
