import {
  Component,
  ChangeDetectionStrategy,
  inject,
  output,
  computed,
} from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import {
  VoiceChatService,
  INTERRUPT_LEVEL_MIN_DB,
  INTERRUPT_LEVEL_MAX_DB,
  MIC_METER_FLOOR_DB,
} from '../../services/voice';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

/** Position of a dBFS level on the meter, 0 to 100. */
function levelToPercent(levelDb: number): number {
  if (!Number.isFinite(levelDb)) return 0;
  const pct = ((levelDb - MIC_METER_FLOOR_DB) / -MIC_METER_FLOOR_DB) * 100;
  return Math.max(0, Math.min(100, pct));
}

@Component({
  selector: 'app-voice-overlay',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [A11yModule, SpinnerComponent],
  templateUrl: './voice-overlay.component.html',
  styleUrl: './voice-overlay.component.css',
  host: {
    '(keydown.escape)': 'endSession()',
  },
})
export class VoiceOverlayComponent {
  private readonly voiceChatService = inject(VoiceChatService);

  /** Emitted when voice session ends (overlay should be removed by parent) */
  closed = output<void>();

  // Expose service signals to template
  readonly voiceStatus = this.voiceChatService.status;

  // Interrupt level slider and mic meter (#2019)
  readonly minLevelDb = INTERRUPT_LEVEL_MIN_DB;
  readonly maxLevelDb = INTERRUPT_LEVEL_MAX_DB;
  readonly interruptLevelDb = this.voiceChatService.interruptLevelDb;
  readonly interruptLevelPercent = computed(() => levelToPercent(this.interruptLevelDb()));
  readonly micLevelPercent = computed(() => levelToPercent(this.voiceChatService.micLevelDb()));

  setInterruptLevel(value: string | number): void {
    this.voiceChatService.setInterruptLevel(Number(value));
  }

  readonly statusLabel = computed(() => {
    switch (this.voiceStatus()) {
      case 'connecting': return 'Connecting';
      case 'listening': return 'Listening';
      case 'speaking': return 'Speaking';
      default: return '';
    }
  });

  readonly statusClass = computed(() => {
    const status = this.voiceStatus();
    return status === 'idle' ? '' : status;
  });

  endSession(): void {
    this.voiceChatService.disconnect();
    this.closed.emit();
  }
}
