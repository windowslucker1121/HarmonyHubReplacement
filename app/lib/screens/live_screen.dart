/// The live view: what the remote is doing right now, and what each button
/// is currently bound to.
///
/// Shows bindings resolved the way the engine resolves them -- the active
/// scene first, then the global fallback -- so what is on screen is what
/// will actually happen, not just what the scene file happens to say.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/config.dart';
import '../api/models.dart';
import '../main.dart';
import '../state/hub_store.dart';
import '../state/ui_prefs.dart';
import '../widgets/activity_filter_sheet.dart';
import '../widgets/remote_diagram.dart';
import 'learn_screen.dart';

class LiveScreen extends StatefulWidget {
  const LiveScreen({super.key});

  @override
  State<LiveScreen> createState() => _LiveScreenState();
}

class _LiveScreenState extends State<LiveScreen> {
  @override
  void initState() {
    super.initState();
    // Post-frame, not here: `openMaximizedRemote` pushes a route, and a
    // route cannot be pushed before this widget's own first frame has
    // built. Runs once per mount -- switching tabs away and back creates a
    // fresh state, but by then `kRemoteMaximized` has already been cleared
    // on exit, so it only ever fires right after a reload that left it set.
    WidgetsBinding.instance.addPostFrameCallback((_) => _restoreMaximized());
  }

  void _restoreMaximized() {
    if (!mounted) return;
    if (PrefsScope.of(context).get(kRemoteMaximized)) {
      openMaximizedRemote(context);
    }
  }

  @override
  Widget build(BuildContext context) {
    final store = HubScope.of(context);
    final wide = MediaQuery.sizeOf(context).width >= 1000;

    final remote = _RemotePanel(store: store);
    final log = _ActivityLog(store: store);

    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Above the scene bar, not folded into it: pausing is a testing
          // safety switch, not a fact about which scene is running, and it
          // needs to be the first thing anyone sees on this page -- easy to
          // miss in Settings, expensive to forget is on.
          if (store.paused) ...[
            _PausedBanner(store: store),
            const SizedBox(height: 16),
          ],
          _ActiveSceneBar(store: store),
          const SizedBox(height: 16),
          Expanded(
            child: wide
                ? Row(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Expanded(flex: 3, child: remote),
                      const SizedBox(width: 16),
                      Expanded(flex: 2, child: log),
                    ],
                  )
                : Column(
                    children: [
                      Expanded(flex: 3, child: remote),
                      const SizedBox(height: 16),
                      Expanded(flex: 2, child: log),
                    ],
                  ),
          ),
        ],
      ),
    );
  }
}

/// Opens the live remote full screen: the same board, the same bindings,
/// the same taps -- just with the shell's chrome out of the way.
///
/// Remembers that it is open the same way the shell remembers the active
/// tab: [kRemoteMaximized] is set the moment this starts and cleared the
/// moment the route is popped, by whatever means popped it -- the exit
/// button, Escape, or the system/browser back gesture all resolve the same
/// `Navigator.push` future, so one place catches every exit.
Future<void> openMaximizedRemote(BuildContext context) async {
  final prefs = PrefsScope.of(context);
  prefs.set(kRemoteMaximized, true);
  await Navigator.push<void>(
    context,
    MaterialPageRoute<void>(builder: (_) => const MaximizedRemotePage()),
  );
  prefs.set(kRemoteMaximized, false);
}

/// The remote, full screen.
///
/// Not a second remote: [_RemotePanel] and [RemoteBoard] are the exact same
/// widgets the live view uses, just without the card chrome around them.
/// The page reads [HubScope] itself (rather than taking a store parameter)
/// so it keeps rebuilding on flashes, scene changes and connection state
/// after the push -- the whole reason this can be a page instead of a
/// second implementation.
class MaximizedRemotePage extends StatelessWidget {
  const MaximizedRemotePage({super.key});

  @override
  Widget build(BuildContext context) {
    final store = HubScope.of(context);

    return Scaffold(
      body: SafeArea(
        child: CallbackShortcuts(
          bindings: {
            const SingleActivator(LogicalKeyboardKey.escape): () => Navigator.of(context).maybePop(),
          },
          child: Focus(
            autofocus: true,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  _ActiveSceneBar(
                    store: store,
                    compact: true,
                    onExit: () => Navigator.of(context).maybePop(),
                  ),
                  const SizedBox(height: 8),
                  Expanded(child: _RemotePanel(store: store, maximized: true)),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Hard to miss, on purpose: pausing is a live safety switch for testing on
/// real hardware, and forgetting it is on is worse than the banner being in
/// the way. `Resume` sits right on it -- the same live toggle Settings'
/// Event source card offers, just reachable from wherever presses are
/// actually being watched.
class _PausedBanner extends StatelessWidget {
  const _PausedBanner({required this.store});

  final HubStore store;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      color: scheme.tertiaryContainer,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            Icon(Icons.pause_circle, color: scheme.onTertiaryContainer),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Paused',
                    style: Theme.of(context)
                        .textTheme
                        .titleMedium
                        ?.copyWith(color: scheme.onTertiaryContainer),
                  ),
                  Text(
                    'Button presses are logged but nothing is being sent to a device.',
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color: scheme.onTertiaryContainer.withValues(alpha: 0.8),
                        ),
                  ),
                ],
              ),
            ),
            FilledButton.tonalIcon(
              onPressed: () => store.resumeHub(),
              icon: const Icon(Icons.play_arrow),
              label: const Text('Resume'),
            ),
          ],
        ),
      ),
    );
  }
}

