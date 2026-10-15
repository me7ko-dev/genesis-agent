import { ActivityIndicator, Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { formatClock, MAX_RECORDING_MS } from '../lib/media';
import type { Attachment } from '../lib/photos';
import { useTheme } from '../lib/theme';

/** Снимките над полето за писане, с × за махане (2026-10-15). */
export function AttachmentStrip({ images, preparing, onRemove }: {
  images: Attachment[];
  preparing: boolean;
  onRemove: (id: string) => void;
}) {
  const theme = useTheme();
  if (!images.length && !preparing) return null;
  return (
    <ScrollView horizontal keyboardShouldPersistTaps="handled" contentContainerStyle={styles.strip} style={styles.stripBox}>
      {images.map((image) => (
        <View key={image.id} style={[styles.thumb, { borderColor: theme.border, backgroundColor: theme.surfaceAlt }]}>
          <Image source={{ uri: image.uri }} style={styles.thumbImage} accessibilityLabel={`Снимка ${image.name}`} />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Махни ${image.name}`}
            onPress={() => onRemove(image.id)}
            hitSlop={8}
            style={styles.remove}>
            <Text style={styles.removeText}>×</Text>
          </Pressable>
        </View>
      ))}
      {preparing ? (
        <View style={[styles.thumb, styles.center, { borderColor: theme.border, backgroundColor: theme.surfaceAlt }]}>
          <ActivityIndicator size="small" color={theme.accent} />
        </View>
      ) : null}
    </ScrollView>
  );
}

/** „Снимай“ / „От галерията“ — над полето, докато е отворено. */
export function AttachMenu({ left, onCamera, onLibrary }: { left: number; onCamera: () => void; onLibrary: () => void }) {
  const theme = useTheme();
  return (
    <View style={[styles.attachMenu, { backgroundColor: theme.surface, borderColor: theme.border }]}>
      <Pressable accessibilityRole="button" onPress={onCamera} style={styles.attachItem}>
        <Text style={[styles.attachText, { color: theme.text }]}>📷 Снимай</Text>
      </Pressable>
      <Pressable accessibilityRole="button" onPress={onLibrary} style={styles.attachItem}>
        <Text style={[styles.attachText, { color: theme.text }]}>🖼 От галерията</Text>
      </Pressable>
      <Text style={[styles.attachHint, { color: theme.muted }]}>още {left}</Text>
    </View>
  );
}

/** На мястото на полето, докато микрофонът записва: време и „Откажи“. */
export function RecordingBar({ durationMs, onCancel }: { durationMs: number; onCancel: () => void }) {
  const theme = useTheme();
  return (
    <View style={styles.recording} accessibilityLiveRegion="polite">
      <View style={[styles.recDot, { backgroundColor: theme.danger }]} />
      <Text style={[styles.recText, { color: theme.text }]}>
        {formatClock(durationMs)} / {formatClock(MAX_RECORDING_MS)}
      </Text>
      <Pressable accessibilityRole="button" accessibilityLabel="Откажи записа" onPress={onCancel} hitSlop={8}>
        <Text style={[styles.recCancel, { color: theme.muted }]}>✕ Откажи</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  stripBox: { flexGrow: 0 },
  strip: { gap: 8, paddingHorizontal: 12, paddingBottom: 8 },
  thumb: { width: 60, height: 60, borderRadius: 10, borderWidth: StyleSheet.hairlineWidth, overflow: 'hidden' },
  thumbImage: { width: '100%', height: '100%' },
  center: { alignItems: 'center', justifyContent: 'center' },
  remove: { position: 'absolute', top: 2, right: 2, width: 20, height: 20, borderRadius: 10, backgroundColor: 'rgba(0,0,0,0.6)', alignItems: 'center', justifyContent: 'center' },
  removeText: { color: '#fff', fontSize: 15, fontWeight: '800', lineHeight: 17 },
  attachMenu: { flexDirection: 'row', alignItems: 'center', gap: 4, marginHorizontal: 10, marginBottom: 8, borderRadius: 12, borderWidth: StyleSheet.hairlineWidth, paddingHorizontal: 4 },
  attachItem: { paddingHorizontal: 12, paddingVertical: 10 },
  attachText: { fontSize: 15 },
  attachHint: { marginLeft: 'auto', paddingRight: 10, fontSize: 12 },
  recording: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: 8, minHeight: 36 },
  recDot: { width: 10, height: 10, borderRadius: 5 },
  recText: { fontSize: 16, fontVariant: ['tabular-nums'] },
  recCancel: { fontSize: 14, marginLeft: 8 },
});
