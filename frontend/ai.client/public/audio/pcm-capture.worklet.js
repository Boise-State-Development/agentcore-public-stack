/**
 * AudioWorklet processor that captures PCM samples from the microphone
 * and forwards them to the main thread via MessagePort.
 *
 * Registered as 'pcm-capture' and used by AudioRecorderService.
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      this.port.postMessage(new Float32Array(input[0]));
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
