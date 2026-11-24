import { Injectable, computed, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  TaskModelSlot,
  TaskModelSlotId,
  TaskModelUpdate,
  TaskModelsListResponse,
} from '../models/task-model.model';

/** Reads and writes the Task models tab (`/admin/task-models`). */
@Injectable({ providedIn: 'root' })
export class TaskModelsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/task-models`);

  readonly slotsResource = resource({
    loader: async () => {
      const response = await firstValueFrom(
        this.http.get<TaskModelsListResponse>(this.baseUrl()),
      );
      return response.slots;
    },
  });

  readonly slots = computed(() => this.slotsResource.value() ?? []);

  /** Set or clear one task's model. Returns the task as the backend now resolves it. */
  async update(slot: TaskModelSlotId, update: TaskModelUpdate): Promise<TaskModelSlot> {
    const updated = await firstValueFrom(
      this.http.put<TaskModelSlot>(`${this.baseUrl()}/${slot}`, update),
    );
    this.slotsResource.update(current =>
      (current ?? []).map(s => (s.slot === updated.slot ? updated : s)),
    );
    return updated;
  }
}
