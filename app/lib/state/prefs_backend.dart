/// Where the UI preferences document is actually stored.
///
/// Kept as a tiny seam rather than calling `shared_preferences` directly
/// from [UiPrefs] -- that is what lets tests use an in-memory backend and
/// what would let a future "sync across devices" feature add a backend
/// that talks to the hub instead, with nothing above this file changing.
library;

import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// The single key the whole preferences document is stored under. One key,
/// not one per preference: that is what makes a single read at startup, a
/// single debounced write, and "reset everything" all free.
const String kUiPrefsStorageKey = 'harmony_hub.ui';

abstract class PrefsBackend {
  /// The raw stored document, or null if nothing has been saved yet.
  Future<String?> read();

  /// Replaces the stored document.
  Future<void> write(String json);
}

/// The real backend: `shared_preferences`, which is localStorage on web and
/// each platform's native prefs store elsewhere. Never throws -- a storage
/// failure (Safari private mode, a full quota) is reported to the caller as
/// an empty read / a swallowed write, not an exception.
class SharedPrefsBackend implements PrefsBackend {
  const SharedPrefsBackend();

  @override
  Future<String?> read() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      return prefs.getString(kUiPrefsStorageKey);
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> write(String json) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(kUiPrefsStorageKey, json);
    } catch (_) {
      // Swallowed on purpose -- see UiPrefs.degraded, which is how the
      // caller finds out without every write site having to check.
    }
  }
}

/// An in-process backend for tests and for `UiPrefs.memory()`. Optionally
/// seeded with the *decoded values* a test wants pre-populated, so a test
/// does not have to hand-construct the versioned document shape itself.
class MemoryPrefsBackend implements PrefsBackend {
  MemoryPrefsBackend([Map<String, dynamic>? seedValues])
      : _stored = seedValues == null
            ? null
            : jsonEncode({'v': 1, 'values': seedValues});

  String? _stored;

  /// Set by [failing] to make every call throw, for testing that failures
  /// degrade gracefully instead of propagating.
  bool shouldFail = false;

  factory MemoryPrefsBackend.failing() => MemoryPrefsBackend()..shouldFail = true;

  @override
  Future<String?> read() async {
    if (shouldFail) throw StateError('backend unavailable');
    return _stored;
  }

  @override
  Future<void> write(String json) async {
    if (shouldFail) throw StateError('backend unavailable');
    _stored = json;
  }
}
