import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

interface ModelsTab {
  label: string;
  route: string;
  exact: boolean;
}

/** The tab strip shared by the Manage Models pages: chat models and task models. */
@Component({
  selector: 'app-models-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  host: { class: 'block' },
  template: `
    <div class="mb-6 border-b border-gray-200 dark:border-white/10">
      <nav class="-mb-px flex flex-wrap gap-x-6" aria-label="Model sections">
        @for (tab of tabs; track tab.route) {
          <a
            [routerLink]="tab.route"
            routerLinkActive="border-primary-500 text-primary-accessible dark:border-primary-400 dark:text-primary-accessible-dark"
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
  `,
})
export class ModelsTabsComponent {
  readonly tabs: ModelsTab[] = [
    { label: 'Chat models', route: '/admin/manage-models', exact: true },
    { label: 'Task models', route: '/admin/manage-models/tasks', exact: true },
  ];
}
