import { Injectable, inject } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  QuotaRequestDialogComponent,
  QuotaRequestDialogData,
  QuotaRequestDialogResult,
} from './quota-request-dialog.component';

/** Opens the one request dialog every entry point shares (quota-requests spec §5.1). */
@Injectable({ providedIn: 'root' })
export class QuotaRequestDialogService {
  private dialog = inject(Dialog);

  open(data: QuotaRequestDialogData = {}): Promise<QuotaRequestDialogResult> {
    const ref = this.dialog.open<QuotaRequestDialogResult, QuotaRequestDialogData>(QuotaRequestDialogComponent, {
      data,
    });
    return firstValueFrom(ref.closed);
  }
}
