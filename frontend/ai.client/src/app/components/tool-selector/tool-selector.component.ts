import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  model,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroMagnifyingGlass, heroXMark } from '@ng-icons/heroicons/outline';
import { ToolSelectorItem } from './tool-selector.model';
import {
  isChildSelected,
  isItemSelected,
  isWholeItemSelected,
  selectedChildCount,
  toggleChild,
  toggleItem,
  withoutItem,
} from './tool-selection';

/** Rows that share a group, as rendered. */
interface RowGroup {
  name: string;
  rows: ToolSelectorItem[];
}

/** Where an unset group lands once any row has one, so every row still has a home. */
const FALLBACK_GROUP = 'Other';

/** Above this the list scrolls. Fixed strings so Tailwind can see them. */
const HEIGHT_CLASS = {
  sm: 'max-h-60',
  md: 'max-h-80',
  lg: 'max-h-[28rem]',
} as const;

let nextInstance = 0;

/**
 * The one list for picking tools (and, by the same rules, skills): search, a
 * category filter, "Selected only", select-all/clear over what's shown, and
 * per-tool narrowing of an MCP server through scoped `serverId::toolName` refs.
 *
 * Presentational. The parent feeds it rows already filtered by the backend's
 * RBAC and owns the save; this reports the new selection through `selected`
 * (`[(selected)]`, or `[selected]` + `(selectedChange)` when the parent has
 * side effects to run). Nothing here is an access boundary.
 *
 * The control is a checkbox, not a switch, on purpose: every surface collects a
 * selection that a Save commits, and a switch reads as "this takes effect now".
 */
@Component({
  selector: 'app-tool-selector',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroChevronDown, heroMagnifyingGlass, heroXMark })],
  templateUrl: './tool-selector.component.html',
  host: { class: 'block' },
})
export class ToolSelectorComponent {
  /** Every selectable row, in the order to show them. */
  readonly items = input.required<readonly ToolSelectorItem[]>();
  /** Selected refs: bare ids, and `id::tool` for a narrowed server. */
  readonly selected = model<ReadonlySet<string>>(new Set());

  /** The list's accessible name, used when `labelledBy` is not set. */
  readonly label = input('Tools');
  /** Id of a visible heading that names the list; preferred over `label`. */
  readonly labelledBy = input<string | null>(null);
  /** Id of visible text elsewhere that describes the list, such as a section blurb. */
  readonly describedBy = input<string | null>(null);
  /** What a row is, for counts and copy: "3 tools selected". */
  readonly noun = input('tool');
  readonly nounPlural = input('tools');
  /** Per-surface guidance under the toolbar, such as a snapshot note. */
  readonly helperText = input('');
  /** Shown when there are no rows at all. */
  readonly emptyText = input('');
  /** Read-only: everything is visible, nothing can change. */
  readonly disabled = input(false);
  readonly searchable = input(true);
  /** Category chips and "Selected only". */
  readonly filterable = input(true);
  /** "Select all" / "Clear" over the rows currently shown. */
  readonly bulkActions = input(true);
  readonly maxHeight = input<keyof typeof HEIGHT_CLASS>('md');
  /** A row's `action` button was pressed. */
  readonly itemAction = output<ToolSelectorItem>();

  private readonly uid = `tool-selector-${nextInstance++}`;
  private readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('search');

  protected readonly query = signal('');
  protected readonly activeGroup = signal<string | null>(null);
  protected readonly selectedOnly = signal(false);
  private readonly expanded = signal<ReadonlySet<string>>(new Set());
  private readonly expandedDetails = signal<ReadonlySet<string>>(new Set());

  protected readonly heightClass = computed(() => HEIGHT_CLASS[this.maxHeight()]);
  protected readonly searchId = `${this.uid}-search`;
  protected readonly helperId = `${this.uid}-helper`;
  protected readonly statusId = `${this.uid}-status`;

  /** Stable per-row DOM id prefix. Refs can hold `::` and spaces, so they aren't used directly. */
  private readonly rowIndex = computed(() => {
    const index = new Map<string, number>();
    this.items().forEach((item, i) => index.set(item.id, i));
    return index;
  });

