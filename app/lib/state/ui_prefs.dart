/// Device-local UI preferences: last tab, filters, and anything else a
/// screen wants to survive a reload.
///
/// Deliberately separate from [HubStore] (`hub_store.dart`): that class
/// holds the hub's own state, shared by every browser pointed at it and
/// gone the moment the hub says so. This holds *this browser's* idea of
/// where it was, which must keep working even when the hub is unreachable
/// and must never be confused for something the hub knows about.
///
/// Adding a new persisted preference should cost one `const Pref<T>`
/// declaration and nothing else -- no new storage code, no new load path.
/// See the declarations at the bottom of this file for the pattern.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'prefs_backend.dart';

/// One persisted preference: a key, a default, and how to turn the decoded
/// JSON value into `T` and back. [decode]/[encode] are only needed for
/// anything that isn't already a JSON-native type (`bool`, `num`, `String`,
/// or a `Map`/`List` of those) -- a plain `String` or `Map<String, dynamic>`
/// pref needs neither.
class Pref<T> {
  const Pref(this.key, this.defaultValue, {this.decode, this.encode});

  /// Declarations are meant to be `const`, which means [decode]/[encode]
  /// must be static or top-level function tear-offs -- an inline closure
  /// (`(m) => m.name`) is not a constant expression and will not compile
  /// here. See `_modeToJson`/`_Mode.fromJson` in `ui_prefs_test.dart` for
  /// the pattern an enum-valued preference follows.

  /// Dotted namespace, e.g. `"shell.tab"`. Never reused for a different
  /// shape of value -- a key that used to hold a `String` and now holds a
  /// `Map` would decode old stored data as the wrong type forever.
  final String key;

  final T defaultValue;

  final T Function(Object json)? decode;
  final Object Function(T value)? encode;
}

/// Master "remember where I was" switch. Declared here, not by whichever
/// screen exposes the toggle, because [UiPrefs] itself has to special-case
/// it: turning it off must still persist -- otherwise there would be no way
/// to record that the user asked for a fresh start every time -- while
/// every other preference stops persisting the moment it is off.
const Pref<bool> kRememberEnabled = Pref<bool>('meta.remembering', true);

/// The document version this build writes. Bumping it means adding an entry
/// to [UiPrefs._migrate] for the old shape; there is nothing to migrate
/// from yet, so it only ever returns an empty document today.
const int kUiPrefsVersion = 1;

/// Holds every persisted UI preference and notifies on change, the same
/// shape as [HubStore] so a screen already knows how to consume it.
///
/// All reads and writes go through [get]/[set] keyed by a [Pref] -- there
/// is no per-preference field on this class to add when a new one shows up.
class UiPrefs extends ChangeNotifier {
  UiPrefs._(this.backend) : _values = {};

  final PrefsBackend backend;
  Map<String, dynamic> _values;

  Timer? _debounce;
  bool _dirty = false;

  bool _degraded = false;

  /// True once a read or write has failed -- storage disabled, quota full,
  /// a corrupt document. The app keeps working on defaults either way; this
  /// is only for the Settings screen to say so rather than pretend nothing
  /// is being lost.
  bool get degraded => _degraded;

  set _degradedFlag(bool value) {
    if (_degraded == value) return;
    _degraded = value;
    notifyListeners();
  }

  bool get _remembering => _decode(kRememberEnabled);

  /// Loads the stored document from [backend]. Never throws and never hangs
  /// the caller indefinitely: a slow or wedged backend degrades to defaults
  /// after a second rather than delaying the app's first frame.
  static Future<UiPrefs> open({PrefsBackend? backend}) async {
    final prefs = UiPrefs._(backend ?? const SharedPrefsBackend());
    try {
      await prefs._load().timeout(const Duration(seconds: 1));
    } catch (_) {
      prefs._degradedFlag = true;
    }
    return prefs;
  }

  /// An in-memory instance for tests and for callers (widget tests, mainly)
  /// that do not care about persistence at all. [seedValues] are the
  /// *decoded* values a test wants present from the first frame, keyed by
  /// [Pref.key] -- e.g. `UiPrefs.memory({'shell.tab': 'devices'})`.
  factory UiPrefs.memory([Map<String, dynamic>? seedValues]) {
    final prefs = UiPrefs._(MemoryPrefsBackend());
    prefs._values = Map<String, dynamic>.from(seedValues ?? const {});
    return prefs;
  }

