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
 * It is also a controlled checkbox. CDK's flips its own `checked` on every
 * trigger, but these rows bind `cdkMenuItemChecked` to real state (a tool's
 * preference, a connector's connection), and that state may not change: a
 * sign-in abandoned, a disconnect or a save that fails. The binding's value
 * is then the same as before, Angular never writes it again, and
 * `aria-checked` would disagree with the row until the next real change. So
 * a trigger leaves `checked` alone; only the binding moves it.
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
    const checked = this.checked;
    super.trigger({ ...options, keepOpen: true });
    this.checked = checked;
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
