import { CameraView, useCameraPermissions } from 'expo-camera';
import { router } from 'expo-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { usePairing } from '../lib/pairing';
import { GenesisClient, ProtocolError, parsePairingUrl } from '../lib/protocol';
import { random } from '../lib/random';
import { mono, useTheme } from '../lib/theme';

export default function Pair() {
  const theme = useTheme();
  const { pair } = usePairing();
  const [permission, requestPermission] = useCameraPermissions();
  const [manual, setManual] = useState('');
  const [error, setError] = useState('');
  const [checking, setChecking] = useState(false);
  const busy = useRef(false);

  const tryPair = useCallback(async (raw: string) => {
    if (busy.current) return;
    const pairing = parsePairingUrl(raw);
    if (!pairing) {
      setError('Това не е код от „genesis serve".');
      return;
    }
    busy.current = true;
    setChecking(true);
    setError('');
    try {
      const client = new GenesisClient(pairing, random);
      const status = await client.status();
      if (status.app !== 'genesis' || status.key_id !== client.keyId()) {
        throw new ProtocolError('another computer', 'unauthorized');
      }
      await pair({ ...pairing, name: status.name || pairing.name });
      if (Platform.OS === 'web') window.history.replaceState(null, '', '/');
      router.replace('/chat');
    } catch (e) {
      const kind = e instanceof ProtocolError ? e.kind : 'network';
      setError(
        kind === 'network'
          ? `Не стигам до ${pairing.base}. Телефонът и компютърът в една Wi-Fi мрежа ли са? На Windows защитната стена трябва да пуска „genesis".`
          : kind === 'clock'
            ? 'Часовникът на телефона или компютъра е разминат с повече от 5 минути.'
            : 'Компютърът не прие ключа. Сканирай кода отново (или `genesis serve --reset` е сменил ключа).',
      );
    } finally {
      busy.current = false;
      setChecking(false);
    }
  }, [pair]);

  // Web build opened from the QR code: the key is in the address itself.
  useEffect(() => {
    if (Platform.OS === 'web' && window.location.hash.includes('k=')) tryPair(window.location.href);
  }, [tryPair]);

  const cameraOk = Platform.OS !== 'web' && permission?.granted;

  return (
    <SafeAreaView style={[styles.flex, { backgroundColor: theme.bg }]}>
      <KeyboardAvoidingView style={styles.flex} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={styles.body} keyboardShouldPersistTaps="handled">
          <Text style={[styles.title, { color: theme.text }]}>Genesis</Text>
          <Text style={[styles.lead, { color: theme.muted }]}>
            Агентът работи на компютъра ти; телефонът е прозорец към него.
          </Text>

          <View style={[styles.step, { backgroundColor: theme.surface, borderColor: theme.border }]}>
            <Text style={[styles.stepTitle, { color: theme.text }]}>1. На компютъра</Text>
            <Text selectable style={[mono, styles.cmd, { backgroundColor: theme.code, color: theme.text }]}>genesis serve</Text>
          </View>

          <View style={[styles.step, { backgroundColor: theme.surface, borderColor: theme.border }]}>
            <Text style={[styles.stepTitle, { color: theme.text }]}>2. Сканирай QR кода</Text>
            {Platform.OS === 'web' ? (
              <Text style={{ color: theme.muted }}>Отвори кода с камерата на телефона — връзката води направо тук.</Text>
            ) : cameraOk ? (
              <View style={styles.cameraBox}>
                <CameraView
                  style={StyleSheet.absoluteFill}
                  facing="back"
                  barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
                  onBarcodeScanned={checking ? undefined : ({ data }) => tryPair(data)}
                />
              </View>
            ) : (
              <Pressable
                accessibilityRole="button"
                onPress={requestPermission}
                style={[styles.primary, { backgroundColor: theme.accent }]}>
                <Text style={[styles.primaryText, { color: theme.accentText }]}>Разреши камерата</Text>
              </Pressable>
            )}
          </View>

          <View style={[styles.step, { backgroundColor: theme.surface, borderColor: theme.border }]}>
            <Text style={[styles.stepTitle, { color: theme.text }]}>…или постави връзката</Text>
            <TextInput
              value={manual}
              onChangeText={setManual}
              placeholder="http://192.168.1.5:8765/#k=…"
              placeholderTextColor={theme.muted}
              autoCapitalize="none"
              autoCorrect={false}
              style={[mono, styles.input, { color: theme.text, borderColor: theme.border, backgroundColor: theme.bg }]}
            />
            <Pressable
              accessibilityRole="button"
              disabled={!manual.trim() || checking}
              onPress={() => tryPair(manual)}
              style={[styles.primary, { backgroundColor: theme.accent, opacity: manual.trim() ? 1 : 0.5 }]}>
              <Text style={[styles.primaryText, { color: theme.accentText }]}>Свържи</Text>
            </Pressable>
          </View>

          {checking ? <ActivityIndicator color={theme.accent} /> : null}
          {error ? <Text style={[styles.error, { color: theme.danger }]}>{error}</Text> : null}
          <Text style={[styles.foot, { color: theme.muted }]}>
            Ключът идва само от кода и се пази в защитеното хранилище на телефона. Всичко между телефона и компютъра е криптирано.
          </Text>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  body: { padding: 20, gap: 16, maxWidth: 560, width: '100%', alignSelf: 'center' },
  title: { fontSize: 34, fontWeight: '800', marginTop: 12 },
  lead: { fontSize: 16, lineHeight: 22 },
  step: { borderRadius: 14, borderWidth: StyleSheet.hairlineWidth, padding: 14, gap: 10 },
  stepTitle: { fontSize: 16, fontWeight: '700' },
  cmd: { fontSize: 15, padding: 10, borderRadius: 8, overflow: 'hidden' },
  cameraBox: { height: 280, borderRadius: 12, overflow: 'hidden' },
  input: { borderWidth: 1, borderRadius: 10, padding: 10, fontSize: 13 },
  primary: { borderRadius: 12, paddingVertical: 13, alignItems: 'center' },
  primaryText: { fontWeight: '700', fontSize: 16 },
  error: { fontSize: 14, lineHeight: 20 },
  foot: { fontSize: 12, lineHeight: 17 },
});
