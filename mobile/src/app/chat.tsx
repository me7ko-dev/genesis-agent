import { Redirect, router } from 'expo-router';
import { useRef, useState } from 'react';
import {
  ActivityIndicator, FlatList, KeyboardAvoidingView, type NativeScrollEvent, type NativeSyntheticEvent, Platform, Pressable, StyleSheet, Text, TextInput, View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { AssistantMessage, ConfirmCard, Note, ToolCard, UserBubble } from '../components/Cards';
import type { ChatItem } from '../lib/chat';
import { usePairing } from '../lib/pairing';
import type { Pairing } from '../lib/protocol';
import { useTheme } from '../lib/theme';
import { useGenesis, type Connection } from '../lib/useGenesis';

export default function ChatRoute() {
  const { pairing, loaded } = usePairing();
  if (!loaded) return null;
  if (!pairing) return <Redirect href="/pair" />;
  return <Chat pairing={pairing} />;
}

const CONNECTION_TEXT: Record<Connection, string> = {
  connecting: 'свързване…',
  online: 'на линия',
  offline: 'няма връзка — опитвам пак',
  unauthorized: 'ключът не пасва',
  clock: 'часовникът е разминат',
};

function Chat({ pairing }: { pairing: Pairing }) {
  const theme = useTheme();
  const { unpair } = usePairing();
  const g = useGenesis(pairing);
  const [draft, setDraft] = useState('');
  const list = useRef<FlatList<ChatItem>>(null);

  // Follow new output only while the reader is at the bottom: scrolling up to
  // read something must not be yanked back by the next tool result.
  const atBottom = useRef(true);
  const onScroll = (e: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
    atBottom.current = contentOffset.y + layoutMeasurement.height >= contentSize.height - 80;
  };
  const follow = () => {
    if (atBottom.current) list.current?.scrollToEnd({ animated: true });
  };

  const submit = async () => {
    const text = draft.trim();
    if (!text || g.busy) return;
    atBottom.current = true;
    if (await g.send(text)) setDraft('');
  };

  const [menuOpen, setMenuOpen] = useState(false);
  const doUnpair = async () => {
    setMenuOpen(false);
    await unpair();
    router.replace('/pair');
  };

  const dot = g.connection === 'online' ? theme.ok : g.connection === 'connecting' ? theme.muted : theme.danger;

  return (
    <SafeAreaView style={[styles.flex, { backgroundColor: theme.bg }]} edges={['top', 'left', 'right', 'bottom']}>
      <View style={[styles.header, { borderColor: theme.border }]}>
        <View style={styles.flex}>
          <Text style={[styles.hTitle, { color: theme.text }]} numberOfLines={1}>{pairing.name}</Text>
          <View style={styles.hSub}>
            <View style={[styles.dot, { backgroundColor: dot }]} />
            <Text style={[styles.hSubText, { color: theme.muted }]} numberOfLines={1}>
              {CONNECTION_TEXT[g.connection]}{g.status?.model ? ` · ${g.status.model}` : ''}
            </Text>
          </View>
        </View>
        <Pressable accessibilityRole="button" accessibilityLabel="Меню" onPress={() => setMenuOpen((o) => !o)} hitSlop={12} style={styles.menuBtn}>
          <Text style={[styles.menuText, { color: theme.text }]}>⋯</Text>
        </Pressable>
      </View>

      {menuOpen ? (
        <View style={[styles.menu, { backgroundColor: theme.surface, borderColor: theme.border }]}>
          {g.status?.workspace ? (
            <Text style={[styles.menuInfo, { color: theme.muted }]} numberOfLines={2}>📁 {g.status.workspace}</Text>
          ) : null}
          <MenuItem label="Нов разговор" onPress={() => { setMenuOpen(false); g.clear(); }} />
          <MenuItem label="Отдвои този телефон" danger onPress={doUnpair} />
        </View>
      ) : null}

      {g.connection === 'unauthorized' ? (
        <Banner tone="danger" text="Компютърът вече не приема този телефон (нов ключ или друг компютър на адреса). Сдвои отново."
          action="Сдвои" onPress={async () => { await unpair(); router.replace('/pair'); }} />
      ) : g.connection === 'clock' ? (
        <Banner tone="warn" text="Часовниците на телефона и компютъра се разминават с над 5 минути — включи автоматичния час." />
      ) : null}

      <KeyboardAvoidingView style={styles.flex} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <FlatList
          ref={list}
          data={g.items}
          keyExtractor={(item) => item.key}
          contentContainerStyle={styles.listBody}
          keyboardDismissMode="interactive"
          onScroll={onScroll}
          scrollEventThrottle={100}
          onContentSizeChange={follow}
          ListEmptyComponent={
            <View style={styles.empty}>
              <Text style={[styles.emptyTitle, { color: theme.text }]}>С какво да помогна?</Text>
              <Text style={[styles.emptyText, { color: theme.muted }]}>
                {g.status?.workspace ? `Работя в ${g.status.workspace}` : 'Пиши като в терминала — командите се изпълняват на компютъра.'}
              </Text>
            </View>
          }
          renderItem={({ item }) => {
            switch (item.kind) {
              case 'user': return <UserBubble item={item} />;
              case 'assistant': return <AssistantMessage item={item} />;
              case 'tool': return <ToolCard item={item} />;
              case 'note': return <Note item={item} />;
              case 'confirm': return <ConfirmCard item={item} onAnswer={(allow) => g.confirm(item.id, allow)} />;
            }
          }}
          ListFooterComponent={
            g.busy ? (
              <View style={styles.thinking}>
                <ActivityIndicator size="small" color={theme.accent} />
                <Text style={[styles.thinkingText, { color: theme.muted }]}>{g.label}</Text>
              </View>
            ) : null
          }
        />

        {g.error ? <Text style={[styles.error, { color: theme.danger }]}>{g.error}</Text> : null}

        <View style={[styles.composer, { borderColor: theme.border, backgroundColor: theme.surface }]}>
          <TextInput
            value={draft}
            onChangeText={setDraft}
            placeholder={g.busy ? 'Genesis работи…' : 'Съобщение към Genesis'}
            placeholderTextColor={theme.muted}
            multiline
            style={[styles.input, { color: theme.text }]}
            onSubmitEditing={Platform.OS === 'web' ? submit : undefined}
            blurOnSubmit={false}
            accessibilityLabel="Съобщение"
          />
          {g.busy ? (
            <Pressable accessibilityRole="button" accessibilityLabel="Спри" onPress={() => g.stop()}
              style={[styles.send, { backgroundColor: theme.danger }]}>
              <Text style={styles.sendText}>■</Text>
            </Pressable>
          ) : (
            <Pressable accessibilityRole="button" accessibilityLabel="Изпрати" onPress={submit}
              disabled={!draft.trim() || g.connection !== 'online'}
              style={[styles.send, { backgroundColor: theme.accent, opacity: draft.trim() && g.connection === 'online' ? 1 : 0.4 }]}>
              <Text style={[styles.sendText, { color: theme.accentText }]}>↑</Text>
            </Pressable>
          )}
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function MenuItem({ label, onPress, danger }: { label: string; onPress: () => void; danger?: boolean }) {
  const theme = useTheme();
  return (
    <Pressable accessibilityRole="button" onPress={onPress} style={styles.menuItem}>
      <Text style={[styles.menuItemText, { color: danger ? theme.danger : theme.text }]}>{label}</Text>
    </Pressable>
  );
}

function Banner({ text, tone, action, onPress }: { text: string; tone: 'warn' | 'danger'; action?: string; onPress?: () => void }) {
  const theme = useTheme();
  const color = tone === 'danger' ? theme.danger : theme.warn;
  return (
    <View style={[styles.banner, { borderColor: color, backgroundColor: theme.surface }]}>
      <Text style={[styles.bannerText, { color: theme.text }]}>{text}</Text>
      {action ? (
        <Pressable accessibilityRole="button" onPress={onPress}>
          <Text style={[styles.bannerAction, { color }]}>{action}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  header: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 16, paddingVertical: 10, borderBottomWidth: StyleSheet.hairlineWidth },
  hTitle: { fontSize: 17, fontWeight: '700' },
  hSub: { flexDirection: 'row', alignItems: 'center', gap: 6, marginTop: 2 },
  hSubText: { fontSize: 12, flexShrink: 1 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  menuBtn: { paddingHorizontal: 8 },
  menuText: { fontSize: 24, fontWeight: '700' },
  listBody: { padding: 16, gap: 12, flexGrow: 1 },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 8, paddingTop: 80 },
  emptyTitle: { fontSize: 20, fontWeight: '700' },
  emptyText: { fontSize: 14, textAlign: 'center', paddingHorizontal: 24 },
  thinking: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingVertical: 6 },
  thinkingText: { fontSize: 14 },
  error: { paddingHorizontal: 16, paddingBottom: 6, fontSize: 13 },
  composer: { flexDirection: 'row', alignItems: 'flex-end', gap: 8, margin: 10, marginTop: 0, padding: 6, paddingLeft: 14, borderRadius: 22, borderWidth: StyleSheet.hairlineWidth },
  input: { flex: 1, fontSize: 16, maxHeight: 140, paddingTop: 8, paddingBottom: 8 },
  send: { width: 36, height: 36, borderRadius: 18, alignItems: 'center', justifyContent: 'center' },
  sendText: { color: '#fff', fontSize: 18, fontWeight: '800' },
  menu: { marginHorizontal: 12, marginTop: 8, borderRadius: 12, borderWidth: StyleSheet.hairlineWidth, paddingVertical: 4 },
  menuInfo: { fontSize: 12, paddingHorizontal: 14, paddingVertical: 8 },
  menuItem: { paddingHorizontal: 14, paddingVertical: 12 },
  menuItemText: { fontSize: 16 },
  banner: { margin: 12, marginBottom: 0, padding: 12, borderRadius: 12, borderWidth: 1, gap: 8 },
  bannerText: { fontSize: 14, lineHeight: 19 },
  bannerAction: { fontWeight: '700' },
});
