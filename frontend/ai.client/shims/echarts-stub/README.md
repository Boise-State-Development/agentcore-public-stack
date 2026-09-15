# echarts-stub

A deliberately empty stand-in for [`echarts`](https://echarts.apache.org/),
substituted into `pptx-preview` by declaring it as the project's own
`echarts` dependency:

```json
"dependencies": { "echarts": "file:./shims/echarts-stub" }
```

Its version (`5.6.0`) satisfies the `^5.5.1` that `pptx-preview` asks
for, so npm dedupes both onto this one directory instead of fetching the
real library.

## Why

`pptx-preview` reaches for ECharts in exactly one place: rendering a
*native* OOXML chart part (`ppt/charts/*.xml`) — a chart PowerPoint draws
itself from embedded data, rather than a picture. It does so via
`import * as echarts from 'echarts'`, a static namespace import, so no
amount of tree-shaking will drop it. Depending on `pptx-preview` means
shipping the whole charting library.

Measured against this repo's build, that is the difference between a
**357 kB** and a **~60 kB** gzipped lazy chunk — ECharts is roughly five
sixths of the viewer's weight.

We do not need it. `create_powerpoint_presentation` builds charts by
rendering a matplotlib PNG and calling `add_picture`, which lands in the
deck as an ordinary image. A survey of every `.pptx` in the dev files
store found **zero** native chart parts across four real decks, including
a 4.3 MB branded template deck carrying 15 images.

## What it costs

An *uploaded* deck that does contain a native chart throws instead of
drawing it, and `PptxViewerComponent` reports the file as unreadable
rather than silently dropping a slide's centrepiece. That is the whole
trade.

## Why a dependency, and not `tsconfig` `paths`

The import lives inside `node_modules/pptx-preview/dist/pptx-preview.es.js`,
a pre-built JavaScript file. `paths` governs TypeScript's resolution of
*our own sources*; the bundler resolves a dependency's own imports with
the node resolver and walks straight past it. This was tried first and
the built chunk still contained the whole of ECharts.

Substituting the package is the only mechanism that applies at the point
the import is resolved. It has to be a **top-level dependency** rather
than a `file:` spec nested under `overrides`: npm resolves the latter
relative to wherever it happens to place the package, so the symlink
lands in a different spot depending on whether the dependency is hoisted,
and it pointed at a non-existent path. A top-level `file:` spec is
defined to resolve against the package root.

Verified by grepping the built chunks for `zrender`, ECharts' renderer,
which is absent.

## Removing it

Replace the `echarts` dependency with a real version and delete this
directory. Pin **6.1.0 or later**: every release below it carries
GHSA-fgmj-fm8m-jvvx, and `pptx-preview`'s own `^5.5.1` range resolves to
a vulnerable one.

## Why the version says 5.6.0

It has to satisfy the `^5.5.1` range `pptx-preview` declares, or `npm ls`
reports the tree as invalid and exits non-zero. The number tracks that
range and says nothing about the contents — there is no ECharts code
here at any version.
