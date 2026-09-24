import { Redirect } from 'expo-router';
import { ActivityIndicator, View } from 'react-native';

import { usePairing } from '../lib/pairing';
import { useTheme } from '../lib/theme';

export default function Index() {
  const { pairing, loaded } = usePairing();
  const theme = useTheme();
  if (!loaded) {
    return (
      <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: theme.bg }}>
        <ActivityIndicator color={theme.accent} />
      </View>
    );
  }
  // On the web build the pairing link itself opens the app: go to /pair first
  // so the key in the address is picked up even when something is stored.
  const hasLinkKey = typeof window !== 'undefined' && !!window.location?.hash?.includes('k=');
  return <Redirect href={pairing && !hasLinkKey ? '/chat' : '/pair'} />;
}
