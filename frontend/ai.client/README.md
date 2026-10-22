# AiClient

This project was generated using [Angular CLI](https://github.com/angular/angular-cli) version 21.0.1.

## Development server

To start a local development server, run:

```bash
ng serve
```

Once the server is running, open your browser and navigate to `http://localhost:4200/`. The application will automatically reload whenever you modify any of the source files.

## Code scaffolding

Angular CLI includes powerful code scaffolding tools. To generate a new component, run:

```bash
ng generate component component-name
```

For a complete list of available schematics (such as `components`, `directives`, or `pipes`), run:

```bash
ng generate --help
```

## Building

To build the project run:

```bash
ng build
```

This will compile your project and store the build artifacts in the `dist/` directory. By default, the production build optimizes your application for performance and speed.

### Initial-bundle budget

The production build enforces an `initial` budget in `angular.json`: **1.6 MB warning,
2 MB error**, measured on _raw_ (pre-compression) bytes. Angular budgets have no
transfer-size mode, so raw size is the only lever — treat it as a proxy and sanity-check
the "Estimated transfer size" column when a change moves the number.

Two things to know before you widen it:

- **The `scripts` array is invisible to lazy loading.** Entries under
  `architect.build.options.scripts` are emitted as a plain `<script>` tag on
  `index.html`, outside the module graph. Route-level lazy loading and `@defer` cannot
  reach them, and a bundle analyzer of the module graph will not show them. Mermaid sat
  there for months at 3.57 MB raw — 71% of the entire initial bundle, downloaded by every
  visitor — while the budget sat at 5 MB and quietly absorbed it. It is now dynamically
  imported (see `src/app/shared/utils/lazy-mermaid.ts`).
- **Prefer a lazy chunk to a bigger number.** Heavy renderers are pulled in with a dynamic
  `import()` so they land in their own chunk and are fetched only when the feature is used.
  The budget is deliberately close to the real footprint so the next multi-megabyte
  dependency shows up as a warning on the PR that adds it, not a year later.

KaTeX (~270 kB) and Prism (~82 kB) are knowingly left eager: ngx-markdown calls them
synchronously during the same render pass that inserts the HTML, so deferring them would
flash raw `$…$` and unhighlighted code on nearly every assistant message. Mermaid was
different — its `run()` is reached only when the rendered DOM already contains a
`.mermaid` element.

To see what is actually in the initial bundle:

```bash
npm run build -- --stats-json
```

then inspect `dist/ai.client/stats.json` (an esbuild metafile: `outputs[<chunk>].inputs`
attributes every byte to a source module).

## Running unit tests

To execute unit tests with the [Vitest](https://vitest.dev/) test runner, use the following command:

```bash
ng test
```

## Running end-to-end tests

For end-to-end (e2e) testing, run:

```bash
ng e2e
```

Angular CLI does not come with an end-to-end testing framework by default. You can choose one that suits your needs.

## Additional Resources

For more information on using the Angular CLI, including detailed command references, visit the [Angular CLI Overview and Command Reference](https://angular.dev/tools/cli) page.
