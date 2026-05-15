// Production environment. Swapped in for environment.ts at build time
// via angular.json fileReplacements (under configurations.production).
//
// `appApiUrl: '/api'` is same-origin and fronted by CloudFront → app-api ALB
// (CloudFront's `/api/*` behavior with a path-strip Function). Same-origin
// is required for the BFF Token Handler `__Host-` cookies and eliminates
// CORS preflights on every request.
export const environment = {
    appApiUrl: '/api',
};
