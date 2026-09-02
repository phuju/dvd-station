import AsyncStorage from '@react-native-async-storage/async-storage';

// AsyncStorage can throw (private mode, quota, native bridge) — never let that
// crash the app; callers get null / a no-op.
export async function get(key: string): Promise<string | null> {
  try {
    return await AsyncStorage.getItem(key);
  } catch {
    return null;
  }
}

export async function set(key: string, value: string): Promise<void> {
  try {
    await AsyncStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}
