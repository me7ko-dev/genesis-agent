/**
 * Кога сканиран QR код да се опита отново (одит 2026-10-27).
 *
 * expo-camera 57 вика onBarcodeScanned за същия код в кадъра на всеки 500 ms
 * (EventThrottleMs в node_modules/expo-camera/build/CameraView.js). След
 * неуспешно сдвояване камерата се включваше пак, кодът още е пред нея, и
 * tryPair тръгваше веднага отново — над 30 опита в минута, а след тях
 * спирачката на сървъра (429) отказва и верния ключ за минута. Същият код
 * след неуспех чака RESCAN_AFTER_FAIL_MS (до 12 опита в минута — компютърът,
 * включен междувременно, пак се хваща сам); друг код минава веднага.
 */
export const RESCAN_AFTER_FAIL_MS = 5000;

export class ScanGate {
  private failed = '';
  private at = 0;

  allows(data: string, now: number): boolean {
    return data !== this.failed || now - this.at >= RESCAN_AFTER_FAIL_MS;
  }

  failedWith(data: string, now: number): void {
    this.failed = data;
    this.at = now;
  }

  succeeded(): void {
    this.failed = '';
  }
}
