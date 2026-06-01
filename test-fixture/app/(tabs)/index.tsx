// Test-fixture screen: contains intentional performance anti-patterns.
// Every anti-pattern below is a planted positive for the corresponding rule
// in the mobile-perf-audit pipeline. DO NOT "fix" them — the fixture exists
// so the audit's regression tests stay honest.

import React, { useEffect, useState } from 'react';
import {
  Animated,            // PLANT: static.animated_api_usage (RN Animated, not Reanimated)
  Image,               // PLANT: static.image_without_caching (RN Image, not expo-image)
  ScrollView,
  Text,
  TextInput,
  View,
  FlatList,
  StyleSheet,
} from 'react-native';
import _ from 'lodash';   // PLANT: bundle.known_bloated_dependency

const ITEMS = Array.from({ length: 100 }, (_v, i) => ({
  id: String(i),
  title: `Item ${i}`,
  imageUri: `https://placekitten.com/200/200?image=${i % 16}`,
}));

export default function FeedScreen() {
  const [query, setQuery] = useState('');

  // PLANT: static.useeffect_no_deps — no dependency array.
  useEffect(() => {
    console.log('FeedScreen render side-effect');  // PLANT: static.console_log_in_production_code
  });

  // PLANT: static.useeffect_missing_cleanup — setInterval started, no cleanup return.
  useEffect(() => {
    const id = setInterval(() => {
      setQuery((q) => q);   // forces a re-render; in real code this is the leak vector
    }, 5000);
    // Missing: return () => clearInterval(id);
  }, []);

  // Use lodash for one trivial thing — exercises bundle.known_bloated_dependency
  const filtered = _.filter(ITEMS, (it) => it.title.toLowerCase().includes(query.toLowerCase()));

  return (
    // PLANT: static.scrollview_with_long_list — unbounded .map() inside <ScrollView>
    <ScrollView
      // PLANT: static.inline_object_props — inline object literal on a style prop
      contentContainerStyle={{ paddingTop: 16, paddingHorizontal: 12 }}
    >
      <Text style={styles.heading}>Feed</Text>

      <TextInput
        placeholder="Search"
        value={query}
        onChangeText={setQuery}
        // PLANT: another inline_object_props instance (style)
        style={{ height: 40, borderWidth: 1, marginBottom: 12, paddingHorizontal: 8 }}
      />

      {filtered.map((item) => (
        <View key={item.id} style={styles.row}>
          {/* PLANT: static.image_without_caching — remote URI via RN <Image> */}
          <Image source={{ uri: item.imageUri }} style={styles.thumb} />
          <Text>{item.title}</Text>
        </View>
      ))}

      {/* A second list, this time as FlatList with an inline-arrow renderItem.
          PLANT: static.inline_arrow_in_renderitem. */}
      <FlatList
        data={ITEMS.slice(0, 20)}
        keyExtractor={(it) => `mini-${it.id}`}
        renderItem={({ item }) => (
          <View style={styles.row}>
            <Text>{item.title}</Text>
          </View>
        )}
      />

      <Animated.View style={styles.spacer} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  heading: { fontSize: 28, fontWeight: '700', marginBottom: 12 },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8 },
  thumb: { width: 48, height: 48, marginRight: 12, borderRadius: 8 },
  spacer: { height: 48 },
});
