/// The persisted UI preferences layer.
///
/// The point of `UiPrefs` is that every failure mode -- corrupt storage, a
/// backend that throws, a value that no longer decodes -- falls back to
/// defaults instead of ever propagating. These tests lean on that: a screen
/// built on top of this never needs its own try/catch around a preference
/// read.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:harmony_hub_app/state/prefs_backend.dart';
import 'package:harmony_hub_app/state/ui_prefs.dart';

enum _Mode {
  full,
  remoteFirst;

  static _Mode fromJson(Object json) =>
      values.firstWhere((m) => m.name == json, orElse: () => full);
}

// `const Pref(...)` only accepts constant expressions, so `encode` has to be
// a top-level/static function tear-off rather than an inline closure -- the
// same constraint a real enum-valued Pref (e.g. a future display mode) would
// need to follow.
Object _modeToJson(_Mode m) => m.name;

const _kMode = Pref<_Mode>('display.mode', _Mode.full,
    decode: _Mode.fromJson, encode: _modeToJson);

/// Wraps a backend and counts how many times [write] actually lands, so the
/// debounce tests can assert on write *count*, not just final content.
class _CountingBackend implements PrefsBackend {
  _CountingBackend(this._inner);

  final PrefsBackend _inner;
  int writeCount = 0;

  @override
  Future<String?> read() => _inner.read();

  @override
  Future<void> write(String json) async {
    writeCount++;
    await _inner.write(json);
  }
}

