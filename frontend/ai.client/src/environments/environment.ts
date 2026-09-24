// Local-dev environment. Production builds replace this with
// environment.production.ts via angular.json fileReplacements.
export const environment = {
    appApiUrl: 'http://localhost:8000',
    // Proactive new-build check (AppUpdateService). Inert on the dev server,
    // whose unhashed `main.js` gives it nothing to compare.
    versionCheckEnabled: true,
};
