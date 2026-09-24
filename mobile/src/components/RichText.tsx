import { Fragment } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { splitBlocks, splitInline } from '../lib/markdown';
import { mono, useTheme } from '../lib/theme';

/** The subset of Markdown models actually write: fences, headings, bullets, bold, inline code. */
export function RichText({ text, color }: { text: string; color?: string }) {
  const theme = useTheme();
  const fg = color ?? theme.text;
  return (
    <View style={styles.wrap}>
      {splitBlocks(text).map((block, i) =>
        block.code ? (
          <Text key={i} selectable style={[styles.code, mono, { backgroundColor: theme.code, color: fg }]}>
            {block.text}
          </Text>
        ) : (
          <Text key={i} selectable style={[styles.p, { color: fg }]}>
            {block.text.split('\n').map((raw, j, lines) => {
              const heading = /^#{1,6}\s+/.test(raw);
              const line = raw.replace(/^#{1,6}\s+/, '').replace(/^(\s*)[-*]\s+/, '$1• ');
              return (
                <Fragment key={j}>
                  {splitInline(line).map((seg, k) => (
                    <Text
                      key={k}
                      style={[
                        (seg.bold || heading) && styles.bold,
                        heading && styles.h,
                        seg.code && [mono, styles.inline, { backgroundColor: theme.code }],
                      ]}>
                      {seg.t}
                    </Text>
                  ))}
                  {j < lines.length - 1 ? '\n' : ''}
                </Fragment>
              );
            })}
          </Text>
        ),
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { gap: 8 },
  p: { fontSize: 16, lineHeight: 23 },
  bold: { fontWeight: '700' },
  h: { fontSize: 17 },
  inline: { fontSize: 14 },
  code: { fontSize: 13, lineHeight: 18, padding: 10, borderRadius: 8, overflow: 'hidden' },
});