  Future<void> _load() async {
    final raw = await backend.read();
    if (raw == null) return; // nothing saved yet -- stay at defaults
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return;
      final version = decoded['v'];
      final values = ((decoded['values'] as Map?))?.cast<String, dynamic>() ?? {};
      _values = version == kUiPrefsVersion ? values : _migrate(version, values);
    } catch (_) {
      // Corrupt document. Defaults for everything beats refusing to start.
      _degradedFlag = true;
      _values = {};
    }
  }

  /// Reshapes an older (or, after a downgrade, newer) document into the
  /// current shape. Nothing has changed shape yet, so an unrecognised
  /// version starts fresh rather than guessing -- add a case here the first
  /// time [kUiPrefsVersion] moves.
  Map<String, dynamic> _migrate(Object? fromVersion, Map<String, dynamic> values) => {};

  // ------------------------------------------------------------------

  /// The current value of [pref], or [Pref.defaultValue] if it was never
  /// set, failed to decode, or "remember where I was" is off.
  T get<T>(Pref<T> pref) {
    if (pref.key != kRememberEnabled.key && !_remembering) return pref.defaultValue;
    return _decode(pref);
  }

  T _decode<T>(Pref<T> pref) {
    final raw = _values[pref.key];
    if (raw == null) return pref.defaultValue;
    try {
      return pref.decode != null ? pref.decode!(raw) : raw as T;
    } catch (_) {
      return pref.defaultValue;
    }
  }

  /// Stores [value] under [pref] and schedules a debounced write. A no-op
  /// while "remember where I was" is off, except for the switch itself.
  void set<T>(Pref<T> pref, T value) {
    if (pref.key != kRememberEnabled.key && !_remembering) return;
    final encoded = pref.encode != null ? pref.encode!(value) : value as Object;
    if (_jsonEquals(_values[pref.key], encoded)) return;
    _values[pref.key] = encoded;
    _dirty = true;
    _scheduleWrite();
    notifyListeners();
  }

  /// Removes [pref]'s stored value, so [get] falls back to its default.
  void reset<T>(Pref<T> pref) {
    if (!_values.containsKey(pref.key)) return;
    _values.remove(pref.key);
    _dirty = true;
    _scheduleWrite();
    notifyListeners();
  }

  /// Clears every stored preference, including the "remember where I was"
  /// switch itself -- a full reset, not "reset everything except this".
  void resetAll() {
    if (_values.isEmpty) return;
    _values = {};
    _dirty = true;
    _scheduleWrite();
    notifyListeners();
  }

  // ------------------------------------------------------------------

  void _scheduleWrite() {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 300), _persist);
  }

  /// Forces any pending write out now. Call this when the app is about to
  /// go away (a hidden tab, a closed window) -- a change made a moment
  /// before that has no other reason to reach storage otherwise.
  Future<void> flush() async {
    _debounce?.cancel();
    _debounce = null;
    if (!_dirty) return;
    await _persist();
  }

  Future<void> _persist() async {
    _debounce = null;
    _dirty = false;
    try {
      await backend.write(jsonEncode({'v': kUiPrefsVersion, 'values': _values}));
    } catch (_) {
      _degradedFlag = true;
    }
  }

  @override
  void dispose() {
    _debounce?.cancel();
    super.dispose();
  }
}

bool _jsonEquals(Object? a, Object? b) {
  if (identical(a, b)) return true;
  if (a == null || b == null) return false;
  try {
    return jsonEncode(a) == jsonEncode(b);
  } catch (_) {
    return a == b;
  }
}

// ----------------------------------------------------------------------
// Declared preferences. One line each -- this is the whole cost of making
// a new piece of UI state survive a reload.
// ----------------------------------------------------------------------

/// The top-level tab: `'live' | 'scenes' | 'devices' | 'settings'`. Stored
/// by id, not index -- inserting a new destination must not silently point
/// everyone's saved position at a different page. An id the current build
/// does not recognise (an older or newer build's tab) falls back to Live.
const Pref<String> kShellTab = Pref<String>('shell.tab', 'live');

const Pref<String> kDevicesQuery = Pref<String>('devices.query', '');

const Pref<String> kScenesQuery = Pref<String>('scenes.query', '');

/// The activity log's filter/sort state, as [ActivityFilter.toJson] shapes
/// it. Typed as a raw map rather than `Pref<ActivityFilter>` so this file
/// does not need to import that class -- `HubStore` does the encode/decode
/// on the way in and out.
const Pref<Map<String, dynamic>> kActivityFilter =
    Pref<Map<String, dynamic>>('activity.filter', {});

/// Whether the live view's remote was full-screen. Set true the moment it
/// opens and false the moment it closes (by any route: the exit button,
/// Escape, or the system/browser back gesture), so a reload lands back on
/// it exactly when it was genuinely still open -- never stale from a
/// session that exited normally.
const Pref<bool> kRemoteMaximized = Pref<bool>('live.maximized', false);
