import { Component, ChangeDetectionStrategy } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

interface FineTuningTab {
  label: string;
  route: string;
  exact: boolean;
}

@Component({
  selector: 'app-fine-tuning-layout',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  host: { class: 'block' },
  template: `
    <div class="mb-6">
      <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Fine-Tuning</h1>
      <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Manage who can access fine-tuning and review the resulting compute spend.
      </p>
    </div>

    <div class="mb-6 border-b border-gray-200 dark:border-white/10">
      <nav class="-mb-px flex flex-wrap gap-x-6" aria-label="Fine-tuning sections">
        @for (tab of tabs; track tab.route) {
          <a
            [routerLink]="tab.route"
            routerLinkActive="border-blue-500 text-blue-600 dark:border-blue-400 dark:text-blue-400"
            [routerLinkActiveOptions]="{ exact: tab.exact }"
            #rla="routerLinkActive"
            [attr.aria-current]="rla.isActive ? 'page' : null"
            class="whitespace-nowrap border-b-2 border-transparent px-1 py-3 text-sm/6 font-medium text-gray-500 hover:border-gray-300 hover:text-gray-700 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-200"
          >
            {{ tab.label }}
          </a>
        }
      </nav>
    </div>

    <router-outlet />
  `,
})
export class FineTuningLayout {
  readonly tabs: FineTuningTab[] = [
    { label: 'Access', route: '.', exact: true },
    { label: 'Costs', route: 'costs', exact: false },
  ];
}
