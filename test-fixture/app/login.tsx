// Login screen — exercises extract_screen_map.py's auth detection.

import React, { useState } from 'react';
import { View, Text, TextInput, Pressable, StyleSheet } from 'react-native';
import { router } from 'expo-router';

export default function LoginScreen() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  return (
    <View style={styles.root}>
      <Text style={styles.h1}>Sign in</Text>

      <Text style={styles.label}>Email</Text>
      <TextInput
        placeholder="Email"
        value={email}
        onChangeText={setEmail}
        autoCapitalize="none"
        style={styles.input}
      />

      <Text style={styles.label}>Password</Text>
      <TextInput
        placeholder="Password"
        value={password}
        onChangeText={setPassword}
        secureTextEntry
        style={styles.input}
      />

      <Pressable
        onPress={() => router.replace('/(tabs)')}
        style={styles.submit}
      >
        <Text style={styles.submitText}>Log in</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { padding: 16, gap: 8 },
  h1: { fontSize: 28, fontWeight: '700', marginBottom: 12 },
  label: { fontSize: 14, color: '#666' },
  input: { borderWidth: 1, borderColor: '#ccc', padding: 10, borderRadius: 8 },
  submit: { backgroundColor: '#222', padding: 14, borderRadius: 8, marginTop: 16, alignItems: 'center' },
  submitText: { color: 'white', fontWeight: '600' },
});