void main() {
  group('get', () {
    test('returns the default for a preference that was never set', () {
      final prefs = UiPrefs.memory();
      expect(prefs.get(kShellTab), 'live');
      expect(prefs.get(kDevicesQuery), '');
    });

    test('round-trips a value through set, including an encoded enum', () {
      final prefs = UiPrefs.memory();
      prefs.set(kShellTab, 'devices');
      expect(prefs.get(kShellTab), 'devices');

      prefs.set(_kMode, _Mode.remoteFirst);
      expect(prefs.get(_kMode), _Mode.remoteFirst);
    });
  });

  group('resilience', () {
    test('a stored value of the wrong type falls back to the default', () {
      // shell.tab is meant to be a String; a number could only get there
      // from a future build storing something under the same key.
      final prefs = UiPrefs.memory({'shell.tab': 42});
      expect(prefs.get(kShellTab), 'live');
    });

    test('an unparseable stored document degrades to defaults, not a throw', () async {
      final backend = MemoryPrefsBackend();
      await backend.write('not json{{{');
      final prefs = await UiPrefs.open(backend: backend);
      expect(prefs.get(kShellTab), 'live');
      expect(prefs.degraded, isTrue);
    });

    test('a stored document that is not a map is ignored, not thrown on', () async {
      final backend = MemoryPrefsBackend();
      await backend.write(jsonEncode([1, 2, 3]));
      final prefs = await UiPrefs.open(backend: backend);
      expect(prefs.get(kShellTab), 'live');
    });

    test('an unrecognised document version starts fresh rather than guessing', () async {
      final backend = MemoryPrefsBackend();
      await backend.write(jsonEncode({
        'v': 99,
        'values': {'shell.tab': 'devices'},
      }));
      final prefs = await UiPrefs.open(backend: backend);
      expect(prefs.get(kShellTab), 'live');
    });

    test('a backend that throws on read degrades instead of propagating', () async {
      final prefs = await UiPrefs.open(backend: MemoryPrefsBackend.failing());
      expect(prefs.get(kShellTab), 'live');
      expect(prefs.degraded, isTrue);
    });

    test('a backend that throws on write degrades instead of propagating', () async {
      final backend = MemoryPrefsBackend.failing();
      final prefs = await UiPrefs.open(backend: backend);
      prefs.set(kShellTab, 'devices');
      await prefs.flush();
      expect(prefs.degraded, isTrue);
    });
  });

  group('storage document', () {
    test('preserves keys this build does not recognise across a write', () async {
      final backend = MemoryPrefsBackend();
      await backend.write(jsonEncode({
        'v': 1,
        'values': {'future.feature': 'kept', 'shell.tab': 'scenes'},
      }));
      final prefs = await UiPrefs.open(backend: backend);

      prefs.set(kDevicesQuery, 'denon');
      await prefs.flush();

      final stored = jsonDecode((await backend.read())!) as Map;
      final values = (stored['values'] as Map).cast<String, dynamic>();
      expect(values['future.feature'], 'kept');
      expect(values['shell.tab'], 'scenes');
      expect(values['devices.query'], 'denon');
    });
  });

  group('debounced writes', () {
    test('a burst of set calls reaches the backend once, after the debounce', () async {
      final backend = _CountingBackend(MemoryPrefsBackend());
      final prefs = await UiPrefs.open(backend: backend);

      prefs.set(kDevicesQuery, 'd');
      prefs.set(kDevicesQuery, 'de');
      prefs.set(kDevicesQuery, 'den');
      prefs.set(kDevicesQuery, 'deno');
      prefs.set(kDevicesQuery, 'denon');

      // Nothing has reached the backend yet -- still within the debounce.
      expect(backend.writeCount, 0);

      await Future<void>.delayed(const Duration(milliseconds: 400));

      expect(backend.writeCount, 1);
      final stored = jsonDecode((await backend.read())!) as Map;
      expect((stored['values'] as Map)['devices.query'], 'denon');
    });

    test('flush forces a pending write out immediately', () async {
      final backend = _CountingBackend(MemoryPrefsBackend());
      final prefs = await UiPrefs.open(backend: backend);

      prefs.set(kScenesQuery, 'watch');
      expect(backend.writeCount, 0);

      await prefs.flush();

      expect(backend.writeCount, 1);
      final stored = jsonDecode((await backend.read())!) as Map;
      expect((stored['values'] as Map)['scenes.query'], 'watch');
    });

    test('flush is a no-op when nothing changed since the last write', () async {
      final backend = _CountingBackend(MemoryPrefsBackend());
      final prefs = await UiPrefs.open(backend: backend);

      await prefs.flush();
      expect(backend.writeCount, 0);

      prefs.set(kScenesQuery, 'watch');
      await prefs.flush();
      expect(backend.writeCount, 1);

      await prefs.flush();
      expect(backend.writeCount, 1);
    });

    test('setting the same value again does not schedule a write', () {
      final prefs = UiPrefs.memory({'devices.query': 'denon'});
      var notified = 0;
      prefs.addListener(() => notified++);

      prefs.set(kDevicesQuery, 'denon');

      expect(notified, 0);
    });
  });

  group('reset', () {
    test('reset drops one preference back to its default', () {
      final prefs = UiPrefs.memory({'shell.tab': 'devices', 'devices.query': 'tv'});
      prefs.reset(kShellTab);
      expect(prefs.get(kShellTab), 'live');
      expect(prefs.get(kDevicesQuery), 'tv');
    });

    test('resetAll clears every preference and notifies', () {
      final prefs = UiPrefs.memory({'shell.tab': 'devices', 'devices.query': 'tv'});
      var notified = false;
      prefs.addListener(() => notified = true);

      prefs.resetAll();

      expect(prefs.get(kShellTab), 'live');
      expect(prefs.get(kDevicesQuery), '');
      expect(notified, isTrue);
    });
  });

  group('remember where I was', () {
    test('turning it off makes reads fall back to defaults', () {
      final prefs = UiPrefs.memory({'shell.tab': 'devices'});
      expect(prefs.get(kShellTab), 'devices');

      prefs.set(kRememberEnabled, false);

      expect(prefs.get(kShellTab), 'live');
    });

    test('turning it off makes set a no-op for everything else', () {
      final prefs = UiPrefs.memory();
      prefs.set(kRememberEnabled, false);

      prefs.set(kShellTab, 'scenes');

      expect(prefs.get(kShellTab), 'live');
    });

    test('the switch itself always persists, and old values resurface when re-enabled', () {
      final prefs = UiPrefs.memory({'shell.tab': 'devices'});
      prefs.set(kRememberEnabled, false);
      expect(prefs.get(kRememberEnabled), isFalse);

      prefs.set(kRememberEnabled, true);

      expect(prefs.get(kShellTab), 'devices');
    });
  });
}