  /** The list's description: the parent's text first, then this list's own helper text. */
  protected readonly listDescribedBy = computed(() => {
    const ids = [this.describedBy(), this.helperText() ? this.helperId : null].filter(Boolean);
    return ids.length ? ids.join(' ') : null;
  });

  /** Groups only exist once some row has one; a flat list stays flat. */
  protected readonly grouped = computed(() => this.items().some((item) => !!item.group));

  protected readonly groupNames = computed<string[]>(() => {
    if (!this.grouped()) return [];
    const names: string[] = [];
    for (const item of this.items()) {
      const name = this.groupOf(item);
      if (!names.includes(name)) names.push(name);
    }
    return names;
  });

  protected readonly showGroupChips = computed(() => this.filterable() && this.groupNames().length > 1);

  protected readonly filtering = computed(
    () => !!this.query().trim() || this.activeGroup() !== null || this.selectedOnly(),
  );

  /** Rows that pass search, category and "Selected only", in input order. */
  protected readonly visibleRows = computed<ToolSelectorItem[]>(() => {
    const query = this.query().trim().toLowerCase();
    const group = this.activeGroup();
    const selectedOnly = this.selectedOnly();
    const selected = this.selected();
    return this.items().filter((item) => {
      if (group !== null && this.groupOf(item) !== group) return false;
      if (selectedOnly && !isItemSelected(selected, item.id)) return false;
      if (!query) return true;
      return (
        item.name.toLowerCase().includes(query) ||
        (item.description ?? '').toLowerCase().includes(query)
      );
    });
  });

  protected readonly visibleGroups = computed<RowGroup[]>(() => {
    const rows = this.visibleRows();
    if (!this.grouped()) return [{ name: '', rows }];
    const byGroup = new Map<string, ToolSelectorItem[]>();
    for (const row of rows) {
      const name = this.groupOf(row);
      const bucket = byGroup.get(name);
      if (bucket) bucket.push(row);
      else byGroup.set(name, [row]);
    }
    return [...byGroup.entries()].map(([name, groupRows]) => ({ name, rows: groupRows }));
  });

  protected readonly selectedCount = computed(() => {
    const selected = this.selected();
    return this.items().filter((item) => isItemSelected(selected, item.id)).length;
  });

  /** Shown rows "Select all" would add. */
  private readonly addable = computed(() => {
    const selected = this.selected();
    return this.visibleRows().filter(
      (item) => !item.locked && !item.addBlocked && !isItemSelected(selected, item.id),
    );
  });

  /** Shown rows "Clear" would remove. */
  private readonly clearable = computed(() => {
    const selected = this.selected();
    return this.visibleRows().filter((item) => !item.locked && isItemSelected(selected, item.id));
  });

  protected readonly addableCount = computed(() => this.addable().length);
  protected readonly clearableCount = computed(() => this.clearable().length);

  /**
   * What the polite live region says. Empty until a filter is applied, so the
   * list doesn't announce itself on load; after that every change is spoken.
   */
  protected readonly statusMessage = computed(() => {
    if (!this.filtering()) return '';
    const count = this.visibleRows().length;
    return `${count} of ${this.items().length} ${this.items().length === 1 ? this.noun() : this.nounPlural()} shown`;
  });

  protected readonly emptyMessage = computed(() => {
    if (!this.items().length) return this.emptyText() || `No ${this.nounPlural()} available.`;
    const query = this.query().trim();
    if (query) return `No ${this.nounPlural()} match “${query}”.`;
    if (this.selectedOnly()) return `No ${this.nounPlural()} selected.`;
    return `No ${this.nounPlural()} in this category.`;
  });

  // ---- filters -----------------------------------------------------------

  protected onSearch(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  protected clearSearch(): void {
    this.query.set('');
    this.searchInput()?.nativeElement.focus();
  }

  /**
   * Escape clears a non-empty search instead of bubbling. Inside a dialog that keeps
   * the first Escape from closing it with the user's half-typed query; an empty
   * search lets Escape through, so the dialog still closes the usual way.
   */
  protected onSearchKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape' || !this.query()) return;
    event.stopPropagation();
    event.preventDefault();
    this.query.set('');
  }

