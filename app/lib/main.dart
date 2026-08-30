/// Harmony Hub control app.
///
/// Talks to the Python hub over its REST + WebSocket API, which is the only
/// contract between the two -- nothing here assumes anything about how the
/// hub reaches equipment, and nothing there assumes this app exists.
library;

import 'package:flutter/material.dart';

import 'api/settings.dart';
import 'screens/devices_screen.dart';
import 'screens/live_screen.dart';
import 'screens/scenes_screen.dart';
import 'screens/settings_screen.dart';
import 'state/hub_store.dart';
import 'state/reload.dart';
import 'state/ui_prefs.dart';

/// The build this page was compiled with, via
/// `--dart-define=BUILD_ID=<build-id>` (see `harmony_deploy.cli.build_web`).
/// Empty for an ordinary `flutter run` or a plain `flutter build web` with
/// no override -- which is why the stale-page banner only ever fires when
/// this is actually set, rather than on every dev run.
const String kBuildId = String.fromEnvironment('BUILD_ID');

/// Whether the page in front of the user is not the build the hub is now serving.
bool _isStale(VersionInfo? version) {
  final served = version?.webBuildId;
  return kBuildId.isNotEmpty && served != null && served.isNotEmpty && served != kBuildId;
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // Awaited before the first frame so the app opens straight on the
  // remembered tab instead of flashing Live and then jumping -- the read
  // itself is a few milliseconds, and `UiPrefs.open` degrades to defaults
  // rather than hanging if storage is slow or unavailable.
  final prefs = await UiPrefs.open();
  runApp(HarmonyHubApp(prefs: prefs));
}

/// Makes the store available to the whole tree and rebuilds on its changes.
class HubScope extends InheritedNotifier<HubStore> {
  const HubScope({super.key, required HubStore store, required super.child})
      : super(notifier: store);

  static HubStore of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<HubScope>()!.notifier!;
}

/// Makes device-local UI preferences (last tab, filters, ...) available to
/// the whole tree. Kept separate from [HubScope]: this is the browser's own
/// state, not the hub's, and it has to keep working when the hub does not.
class PrefsScope extends InheritedNotifier<UiPrefs> {
  const PrefsScope({super.key, required UiPrefs prefs, required super.child})
      : super(notifier: prefs);

  static UiPrefs of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<PrefsScope>()!.notifier!;
}

class HarmonyHubApp extends StatefulWidget {
  const HarmonyHubApp({super.key, this.store, this.prefs});

  /// Injectable so widget tests can drive the whole app against a fake hub.
  final HubStore? store;

  /// Injectable so widget tests can seed a starting tab, filter, etc.
  /// without touching real storage. Defaults to an in-memory instance --
  /// nothing persists, but nothing crashes either.
  final UiPrefs? prefs;

  @override
  State<HarmonyHubApp> createState() => _HarmonyHubAppState();
}

class _HarmonyHubAppState extends State<HarmonyHubApp> {
  late final HubStore _store;
  late final bool _ownsStore;
  late final UiPrefs _prefs;
  late final bool _ownsPrefs;
  late final AppLifecycleListener _lifecycleListener;

  @override
  void initState() {
    super.initState();
    // Prefs first: the store reads the saved activity filter out of it at
    // construction time.
    _ownsPrefs = widget.prefs == null;
    _prefs = widget.prefs ?? UiPrefs.memory();
    // A debounced write sitting in the timer when the tab closes or the app
    // is backgrounded would otherwise just be lost.
    _lifecycleListener = AppLifecycleListener(onHide: _prefs.flush);

    _ownsStore = widget.store == null;
    _store = widget.store ?? HubStore(prefs: _prefs);
    _store.load();
    _store.connect();
  }

  @override
  void dispose() {
    _lifecycleListener.dispose();
    // A store or prefs instance handed in from outside belongs to whoever
    // created it.
    if (_ownsStore) _store.dispose();
    if (_ownsPrefs) _prefs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = ColorScheme.fromSeed(
      seedColor: const Color(0xFF4F7CFF),
      brightness: Brightness.dark,
    );
    return HubScope(
      store: _store,
      child: PrefsScope(
        prefs: _prefs,
        child: MaterialApp(
          title: 'Harmony Hub',
          debugShowCheckedModeBanner: false,
          theme: ThemeData(
            colorScheme: scheme,
            useMaterial3: true,
            cardTheme: CardThemeData(
              elevation: 0,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(14),
                side: BorderSide(color: scheme.outlineVariant),
              ),
            ),
          ),
          home: SelectionArea(child: const HomeShell()),
        ),
      ),
    );
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  /// Which tab is showing, by id rather than position -- a saved index
  /// would silently point at the wrong page the day a destination is added
  /// or reordered. `null` until the first build seeds it from [UiPrefs].
  String? _tab;

