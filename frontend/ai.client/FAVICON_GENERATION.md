# Favicon Generation

## Overview

Favicons are automatically generated at build-time from a single source PNG image. This process runs during `npm run prebuild` (which is called before `npm run build` and `npm start`).

## How It Works

1. **Source Image**: Place a square PNG at `public/favicon-source.png` (512x512 or larger recommended)
2. **Automatic Generation**: The build process generates:
   - `favicon-16x16.png` (browser tab)
   - `favicon-32x32.png` (browser tab, high DPI)
   - `apple-touch-icon.png` (180x180 for iOS)
   - `android-chrome-192x192.png` (Android PWA)
   - `android-chrome-512x512.png` (Android PWA splash screen)
   - `site.webmanifest` (PWA manifest with brand colors and app name)

3. **Manifest Updates**: The `site.webmanifest` is automatically updated with:
   - App name from `brand.config.ts` (`appName`)
   - Theme color from `brand.config.ts` (`backgroundColors.dark`)
   - Background color from `brand.config.ts` (`backgroundColors.light`)

## Rebranding

To change the favicon when rebranding:

1. **Create a new source image** (512x512 or larger, PNG format, ideally square with transparent background)
2. **Place it at** `frontend/ai.client/public/favicon-source.png`
3. **Run the build**: `npm run build` or `npm start`
4. **Done**: All favicon sizes are automatically generated and the manifest is updated

## Generated Files Location

All generated favicons are written to `public/favicon/`:
- `favicon-16x16.png`
- `favicon-32x32.png`
- `apple-touch-icon.png`
- `android-chrome-192x192.png`
- `android-chrome-512x512.png`
- `site.webmanifest` (dynamically generated)

The `favicon.ico` file (multi-resolution ICO format) is kept as-is from the existing build.

## Skipping Generation

If `public/favicon-source.png` is missing, the build logs a warning and skips favicon generation. The existing favicon files are retained.

## Implementation Details

- **Tool**: Built with [Sharp](https://sharp.pixelplumbing.com/) image processing library
- **Script**: `scripts/branding/generate-favicons.ts`
- **Build Integration**: Runs as part of `npm run prebuild`
- **PNG Support**: Reads PNG source images and generates PNG outputs
- **Transparent Backgrounds**: All generated PNGs preserve transparency
- **Quality**: Images are resized with proper interpolation and gamma correction

## Customizing the Generation

To modify favicon generation (e.g., add new sizes or formats), edit `scripts/branding/generate-favicons.ts`:

```typescript
// Add a new size
const FAVICON_SIZES: Array<[number, string]> = [
  [16, 'favicon-16x16.png'],
  [32, 'favicon-32x32.png'],
  [180, 'apple-touch-icon.png'],
  [192, 'android-chrome-192x192.png'],
  [512, 'android-chrome-512x512.png'],
  [1024, 'favicon-1024x1024.png'], // New size
];
```

Then update `index.html` and `site.webmanifest` to reference the new size.

## Troubleshooting

### Favicon not updating
- Ensure `favicon-source.png` exists in `public/`
- Clear browser cache (Ctrl+Shift+Delete)
- Run `npm run prebuild` manually to verify generation

### Image quality issues
- Use a source image at least 512x512 pixels
- Ensure the source image has good contrast and detail
- Avoid heavily compressed source images

### Build failure
- Check that `favicon-source.png` is a valid PNG
- Ensure the image file is not corrupted
- Try a different PNG file to isolate the issue
