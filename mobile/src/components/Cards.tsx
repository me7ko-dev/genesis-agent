import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { ChatItem } from '../lib/chat';
import { mono, useTheme } from '../lib/theme';
import { RichText } from './RichText';

type Item<K extends ChatItem['kind']> = Extract<ChatItem, { kind: K }>;

export function UserBubble({ item }: { item: Item<'user'> }) {
  const theme = useTheme();
  return (
    <View style={[styles.user, { backgroundColor: theme.userBubble }]}>
      <Text selectable style={[styles.userText, { color: theme.userText }]}>{item.text}</Text>
    </View>
  );
}

export function AssistantMessage({ item }: { item: Item<'assistant'> }) {
  return (
    <View style={styles.assistant}>
      <RichText text={item.text} />
    </View>
  );
}

export function ToolCard({ item }: { item: Item<'tool'> }) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);
  const first = item.result.split('\n').find((l) => l.trim()) ?? '';
  return (
    <Pressable
      onPress={() => setOpen((o) => !o)}
      accessibilityRole="button"
      accessibilityLabel={`Инструмент ${item.name}, ${open ? 'свий' : 'разгъни'}`}
      style={[styles.tool, { backgroundColor: theme.surfaceAlt, borderColor: theme.border }]}>
      <Text style={[styles.toolName, { color: theme.muted }]}>
        {open ? '▾' : '▸'} 🔧 {item.name}
      </Text>
      <Text selectable style={[mono, styles.toolBody, { color: theme.text }]} numberOfLines={open ? undefined : 2}>
        {open ? item.result : first}
      </Text>
      {open && item.clipped ? (
        <Text style={[styles.small, { color: theme.muted }]}>… съкратено — пълното е на компютъра</Text>
      ) : null}
    </Pressable>
  );
}

export function Note({ item }: { item: Item<'note'> }) {
  const theme = useTheme();
  const color = item.tone === 'error' ? theme.danger : item.tone === 'info' ? theme.muted : theme.warn;
  const icon = item.tone === 'asked' ? '❓' : item.tone === 'error' ? '⛔' : item.tone === 'warn' ? '⚠️' : '';
  if (item.tone === 'asked') {
    return (
      <View style={[styles.asked, { borderColor: theme.warn, backgroundColor: theme.surface }]}>
        <Text style={[styles.askedTitle, { color: theme.warn }]}>❓ Genesis пита</Text>
        <RichText text={item.text} />
      </View>
    );
  }
  return <Text selectable style={[styles.note, { color }]}>{icon ? `${icon} ` : ''}{item.text}</Text>;
}

export function ConfirmCard({ item, onAnswer }: { item: Item<'confirm'>; onAnswer: (allow: boolean) => void }) {
  const theme = useTheme();
  const pending = item.state === 'pending';
  return (
    <View style={[styles.confirm, { backgroundColor: theme.surface, borderColor: pending ? theme.warn : theme.border }]}>
      <Text style={[styles.confirmTitle, { color: theme.warn }]}>⚠️ Изисква потвърждение</Text>
      {item.reasons.map((r, i) => (
        <Text key={i} style={[styles.reason, { color: theme.text }]}>• {r}</Text>
      ))}
      <Text selectable style={[mono, styles.op, { backgroundColor: theme.code, color: theme.text }]}>{item.operation}</Text>
      {pending ? (
        <View style={styles.row}>
          <Pressable
            accessibilityRole="button"
            onPress={() => onAnswer(false)}
            style={[styles.btn, { borderColor: theme.border }]}>
            <Text style={[styles.btnText, { color: theme.text }]}>Откажи</Text>
          </Pressable>
          <Pressable
            accessibilityRole="button"
            onPress={() => onAnswer(true)}
            style={[styles.btn, { backgroundColor: theme.danger, borderColor: theme.danger }]}>
            <Text style={[styles.btnText, { color: '#fff' }]}>Изпълни</Text>
          </Pressable>
        </View>
      ) : (
        <Text style={[styles.small, { color: item.state === 'allowed' ? theme.ok : theme.muted }]}>
          {item.state === 'allowed' ? '✓ Разрешено' : '✕ Отказано'}{item.note ? ` — ${item.note}` : ''}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  user: { alignSelf: 'flex-end', maxWidth: '85%', borderRadius: 18, borderBottomRightRadius: 4, paddingHorizontal: 14, paddingVertical: 9 },
  userText: { fontSize: 16, lineHeight: 22 },
  assistant: { paddingVertical: 2 },
  tool: { borderRadius: 10, borderWidth: StyleSheet.hairlineWidth, padding: 10, gap: 4 },
  toolName: { fontSize: 13, fontWeight: '600' },
  toolBody: { fontSize: 12, lineHeight: 17 },
  small: { fontSize: 12 },
  note: { fontSize: 13, lineHeight: 18 },
  asked: { borderRadius: 12, borderWidth: 1, padding: 12, gap: 6 },
  askedTitle: { fontWeight: '700' },
  confirm: { borderRadius: 12, borderWidth: 1, padding: 12, gap: 8 },
  confirmTitle: { fontWeight: '700', fontSize: 15 },
  reason: { fontSize: 14 },
  op: { fontSize: 12, padding: 8, borderRadius: 6, overflow: 'hidden' },
  row: { flexDirection: 'row', gap: 10, justifyContent: 'flex-end' },
  btn: { borderWidth: 1, borderRadius: 10, paddingHorizontal: 18, paddingVertical: 10, minWidth: 100, alignItems: 'center' },
  btnText: { fontWeight: '600', fontSize: 15 },
});
