// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// https://astro.build/config
export default defineConfig({
	// Project GitHub Pages: served at https://boise-state-development.github.io/agentcore-public-stack/
	// `base` must match the repo name so assets and internal links resolve under the sub-path.
	site: 'https://boise-state-development.github.io',
	base: '/agentcore-public-stack/',
	integrations: [
		starlight({
			title: 'AgentCore Public Stack',
			// Theme-aware logo: light SVG shown in light mode, dark SVG in dark mode.
			// `replacesTitle` is left at its default (false) so the globe sits beside the title text.
			logo: {
				light: './src/assets/globe-light.svg',
				dark: './src/assets/globe-dark.svg',
				alt: 'AgentCore Public Stack',
			},
			// Brand-aligned look & feel: frosted glass, graph-paper grid, and the
			// lava-lamp blob field from the product's login / first-boot screens.
			customCss: ['./src/styles/custom.css'],
			// Inject the distinctive type system (display grotesque + Inter + mono).
			head: [
				{
					tag: 'link',
					attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
				},
				{
					tag: 'link',
					attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true },
				},
				{
					tag: 'link',
					attrs: {
						rel: 'stylesheet',
						href: 'https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800&family=Inter:wght@400;450;500;600&family=JetBrains+Mono:wght@400;500&display=swap',
					},
				},
			],
			// Inject the decorative lava-lamp + grid backdrop behind every page,
			// and render the site title as a compact two-line wordmark.
			components: {
				PageFrame: './src/components/PageFrame.astro',
				SiteTitle: './src/components/SiteTitle.astro',
			},
			social: [
				{
					icon: 'github',
					label: 'GitHub',
					href: 'https://github.com/Boise-State-Development/agentcore-public-stack',
				},
			],
			sidebar: [
				{ label: 'Getting Started', items: [{ autogenerate: { directory: 'getting-started' } }] },
				{ label: 'Local Development', slug: 'local-development' },
				{ label: 'Deployment', items: [{ autogenerate: { directory: 'deployment' } }] },
				{ label: 'Configuration', items: [{ autogenerate: { directory: 'configuration' } }] },
				{ label: 'Features', items: [{ autogenerate: { directory: 'features' } }] },
				{ label: 'Admin', items: [{ autogenerate: { directory: 'admin' } }] },
				{ label: 'MCP & Integrations', items: [{ autogenerate: { directory: 'integrations' } }] },
				{ label: 'Development', items: [{ autogenerate: { directory: 'development' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
			],
		}),
	],
});