class _ActiveSceneBar extends StatelessWidget {
  const _ActiveSceneBar({required this.store, this.compact = false, this.onExit});

  final HubStore store;

  /// A single row -- just which scene is running -- instead of the full
  /// three-line card. Used by the maximized remote, where the bindings
  /// count and the SmartHome-focus line are editing context that a page
  /// meant for *pressing* buttons does not need, and where every pixel of
  /// vertical chrome costs width on a diagram 3.4x taller than it is wide.
  final bool compact;

  /// Present only in [compact] mode: the way back to the normal live view.
  final VoidCallback? onExit;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final active = store.status?.activeScene;
    final scene = active == null ? null : store.config?.scene(active);

    if (compact) {
      return Card(
        color: active == null ? null : scheme.primaryContainer,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: Row(
            children: [
              Icon(
                active == null ? Icons.power_settings_new : Icons.play_circle_fill,
                size: 20,
                color: active == null ? scheme.outline : scheme.onPrimaryContainer,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  scene?.name ?? 'No scene running',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleSmall?.copyWith(
                        color: active == null ? null : scheme.onPrimaryContainer,
                      ),
                ),
              ),
              ConnectionDot(connected: store.connected, compact: true),
              if (active != null)
                IconButton(
                  tooltip: 'Stop scene',
                  visualDensity: VisualDensity.compact,
                  icon: Icon(Icons.stop, color: scheme.onPrimaryContainer),
                  onPressed: store.hubRunning ? store.stopScene : null,
                ),
              if (onExit != null)
                IconButton(
                  tooltip: 'Exit full screen',
                  visualDensity: VisualDensity.compact,
                  icon: Icon(
                    Icons.fullscreen_exit,
                    color: active == null ? null : scheme.onPrimaryContainer,
                  ),
                  onPressed: onExit,
                ),
            ],
          ),
        ),
      );
    }

    return Card(
      color: active == null ? null : scheme.primaryContainer,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            Icon(
              active == null ? Icons.power_settings_new : Icons.play_circle_fill,
              color: active == null ? scheme.outline : scheme.onPrimaryContainer,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    scene?.name ?? 'No scene running',
                    style: Theme.of(context).textTheme.titleMedium?.copyWith(
                          color: active == null ? null : scheme.onPrimaryContainer,
                        ),
                  ),
                  Text(
                    active == null
                        ? (store.config?.globalScene == null
                            ? 'No global scene set — unbound buttons do nothing'
                            : 'Only the global scene\'s bindings are in effect')
                        : '${scene?.bindings.length ?? 0} button(s) rebound by this scene',
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color: active == null ? null : scheme.onPrimaryContainer.withValues(alpha: 0.8),
                        ),
                  ),
                  Text(
                    _focusSummary(store.status?.focus),
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color: active == null ? scheme.outline : scheme.onPrimaryContainer.withValues(alpha: 0.8),
                        ),
                  ),
                ],
              ),
            ),
            if (active != null)
              FilledButton.tonalIcon(
                onPressed: store.hubRunning ? store.stopScene : null,
                icon: const Icon(Icons.stop),
                label: const Text('Stop'),
              ),
          ],
        ),
      ),
    );
  }
}

/// What the SmartHome +/- keys will do right now, for the line under the
/// active scene. They are not part of any scene -- a light stays focused
/// across a scene switch -- which is why this reads off `focus` directly
/// rather than anything scene-shaped.
String _focusSummary(FocusInfo? focus) {
  if (focus == null) return 'SmartHome +/- keys: nothing touched yet';
  return focus.canAdjust
      ? 'SmartHome +/- keys follow ${focus.label}'
      : 'SmartHome +/- keys follow ${focus.label} (nothing to turn up or down)';
}

class _RemotePanel extends StatelessWidget {
  const _RemotePanel({required this.store, this.maximized = false});

  final HubStore store;

  /// Drops the card and the header row: the maximized page has its own
  /// top strip, and every pixel of chrome here costs roughly a third of a
  /// pixel of remote width, the diagram being 3.4x taller than it is wide.
  final bool maximized;

