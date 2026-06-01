// Profile screen — second, larger anti-pattern: a >100-line function
// component with no React.memo wrapper.

import React, { useState } from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';

// PLANT: static.large_unmemoized_component — function component >100 LOC,
// no memo, with JSX.
export default function ProfileScreen() {
  const [count, setCount] = useState(0);
  const [bio, setBio] = useState('A short bio that just exists so the file is long enough.');
  const [favoriteColor, setFavoriteColor] = useState<'red' | 'green' | 'blue'>('blue');

  // intentionally bloated body to trip the length threshold
  const colorPickerRows = [
    'red', 'green', 'blue', 'purple', 'orange', 'yellow',
    'pink', 'teal', 'cyan', 'magenta', 'brown', 'gray',
  ];

  return (
    <View style={styles.root}>
      <Text style={styles.h1}>Profile</Text>

      <Text style={styles.label}>Bio</Text>
      <Text style={styles.body}>{bio}</Text>

      <Text style={styles.label}>Counter</Text>
      <Text style={styles.body}>{count}</Text>

      <View style={styles.row}>
        <Pressable
          onPress={() => setCount((c) => c + 1)}
          style={styles.btn}
        >
          <Text>+1</Text>
        </Pressable>
        <Pressable
          onPress={() => setCount((c) => Math.max(0, c - 1))}
          style={styles.btn}
        >
          <Text>-1</Text>
        </Pressable>
        <Pressable
          onPress={() => setCount(0)}
          style={styles.btn}
        >
          <Text>reset</Text>
        </Pressable>
      </View>

      <Text style={styles.label}>Favorite color</Text>
      <View style={styles.colorRow}>
        {colorPickerRows.map((c) => (
          <Pressable
            key={c}
            onPress={() => setFavoriteColor(c as any)}
            style={[styles.colorChip, { backgroundColor: c }]}
          >
            <Text style={styles.chipText}>{c}</Text>
          </Pressable>
        ))}
      </View>

      <Text style={styles.label}>About</Text>
      <Text style={styles.body}>
        This profile screen is intentionally long. The point is to give the
        large_unmemoized_component AST rule something to match against in the
        test fixture. The rule's threshold is 100 lines; the body therefore
        contains enough boilerplate JSX + small handlers to clear that bar
        without doing anything genuinely interesting.
      </Text>

      <Text style={styles.label}>FAQ</Text>
      <Text style={styles.body}>Q. Why does this exist?</Text>
      <Text style={styles.body}>
        A. So that whenever a new contributor edits the AST rule, the fixture
        immediately surfaces whether the rule still fires or whether it has
        regressed. Without a fixture, audit pipelines drift silently.
      </Text>
      <Text style={styles.body}>Q. Why aren't there more comments?</Text>
      <Text style={styles.body}>
        A. The rule operates on AST shape, not on file-level comment density.
        We pad the body with normal-looking JSX so the line count matches what
        a real, modestly-large screen would look like.
      </Text>

      <Text style={styles.label}>Settings</Text>
      <Pressable onPress={() => setBio('Edited at ' + new Date().toISOString())} style={styles.btn}>
        <Text>Edit bio</Text>
      </Pressable>
      <Pressable onPress={() => setBio('A short bio that just exists so the file is long enough.')} style={styles.btn}>
        <Text>Reset bio</Text>
      </Pressable>

      <Text style={styles.label}>Selected color</Text>
      <View style={[styles.colorPreview, { backgroundColor: favoriteColor }]} />

      <Text style={styles.label}>Recent activity</Text>
      <View style={styles.row}>
        <Text style={styles.body}>Liked a post</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>Commented on a thread</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>Followed a new user</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>Saved a draft</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>Updated profile picture</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>Joined a community</Text>
      </View>

      <Text style={styles.label}>Achievements</Text>
      <View style={styles.row}>
        <Text style={styles.body}>First post made</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>100 likes received</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>One-month streak</Text>
      </View>
      <View style={styles.row}>
        <Text style={styles.body}>Top contributor of the week</Text>
      </View>

      <Text style={styles.label}>Linked accounts</Text>
      <Pressable style={styles.btn}><Text>GitHub</Text></Pressable>
      <Pressable style={styles.btn}><Text>X / Twitter</Text></Pressable>
      <Pressable style={styles.btn}><Text>LinkedIn</Text></Pressable>

      <Text style={styles.body}>End of profile.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { padding: 16, gap: 8 },
  h1: { fontSize: 26, fontWeight: '700' },
  label: { fontSize: 14, color: '#666', marginTop: 12 },
  body: { fontSize: 16 },
  row: { flexDirection: 'row', gap: 8, marginTop: 8 },
  btn: { padding: 10, borderRadius: 8, backgroundColor: '#eee' },
  colorRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 8 },
  colorChip: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8 },
  chipText: { color: 'white', fontWeight: '600' },
  colorPreview: { width: 64, height: 64, borderRadius: 8, marginTop: 8 },
});