  static const _destinations = [
    (id: 'live', icon: Icons.settings_remote, label: 'Live'),
    (id: 'scenes', icon: Icons.movie_filter_outlined, label: 'Scenes'),
    (id: 'devices', icon: Icons.devices_other, label: 'Devices'),
    (id: 'settings', icon: Icons.tune, label: 'Settings'),
  ];

  /// Reads the remembered tab on first build only -- later builds must not
  /// re-adopt it, or picking a tab and then having some unrelated pref
  /// change (typing in a search box elsewhere) rebuild this widget would
  /// snap the selection back. An id this build does not recognise (an
  /// older or newer build's tab) falls back to the first destination.
  String _ensureTab(UiPrefs prefs) {
    _tab ??= prefs.get(kShellTab);
    if (!_destinations.any((d) => d.id == _tab)) _tab = _destinations.first.id;
    return _tab!;
  }

  @override
  Widget build(BuildContext context) {
    final store = HubScope.of(context);
    final prefs = PrefsScope.of(context);
    final tab = _ensureTab(prefs);

    void select(String id) {
      setState(() => _tab = id);
      prefs.set(kShellTab, id);
    }

    final pages = <String, Widget>{
      'live': const LiveScreen(),
      'scenes': const ScenesScreen(),
      'devices': const DevicesScreen(),
      'settings': const SettingsScreen(),
    };

    // Rail on anything desktop-sized, bottom bar on phones. Doing this now
    // costs one branch and means the Android and iOS targets need no layout
    // work when they are switched on.
    final wide = MediaQuery.sizeOf(context).width >= 720;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Harmony Hub'),
        actions: [
          ConnectionDot(connected: store.connected),
          IconButton(
            tooltip: 'Refresh',
            onPressed: store.load,
            icon: const Icon(Icons.refresh),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: Column(
        children: [
          // Stale wins over "an update is available": after an install the
          // page is stale *and* that release is already current, so showing
          // both would contradict itself. Hidden while installing, too --
          // the Software card already shows progress, and a banner still
          // offering to review a release that is mid-install is confusing.
          if (_isStale(store.version))
            const _StalePageBanner()
          else if (store.hasUndismissedUpdate && !store.installingUpdate)
            _UpdateAvailableBanner(store: store, onOpenSettings: () => select('settings')),
          if (store.error != null) _ErrorBanner(message: store.error!, onDismiss: store.clearError),
          // The page outlives the hub, so it has to say when it has. Without
          // this a stopped hub looks exactly like one where nothing is
          // happening, and every screen would quietly do nothing.
          if (!store.loading && !store.hubRunning)
            _HubDownBanner(
              runtime: store.runtime,
              onOpenSettings: () => select('settings'),
            ),
          Expanded(
            child: store.loading
                ? const Center(child: CircularProgressIndicator())
                : Row(
                    children: [
                      if (wide)
                        NavigationRail(
                          selectedIndex: _destinations.indexWhere((d) => d.id == tab),
                          onDestinationSelected: (i) => select(_destinations[i].id),
                          labelType: NavigationRailLabelType.all,
                          destinations: [
                            for (final d in _destinations)
                              NavigationRailDestination(
                                icon: Icon(d.icon),
                                label: Text(d.label),
                              ),
                          ],
                        ),
                      Expanded(child: pages[tab]!),
                    ],
                  ),
          ),
        ],
      ),
      bottomNavigationBar: wide
          ? null
          : NavigationBar(
              selectedIndex: _destinations.indexWhere((d) => d.id == tab),
              onDestinationSelected: (i) => select(_destinations[i].id),
              destinations: [
                for (final d in _destinations)
                  NavigationDestination(icon: Icon(d.icon), label: d.label),
              ],
            ),
    );
  }
}

/// The live-events connection state, as a dot plus label.
///
/// Public because the maximized remote page (`live_screen.dart`) shows it
/// too -- the AppBar it normally lives in is hidden there, and a dropped
/// socket must not look identical to a quiet remote.
class ConnectionDot extends StatelessWidget {
  const ConnectionDot({super.key, required this.connected, this.compact = false});