  /// The binding the engine would use for this button right now.
  ///
  /// Mirrors `SceneEngine.binding_for`: the active scene first, then whatever
  /// scene `globalScene` names -- a reference, not a separate set of its own.
  Binding? _effective(String key) {
    final active = store.status?.activeScene;
    if (active != null) {
      final scene = store.config?.scene(active);
      final bound = scene?.bindings[key];
      if (bound != null) return bound;
    }
    final globalScene = store.config?.globalScene;
    if (globalScene == null) return null;
    return store.config?.scene(globalScene)?.bindings[key];
  }

  @override
  Widget build(BuildContext context) {
    if (store.buttons.isEmpty) {
      return Card(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.settings_remote, size: 48),
                const SizedBox(height: 12),
                Text('No buttons known yet', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 4),
                const Text(
                  'The hub does not know what the buttons on your remote are called.\n'
                  'Press them once each and give them names.',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 16),
                FilledButton.icon(
                  onPressed: () => openLearnPage(context, store),
                  icon: const Icon(Icons.school_outlined),
                  label: const Text('Learn the remote'),
                ),
              ],
            ),
          ),
        ),
      );
    }

    final board = RemoteBoard(
      buttons: store.buttons,
      status: (key) {
        final binding = _effective(key);
        return RemoteKeyStatus(
          caption: binding != null && !binding.isEmpty ? binding.summary : null,
          highlighted: store.isFlashing(key),
        );
      },
      onTap: store.hubRunning ? store.tap : null,
      // Maximized has no header to carry the "hub is stopped" note, so the
      // hint line does it instead -- it is already reserved space, so this
      // costs no extra height.
      hint: !maximized
          ? 'Tap a button to send it, or hover to see its binding'
          : store.hubRunning
              ? 'Tap a button to send it'
              : 'The hub is stopped — presses will not reach anything',
    );

    if (maximized) return board;

    return Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
            child: Row(
              children: [
                Text('Remote', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(width: 8),
                // Expanded, because at phone width the subtitle is wider than
                // what is left of the row.
                Expanded(
                  child: Text(
                    store.hubRunning
                        ? '${store.buttons.length} buttons · tap to send'
                        : '${store.buttons.length} buttons · the hub is stopped, so nothing to send to',
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
                IconButton(
                  tooltip: 'Maximize the remote',
                  visualDensity: VisualDensity.compact,
                  icon: const Icon(Icons.open_in_full),
                  onPressed: () => openMaximizedRemote(context),
                ),
              ],
            ),
          ),
          Expanded(
            // Bindings still render with the hub down -- they come from
            // configuration, not from the engine -- but tapping is off,
            // because a press that reaches nothing is worse than one that
            // visibly cannot be made.
            child: board,
          ),
        ],
      ),
    );
  }
}

class _ActivityLog extends StatelessWidget {
  const _ActivityLog({required this.store});

  final HubStore store;

  static const _icons = {
    'button': Icons.radio_button_checked,
    'scene': Icons.movie_filter_outlined,
    'action': Icons.bolt,
    'status': Icons.info_outline,
    'hub': Icons.power_settings_new,
  };

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final events = store.events;
    final visible = store.visibleEvents;
    final filter = store.activityFilter;

    return Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 8, 6),
            child: Row(
              children: [
                Text('Activity', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(width: 8),
                if (filter.isActive)
                  Expanded(
                    child: Text(
                      '${visible.length} of ${events.length}',
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(color: scheme.outline),
                    ),
                  )
                else
                  const Spacer(),
                IconButton(
                  tooltip: 'Filter & sort',
                  visualDensity: VisualDensity.compact,
                  icon: filter.isActive
                      ? Badge(smallSize: 8, child: const Icon(Icons.filter_list))
                      : const Icon(Icons.filter_list),
                  onPressed: () => showActivityFilterDialog(context, store),
                ),
              ],
            ),
          ),
          Expanded(
            child: events.isEmpty
                ? Center(
                    child: Text(
                      'Waiting for the remote…',
                      style: TextStyle(color: scheme.outline),
                    ),
                  )
                : visible.isEmpty
                    ? Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              '${events.length} event(s) hidden by the filter',
                              style: TextStyle(color: scheme.outline),
                            ),
                            TextButton(
                              onPressed: () => store.updateActivityFilter((f) => f.clear()),
                              child: const Text('Clear filter'),
                            ),
                          ],
                        ),
                      )
                    : ListView.builder(
                        padding: const EdgeInsets.only(bottom: 8),
                        itemCount: visible.length,
                        itemBuilder: (context, index) {
                          final event = visible[index];
                          final time = event.at.toIso8601String().substring(11, 23);
                          return ListTile(
                            dense: true,
                            visualDensity: VisualDensity.compact,
                            leading: Icon(
                              _icons[event.type] ?? Icons.circle,
                              size: 18,
                              color: event.isFailure ? scheme.error : scheme.primary,
                            ),
                            title: Text(
                              event.summary,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(color: event.isFailure ? scheme.error : null),
                            ),
                            trailing: Text(time, style: Theme.of(context).textTheme.labelSmall),
                          );
                        },
                      ),
          ),
        ],
      ),
    );
  }
}
