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
			social: [
				{
					icon: 'github',
					label: 'GitHub',
					href: 'https://github.com/Boise-State-Development/agentcore-public-stack',
				},
			],
			sidebar: [
				{ label: 'Getting Started', items: [{ autogenerate: { directory: 'getting-started' } }] },
				{ label: 'Installation', items: [{ autogenerate: { directory: 'installation' } }] },
				{ label: 'Deployment', items: [{ autogenerate: { directory: 'deployment' } }] },
				{ label: 'Configuration', items: [{ autogenerate: { directory: 'configuration' } }] },
				{ label: 'Features', items: [{ autogenerate: { directory: 'features' } }] },
				{ label: 'MCP & Integrations', items: [{ autogenerate: { directory: 'integrations' } }] },
				{ label: 'Development', items: [{ autogenerate: { directory: 'development' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
			],
		}),
	],
});
