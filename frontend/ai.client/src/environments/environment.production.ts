// Production environment. Swapped in for environment.ts at build time
// via angular.json fileReplacements (under configurations.production).
//
// `appApiUrl: '/api'` is same-origin and fronted by CloudFront → app-api ALB
// (CloudFront's `/api/*` behavior with a path-strip Function). Same-origin
// is required for the BFF Token Handler `__Host-` cookies and eliminates
// CORS preflights on every request.
export const environment = {
    appApiUrl: '/api',
    // Proactive new-build check (AppUpdateService): on tab focus, at most every
    // 10 minutes, one conditional GET of index.html (a 304 when unchanged).
    // Kill switch — chunk-failure recovery works without it.
    versionCheckEnabled: true,
};
