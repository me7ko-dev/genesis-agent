import { CameraView, useCameraPermissions } from 'expo-camera';
import { router, useLocalSearchParams } from 'expo-router';
import { useMemo, useState } from 'react';
import { ActivityIndicator, Platform, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { keysFromParams, maskKey, parseKeysLink } from '../lib/keys';
import { usePairing } from '../lib/pairing';
import { GenesisClient, ProtocolError } from '../lib/protocol';
import { random } from '../lib/random';
import { mono, useTheme } from '../lib/theme';

/**
 * The API keys from the computer into Genesis here: `genesis keys qr` shows a
 * QR code; scanned here (or opened by the phone's camera as a
 * genesisremote://keys link) they go to Genesis over the encrypted channel.
 */
export default function Keys() {
  const theme = useTheme();
  const { pairing } = usePairing();
  const params = useLocalSearchParams();
  const fromLink = useMemo(() => keysFromParams(params), [params]);
  const [scanned, setScanned] = useState<Record<string, string> | null>(null);
  const keys = scanned ?? (Object.keys(fromLink).length ? fromLink : null);
  const [permission, requestPermission] = useCameraPermissions();
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string[] | null>(null);
  const [error, setError] = useState('');

  const onScan = (data: string) => {
    const found = parseKeysLink(data);
    if (found) {
      setScanned(found);
      setError('');
    } else {
      setError('Това не е кодът от „genesis keys qr".');
    }
  };

  const save = async () => {
    if (!pairing || !keys) return;
    setSaving(true);
    setError('');
    try {
      const reply = await new GenesisClient(pairing, random).importKeys(keys);
      if (reply.ok && reply.saved?.length) {
        setSaved(reply.saved);
        setScanned({});   // the keys leave this screen's memory
      } else {
        setError('Genesis не прие ключовете.');
      }
    } catch (e) {
      const kind = e instanceof ProtocolError ? e.kind : 'network';
      setError(kind === 'network' ? 'Genesis не отговаря — пуснат ли е?' : 'Genesis отказа връзката. Сдвои отново.');
    } finally {
      setSaving(false);
    }
  };

  const names = keys ? Object.keys(keys) : [];
  const cameraOk = Platform.OS !== 'web' && permission?.granted;

  return (
    <SafeAreaView style={[styles.flex, { backgroundColor: theme.bg }]}>
      <ScrollView contentContainerStyle={styles.body}>
        <Text style={[styles.title, { color: theme.text }]}>Ключове от компютъра</Text>

        {saved ? (
          <View style={[styles.card, { backgroundColor: theme.surface, borderColor: theme.ok }]}>
            <Text style={[styles.cardTitle, { color: theme.text }]}>✓ Записани {saved.length} ключа</Text>
            <Text style={{ color: theme.muted, lineHeight: 20 }}>
              Genesis ги ползва от следващото съобщение. На компютъра натисни Enter — страницата с кода се трие.
            </Text>
            <Pressable accessibilityRole="button" onPress={() => router.replace('/chat')}
              style={[styles.primary, { backgroundColor: theme.accent }]}>
              <Text style={[styles.primaryText, { color: theme.accentText }]}>Към чата</Text>
            </Pressable>
          </View>
        ) : keys && names.length ? (
          <View style={[styles.card, { backgroundColor: theme.surface, borderColor: theme.border }]}>
            <Text style={[styles.cardTitle, { color: theme.text }]}>Идват {names.length} ключа</Text>
            {names.map((name) => (
              <View key={name} style={styles.row}>
                <Text style={[mono, styles.name, { color: theme.text }]}>{name}</Text>
                <Text style={[mono, styles.value, { color: theme.muted }]}>{maskKey(keys[name])}</Text>
              </View>
            ))}
            {pairing ? (
              <Pressable accessibilityRole="button" disabled={saving} onPress={save}
                style={[styles.primary, { backgroundColor: theme.accent, opacity: saving ? 0.6 : 1 }]}>
                {saving ? <ActivityIndicator color={theme.accentText} /> : (
                  <Text style={[styles.primaryText, { color: theme.accentText }]}>Запиши в Genesis ({pairing.name})</Text>
                )}
              </Pressable>
            ) : (
              <Text style={{ color: theme.warn, lineHeight: 20 }}>
                Първо свържи приложението с Genesis (на телефона: „Без компютър“), после сканирай кода пак.
              </Text>
            )}
          </View>
        ) : (
          <View style={[styles.card, { backgroundColor: theme.surface, borderColor: theme.border }]}>
            <Text style={[styles.cardTitle, { color: theme.text }]}>На компютъра</Text>
            <Text selectable style={[mono, styles.cmd, { backgroundColor: theme.code, color: theme.text }]}>genesis keys qr</Text>
            <Text style={{ color: theme.muted, lineHeight: 20 }}>
              Отваря QR код с ключовете, с които Genesis работи на компютъра (само работещите). Насочи камерата към него.
            </Text>
            {Platform.OS === 'web' ? null : cameraOk ? (
              <View style={styles.cameraBox}>
                <CameraView
                  style={StyleSheet.absoluteFill}
                  facing="back"
                  barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
                  onBarcodeScanned={({ data }) => onScan(data)}
                />
              </View>
            ) : (
              <Pressable accessibilityRole="button" onPress={requestPermission}
                style={[styles.primary, { backgroundColor: theme.accent }]}>
                <Text style={[styles.primaryText, { color: theme.accentText }]}>Разреши камерата</Text>
              </Pressable>
            )}
          </View>
        )}

        {error ? <Text style={[styles.error, { color: theme.danger }]}>{error}</Text> : null}

        {saved ? null : (
          <Pressable accessibilityRole="button" onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}>
            <Text style={[styles.back, { color: theme.accent }]}>Назад</Text>
          </Pressable>
        )}
        <Text style={[styles.foot, { color: theme.muted }]}>
          Ключовете отиват само до Genesis, по криптираната връзка, и се пазят в ~/.genesis/.env там. Приложението не ги помни.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  body: { padding: 20, gap: 16, maxWidth: 560, width: '100%', alignSelf: 'center' },
  title: { fontSize: 28, fontWeight: '800', marginTop: 12 },
  card: { borderRadius: 14, borderWidth: 1, padding: 14, gap: 10 },
  cardTitle: { fontSize: 17, fontWeight: '700' },
  row: { flexDirection: 'row', justifyContent: 'space-between', gap: 8 },
  name: { fontSize: 13, flexShrink: 1 },
  value: { fontSize: 13 },
  cmd: { fontSize: 15, padding: 10, borderRadius: 8, overflow: 'hidden' },
  cameraBox: { height: 300, borderRadius: 12, overflow: 'hidden' },
  primary: { borderRadius: 12, paddingVertical: 13, alignItems: 'center', marginTop: 4 },
  primaryText: { fontWeight: '700', fontSize: 16 },
  error: { fontSize: 14, lineHeight: 20 },
  back: { fontSize: 15, fontWeight: '600', paddingVertical: 4 },
  foot: { fontSize: 12, lineHeight: 17 },
});