  final bool connected;

  /// Drops the "Live"/"Offline" label, keeping just the dot and its
  /// tooltip -- for the maximized remote's slim scene strip.
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final dot = Icon(Icons.circle, size: 10, color: connected ? Colors.greenAccent : scheme.error);
    return Tooltip(
      message: connected ? 'Live events connected' : 'Reconnecting to the hub…',
      child: compact
          ? dot
          : Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: Row(
                children: [
                  dot,
                  const SizedBox(width: 6),
                  Text(connected ? 'Live' : 'Offline', style: Theme.of(context).textTheme.labelMedium),
                ],
              ),
            ),
    );
  }
}

/// Says a deploy landed while this page was open, and offers a reload.
///
/// A restart-onto-new-code changes what `/api/version` reports without this
/// tab noticing on its own -- the websocket reconnects to the *same* build
/// of the page, since reconnecting does not reload anything. Manual reload
/// rather than automatic: this page may have unsaved edits open in a form.
class _StalePageBanner extends StatelessWidget {
  const _StalePageBanner();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.tertiaryContainer,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
        child: Row(
          children: [
            Icon(Icons.system_update, size: 20, color: scheme.onTertiaryContainer),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                'A new version of this app is available.',
                style: TextStyle(color: scheme.onTertiaryContainer),
              ),
            ),
            TextButton(onPressed: reloadPage, child: const Text('Reload')),
          ],
        ),
      ),
    );
  }
}

/// Says the hub found a new release on GitHub on its own, and points at
/// Software in Settings to review and install it.
///
/// Deliberately does not install from here -- one place to actually trigger
/// an install (the Software card) is easier to reason about than two, and
/// this banner can be dismissed per-release without losing that.
class _UpdateAvailableBanner extends StatelessWidget {
  const _UpdateAvailableBanner({required this.store, required this.onOpenSettings});

  final HubStore store;
  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context) {
    final release = store.availableUpdate?.available;
    if (release == null) return const SizedBox.shrink();
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.tertiaryContainer,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
        child: Row(
          children: [
            Icon(Icons.system_update, size: 20, color: scheme.onTertiaryContainer),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                'Harmony Hub ${release.tag} is available.',
                style: TextStyle(color: scheme.onTertiaryContainer),
              ),
            ),
            TextButton(onPressed: onOpenSettings, child: const Text('Review')),
            TextButton(onPressed: store.dismissUpdateBanner, child: const Text('Dismiss')),
          ],
        ),
      ),
    );
  }
}

/// Says the hub is down, and where to go about it.
///
/// Deliberately not a blocking dialog: everything to do with *configuring* a
/// hub still works while it is stopped, and being stopped is when that is
/// most needed.
class _HubDownBanner extends StatelessWidget {
  const _HubDownBanner({required this.runtime, required this.onOpenSettings});

  final RuntimeStatus runtime;
  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final failed = runtime.isFailed;
    return Material(
      color: failed ? scheme.errorContainer : scheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
        child: Row(
          children: [
            Icon(
              failed ? Icons.error_outline : Icons.pause_circle_outline,
              size: 20,
              color: failed ? scheme.onErrorContainer : scheme.onSurfaceVariant,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                failed
                    ? 'The hub could not start: ${runtime.detail}. Scenes and devices can still be edited.'
                    : 'The hub is stopped, so presses will not reach any equipment. '
                        'Scenes and devices can still be edited.',
                style: TextStyle(
                  color: failed ? scheme.onErrorContainer : scheme.onSurfaceVariant,
                ),
              ),
            ),
            TextButton(onPressed: onOpenSettings, child: const Text('Settings')),
            const SizedBox(width: 4),
          ],
        ),
      ),
    );
  }
}

class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({required this.message, required this.onDismiss});

  final String message;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.errorContainer,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
        child: Row(
          children: [
            Icon(Icons.warning_amber_rounded, color: scheme.onErrorContainer, size: 20),
            const SizedBox(width: 12),
            Expanded(
              child: Text(message, style: TextStyle(color: scheme.onErrorContainer)),
            ),
            IconButton(
              icon: const Icon(Icons.close, size: 18),
              color: scheme.onErrorContainer,
              onPressed: onDismiss,
            ),
          ],
        ),
      ),
    );
  }
}