  protected setGroup(name: string | null): void {
    this.activeGroup.set(name);
  }

  protected toggleSelectedOnly(): void {
    this.selectedOnly.update((on) => !on);
  }

  // ---- selection ---------------------------------------------------------

  protected isSelected(item: ToolSelectorItem): boolean {
    return isItemSelected(this.selected(), item.id);
  }

  /** Off-to-on is refused for `addBlocked`; neither direction for `locked`. */
  protected canToggle(item: ToolSelectorItem): boolean {
    if (this.disabled() || item.locked) return false;
    return !item.addBlocked || this.isSelected(item);
  }

  protected toggle(item: ToolSelectorItem, event?: Event): void {
    if (!this.canToggle(item)) {
      // A native checkbox flips itself before `change` fires; put it back.
      if (event) (event.target as HTMLInputElement).checked = this.isSelected(item);
      return;
    }
    this.selected.set(toggleItem(this.selected(), item.id));
  }

  protected runAction(item: ToolSelectorItem): void {
    if (item.action && !item.action.disabled) this.itemAction.emit(item);
  }

  protected selectAllShown(): void {
    if (this.disabled()) return;
    const next = new Set(this.selected());
    for (const item of this.addable()) next.add(item.id);
    this.selected.set(next);
  }

  protected clearShown(): void {
    if (this.disabled()) return;
    let next: ReadonlySet<string> = this.selected();
    for (const item of this.clearable()) next = withoutItem(next, item.id);
    this.selected.set(next);
  }

  // ---- per-tool narrowing of an MCP server ------------------------------

  protected canNarrow(item: ToolSelectorItem): boolean {
    return !!item.children?.length && this.isSelected(item);
  }

  protected isExpanded(item: ToolSelectorItem): boolean {
    return this.expanded().has(item.id);
  }

  protected toggleExpanded(item: ToolSelectorItem): void {
    this.expanded.update((set) => flip(set, item.id));
  }

  protected isWhole(item: ToolSelectorItem): boolean {
    return isWholeItemSelected(this.selected(), item.id);
  }

  protected childCount(item: ToolSelectorItem): number {
    return selectedChildCount(this.selected(), item.id, childNames(item));
  }

  protected isChildOn(item: ToolSelectorItem, name: string): boolean {
    return isChildSelected(this.selected(), item.id, name);
  }

  protected toggleChildTool(item: ToolSelectorItem, name: string): void {
    if (this.disabled() || item.locked) return;
    this.selected.set(toggleChild(this.selected(), item.id, childNames(item), name));
  }

  protected isDetailOpen(item: ToolSelectorItem, name: string): boolean {
    return this.expandedDetails().has(`${item.id}::${name}`);
  }

  protected toggleDetail(item: ToolSelectorItem, name: string): void {
    this.expandedDetails.update((set) => flip(set, `${item.id}::${name}`));
  }

  // ---- ids ---------------------------------------------------------------

  protected rowId(item: ToolSelectorItem, part: string): string {
    return `${this.uid}-${this.rowIndex().get(item.id) ?? 0}-${part}`;
  }

  protected childId(item: ToolSelectorItem, childIndex: number, part: string): string {
    return `${this.rowId(item, 'child')}-${childIndex}-${part}`;
  }

  protected groupId(index: number): string {
    return `${this.uid}-group-${index}`;
  }

  /** `aria-describedby` for a row: its note, then its description. */
  protected rowDescribedBy(item: ToolSelectorItem): string | null {
    const ids: string[] = [];
    if (item.note) ids.push(this.rowId(item, 'note'));
    if (item.description) ids.push(this.rowId(item, 'desc'));
    if (this.canNarrow(item) && !this.isWhole(item)) ids.push(this.rowId(item, 'count'));
    return ids.length ? ids.join(' ') : null;
  }

  private groupOf(item: ToolSelectorItem): string {
    return item.group?.trim() || FALLBACK_GROUP;
  }
}

function childNames(item: ToolSelectorItem): string[] {
  return (item.children ?? []).map((child) => child.name);
}

function flip(set: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(set);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}
