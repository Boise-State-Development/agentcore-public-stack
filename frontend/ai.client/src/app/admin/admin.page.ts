import { Component, ChangeDetectionStrategy } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCpuChip,
  heroPencilSquare,
  heroScale,
  heroChartBar,
  heroClipboardDocumentList,
  heroMagnifyingGlass,
  heroCalendar,
  heroSparkles,
  heroCurrencyDollar,
  heroUsers,
  heroShieldCheck,
  heroWrenchScrewdriver,
  heroLink
} from '@ng-icons/heroicons/outline';

interface AdminFeature {
  title: string;
  description: string;
  icon: string;
  route: string;
}

@Component({
  selector: 'app-admin-page',
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroCpuChip,
      heroPencilSquare,
      heroScale,
      heroChartBar,
      heroClipboardDocumentList,
      heroMagnifyingGlass,
      heroCalendar,
      heroSparkles,
      heroCurrencyDollar,
      heroUsers,
      heroShieldCheck,
      heroWrenchScrewdriver,
      heroLink
    })
  ],
  templateUrl: './admin.page.html',
  styleUrl: './admin.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AdminPage {
  readonly features: AdminFeature[] = [
    {
      title: 'Cost Analytics',
      description: 'View system-wide usage metrics, top users by cost, model breakdowns, and cost trends. Export reports for analysis.',
      icon: 'heroCurrencyDollar',
      route: '/admin/costs',
    },
    {
      title: 'Manage Models',
      description: 'Configure and manage AI models available to users. Control model access by role, set pricing, and enable/disable models.',
      icon: 'heroPencilSquare',
      route: '/admin/manage-models',
    },
    {
      title: 'Tool Catalog',
      description: 'Manage the tool catalog, configure role-based access, and sync tools from the registry. Control which tools are available to users.',
      icon: 'heroWrenchScrewdriver',
      route: '/admin/tools',
    },
    // {
    //   title: 'Bedrock Models',
    //   description: 'Browse and explore AWS Bedrock foundation models. View model capabilities, pricing, and add models to your managed collection.',
    //   icon: 'heroCpuChip',
    //   route: '/admin/bedrock/models',
    // },
    // {
    //   title: 'Gemini Models',
    //   description: 'Browse and explore Google Gemini AI models. View model specifications, features, and add models to your managed collection.',
    //   icon: 'heroSparkles',
    //   route: '/admin/gemini/models',
    // },
    // {
    //   title: 'OpenAI Models',
    //   description: 'Browse and explore OpenAI models including GPT-4 and other offerings. View capabilities and add models to your managed collection.',
    //   icon: 'heroCpuChip',
    //   route: '/admin/openai/models',
    // },
    
    {
      title: 'User Lookup',
      description: 'Search and browse users to view their profile, costs, and quota status. Manage user-specific overrides and assignments.',
      icon: 'heroUsers',
      route: '/admin/users',
    },
    {
      title: 'Role Management',
      description: 'Create and manage application roles with tool and model permissions. Configure JWT mappings and role inheritance.',
      icon: 'heroShieldCheck',
      route: '/admin/roles',
    },
    {
      title: 'OAuth Providers',
      description: 'Configure third-party OAuth integrations for MCP tool authentication. Manage Google, Microsoft, GitHub, and custom providers.',
      icon: 'heroLink',
      route: '/admin/oauth-providers',
    },
    {
      title: 'Quota Tiers',
      description: 'Create and manage quota tiers with cost limits and soft limit configurations. Define monthly/daily limits and warning thresholds.',
      icon: 'heroScale',
      route: '/admin/quota/tiers',
    },
    {
      title: 'Quota Assignments',
      description: 'Assign quota tiers to users, roles, or email domains. Control priority and manage default tier assignments.',
      icon: 'heroClipboardDocumentList',
      route: '/admin/quota/assignments',
    },
    {
      title: 'Quota Overrides',
      description: 'Create temporary quota exceptions for individual users. Set custom limits or unlimited access with expiration dates.',
      icon: 'heroCalendar',
      route: '/admin/quota/overrides',
    },
    {
      title: 'Quota Inspector',
      description: 'Debug and inspect quota resolution for individual users. View resolved quotas, current usage, and recent blocks.',
      icon: 'heroMagnifyingGlass',
      route: '/admin/quota/inspector',
    },
    {
      title: 'Quota Events',
      description: 'Monitor quota enforcement events including warnings, blocks, resets, and override applications. Export event data to CSV.',
      icon: 'heroChartBar',
      route: '/admin/quota/events',
    },
    
  ];

  getIconBackgroundClasses(index: number): string {
    const backgrounds = [
      'bg-purple-100 dark:bg-purple-900/30',
      'bg-blue-100 dark:bg-blue-900/30',
      'bg-green-100 dark:bg-green-900/30',
      'bg-amber-100 dark:bg-amber-900/30',
      'bg-pink-100 dark:bg-pink-900/30',
      'bg-indigo-100 dark:bg-indigo-900/30',
      'bg-teal-100 dark:bg-teal-900/30',
      'bg-rose-100 dark:bg-rose-900/30',
      'bg-emerald-100 dark:bg-emerald-900/30',
    ];
    return backgrounds[index % backgrounds.length];
  }

  getIconColorClasses(index: number): string {
    const colors = [
      'text-purple-600 dark:text-purple-400',
      'text-blue-600 dark:text-blue-400',
      'text-green-600 dark:text-green-400',
      'text-amber-600 dark:text-amber-400',
      'text-pink-600 dark:text-pink-400',
      'text-indigo-600 dark:text-indigo-400',
      'text-teal-600 dark:text-teal-400',
      'text-rose-600 dark:text-rose-400',
      'text-emerald-600 dark:text-emerald-400',
    ];
    return colors[index % colors.length];
  }
}
