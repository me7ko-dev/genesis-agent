import { requireOptionalNativeModule } from 'expo';

// Android only (android/…/TermuxBridgeModule.kt). On iOS and the web the
// module does not exist: null, and the phone-only mode is not offered there.
type TermuxBridge = {
  isTermuxInstalled(): boolean;
  hasRunPermission(): boolean;
  runCommand(path: string, args: string[], workdir: string, background: boolean): boolean;
  openTermux(): boolean;
};

export default requireOptionalNativeModule<TermuxBridge>('TermuxBridge');
