import { ChangeDetectionStrategy, Component, OnDestroy, OnInit, computed, inject } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { TooltipDirective } from '../../../components/tooltip';
import {
  IncognitoChatService,
  incognitoIcon,
  incognitoSlashIcon,
} from '../../services/incognito';

/**
 * The new-chat composer's incognito switch (#1738), beside the model picker.
 * Off (slashed icon) whenever a new chat opens; the choice is handed to the
 * conversation by its first message and can't be changed after.
 */
@Component({
  selector: 'app-incognito-toggle',
  imports: [NgIcon, TooltipDirective],
  viewProviders: [provideIcons({ incognitoIcon, incognitoSlashIcon })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      (click)="incognito.toggle()"
      [attr.aria-pressed]="on()"
      [attr.aria-label]="label()"
      [appTooltip]="label()"
      appTooltipPosition="top"
      class="flex size-8 items-center justify-center rounded-full transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
      [class]="
        on()
          ? 'bg-gray-900 text-white hover:bg-gray-700 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white'
          : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200'
      "
    >
      <ng-icon [name]="on() ? 'incognitoIcon' : 'incognitoSlashIcon'" class="size-4" aria-hidden="true" />
    </button>
  `,
})
export class IncognitoToggleComponent implements OnInit, OnDestroy {
  protected readonly incognito = inject(IncognitoChatService);

  protected readonly on = this.incognito.pending;
  protected readonly label = computed(() =>
    this.on()
      ? 'Disable incognito chat, will create new memories'
      : 'Activate incognito chat, will not create new memories',
  );

  ngOnInit(): void {
    // Every new chat starts as not incognito.
    this.incognito.reset();
  }

  ngOnDestroy(): void {
    // Leaving the new-chat composer without sending drops the choice, so it
    // can't carry over to another conversation. A send claims it first.
    this.incognito.reset();
  }
}
