import { Directive } from '@angular/core';
import { CdkMenuItem, CdkMenuItemCheckbox, CdkMenuItemSelectable } from '@angular/cdk/menu';

/**
 * A CDK menu checkbox item whose menu stays open when it is toggled.
 *
 * The composer's Tools submenu is a list of switches, and a person flipping
 * three of them should not have to reopen the menu three times. CDK closes
 * the whole menu tree on every trigger unless the caller passes `keepOpen`,
 * which only its own keyboard handler ever does (Space, not Enter), so this
 * subclass makes every trigger keep the menu open. Escape, a click outside
 * and the other items still close it as usual.
 *
 * Everything else — `role="menuitemcheckbox"`, `aria-checked` from
 * `cdkMenuItemChecked`, arrow-key navigation, `cdkMenuItemTriggered` — is
 * inherited. The providers are redeclared because Angular does not inherit
 * them, and the CDK menu finds its items through those tokens.
 */
@Directive({
  selector: '[appMenuToggleItem]',
  providers: [
    { provide: CdkMenuItemSelectable, useExisting: MenuToggleItemDirective },
    { provide: CdkMenuItem, useExisting: MenuToggleItemDirective },
  ],
})
export class MenuToggleItemDirective extends CdkMenuItemCheckbox {
  override trigger(options?: { keepOpen: boolean }): void {
    super.trigger({ ...options, keepOpen: true });
  }
}

/**
 * A plain CDK menu item that keeps its menu open when triggered, for an action
 * whose progress shows in the same menu: the Connectors submenu's "Open
 * sign-in" row reopens a blocked popup, and the connector's row above it then
 * reads "Awaiting consent" like any other switch mid-flight.
 */
@Directive({
  selector: '[appMenuKeepOpenItem]',
  providers: [{ provide: CdkMenuItem, useExisting: MenuKeepOpenItemDirective }],
})
export class MenuKeepOpenItemDirective extends CdkMenuItem {
  override trigger(options?: { keepOpen: boolean }): void {
    super.trigger({ ...options, keepOpen: true });
  }
}
