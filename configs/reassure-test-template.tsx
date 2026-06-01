/**
 * reassure-test-template.tsx
 *
 * A template emitted by `scripts/gen_reassure_tests.py` for each detected
 * screen component. The generator substitutes the `{{TOKEN}}` placeholders
 * below and writes the resulting file under
 * `workspace/__reassure_tests__/<screen>.perf-test.tsx`.
 *
 * The template aims for "tests that at least try to render". Many real screens
 * will throw when rendered out-of-context — that throw becomes a Finding
 * (test_failure category) rather than aborting the audit. Per Stage 4c's
 * failure-handling rules.
 *
 * Placeholders (filled by the generator):
 *   {{COMPONENT_IMPORT_PATH}}   relative import to the screen module
 *   {{COMPONENT_NAME}}          imported identifier
 *   {{COMPONENT_PROPS_LITERAL}} a JSON object literal of best-guess mock props
 *   {{RUNS}}                    integer iteration count (default 10)
 */

import React from 'react';
import { View } from 'react-native';
import { measureRenders } from 'reassure';

// ─── Best-effort mocks for the most common Expo/React-Native ecosystem deps.
// If a screen pulls in something not mocked here, the test will throw and the
// audit will surface a `reassure.render_failure` Finding pointing at the file.

jest.mock('react-native-reanimated', () => {
  const Reanimated = require('react-native-reanimated/mock');
  Reanimated.default.call = () => {};
  return Reanimated;
});

jest.mock('react-native-gesture-handler', () => {
  const View = require('react-native').View;
  return {
    Swipeable: View,
    DrawerLayout: View,
    GestureHandlerRootView: View,
    PanGestureHandler: View,
    TapGestureHandler: View,
    State: {},
    Directions: {},
    gestureHandlerRootHOC: (c: any) => c,
  };
});

jest.mock('expo-router', () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn(), back: jest.fn() }),
  useLocalSearchParams: () => ({}),
  useGlobalSearchParams: () => ({}),
  useSegments: () => [],
  usePathname: () => '/',
  Stack: ({ children }: any) => children,
  Tabs: ({ children }: any) => children,
  Slot: ({ children }: any) => children,
  Link: ({ children }: any) => children,
  Redirect: () => null,
}));

jest.mock('@react-navigation/native', () => ({
  useNavigation: () => ({ navigate: jest.fn(), goBack: jest.fn() }),
  useRoute: () => ({ params: {} }),
  useFocusEffect: jest.fn(),
  NavigationContainer: ({ children }: any) => children,
}));

// Async storage mock — defaults to empty store.
jest.mock('@react-native-async-storage/async-storage', () => ({
  getItem: jest.fn(async () => null),
  setItem: jest.fn(async () => undefined),
  removeItem: jest.fn(async () => undefined),
  clear: jest.fn(async () => undefined),
}));

// Network-layer mock — every fetch resolves with an empty object. Adjust per
// screen by populating the mocked URL map in the generator.
global.fetch = jest.fn(async () =>
  ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }) as any
) as any;

// ─── Component under test ───────────────────────────────────────────────────

import {{COMPONENT_NAME}} from '{{COMPONENT_IMPORT_PATH}}';

const Wrapper = (props: any) => (
  <View style={{ flex: 1 }}>
    <{{COMPONENT_NAME}} {...props} />
  </View>
);

test('{{COMPONENT_NAME}} render perf', async () => {
  await measureRenders(<Wrapper {...{{COMPONENT_PROPS_LITERAL}}} />, {
    runs: {{RUNS}},
  });
});
