import {
  Component,
  ChangeDetectionStrategy,
  inject,
  output,
  computed,
} from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { VoiceChatService } from '../../services/voice';

@Component({
  selector: 'app-voice-overlay',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [A11yModule],
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
