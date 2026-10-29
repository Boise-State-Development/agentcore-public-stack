/**
 * Incognito icons (#1738), drawn in Heroicons' outline style (24px grid,
 * 1.5 stroke) because Heroicons has none. Register with `provideIcons`.
 */
const HAT_AND_GLASSES =
  'M2.25 11h19.5M5 11l2-6.3a1 1 0 0 1 1.3-.63l2.2.85a4.2 4.2 0 0 0 3 0l2.2-.85a1 1 0 0 1 1.3.63L19 11' +
  'M10.5 16.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm9 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm-9 0c1-.8 2-.8 3 0';

const svg = (d: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="${d}"/></svg>`;

export const incognitoIcon = svg(HAT_AND_GLASSES);
export const incognitoSlashIcon = svg(`${HAT_AND_GLASSES}M3 3l18 18`);
