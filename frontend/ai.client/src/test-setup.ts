// Loads the JIT compiler before any spec runs.
//
// Angular packages ship fesm2022 chunks with partial declarations
// (ɵɵngDeclareInjectable / ɵɵngDeclareFactory) that compile EAGERLY at module
// evaluation. The unit-test builder keeps node_modules external
// (externalPackages: true), so those chunks reach the vitest module runner
// unlinked and fall back to JIT. Normally `@angular/core/testing` (imported by
// the builder's init-testbed setup) transitively evaluates `@angular/compiler`
// first, but that ordering is incidental — specs with no static Angular imports
// (e.g. app.spec.ts's dynamic `import('./app')`) can evaluate a raw
// `@angular/common` chunk before the compiler is present and die with
// "The injectable 'PlatformLocation' needs to be compiled using the JIT
// compiler, but '@angular/compiler' is not available". This import makes the
// compiler's presence an explicit invariant. See angular/angular-cli#31993.
import '@angular/compiler';
