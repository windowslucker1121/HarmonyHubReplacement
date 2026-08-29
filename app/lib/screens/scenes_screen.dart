/// Scene list, and the editor for one scene.
library;

import 'package:flutter/material.dart';

import '../api/config.dart';
import '../main.dart';
import '../state/hub_store.dart';
import '../state/ui_prefs.dart';
import '../widgets/action_editor.dart';
import '../widgets/labeled_slider.dart';
import '../widgets/remote_diagram.dart';
import '../widgets/responsive_card_grid.dart';
import '../widgets/search_field.dart';
import 'binding_editor.dart';

class ScenesScreen extends StatefulWidget {
  const ScenesScreen({super.key});

  @override
  State<ScenesScreen> createState() => _ScenesScreenState();
}

/// Keywords for the "Global scene" card -- there is only ever one of these,
/// so unlike a scene's own haystack this does not need to be recomputed
/// from live config, just matched against.
const _globalSceneKeywords = 'global scene fallback default unbound idle';

/// Keywords for the "Default repeat timing" card, likewise fixed.
const _repeatTimingKeywords = 'default repeat timing delay interval hold wait button press';

class _ScenesScreenState extends State<ScenesScreen> {
  String _query = '';

  /// Seeded from [UiPrefs] once, on the first build -- see the identical
  /// note on `_HomeShellState._ensureTab` for why this can't just be a
  /// field initializer.
  bool _restoredQuery = false;

  Future<void> _openEditor(BuildContext context, HubStore store, SceneConfig scene, {bool isNew = false}) async {
    final edited = await Navigator.push<SceneConfig>(
      context,
      MaterialPageRoute(builder: (_) => SceneEditorPage(scene: scene, store: store)),
    );
    if (edited == null) return;

    final config = store.config!.copy();
    final index = config.scenes.indexWhere((s) => s.id == scene.id);
    if (index >= 0) {
      config.scenes[index] = edited;
    } else {
      config.scenes.add(edited);
    }
    final saved = await store.saveConfig(config);
    if (context.mounted && !saved) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(store.error ?? 'Could not save')),
      );
    }
  }

  Future<void> _delete(BuildContext context, HubStore store, SceneConfig scene) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Delete "${scene.name}"?'),
        content: const Text(
          'Any button bound to this scene elsewhere will stop the save from being accepted '
          'until those bindings are changed too.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (confirmed != true) return;

    final config = store.config!.copy()..scenes.removeWhere((s) => s.id == scene.id);
    final saved = await store.saveConfig(config);
    if (context.mounted && !saved) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(store.error ?? 'Could not delete')),
      );
    }
  }

  Future<void> _create(BuildContext context, HubStore store) async {
    final name = await _promptForName(context);
    if (name == null || name.isEmpty) return;
    final id = slugify(name);
    if (store.config!.scene(id) != null) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('A scene called "$id" already exists')),
        );
      }
      return;
    }
    if (context.mounted) {
      await _openEditor(context, store, SceneConfig(id: id, name: name), isNew: true);
    }
  }

  Iterable<String?> _sceneHaystack(HubConfig config, SceneConfig scene) sync* {
    yield scene.name;
    yield scene.id;
    for (final deviceId in scene.devices) {
      yield config.device(deviceId)?.name;
    }
    yield* scene.bindings.keys;
  }

  @override
  Widget build(BuildContext context) {
    final store = HubScope.of(context);
    final prefs = PrefsScope.of(context);
    if (!_restoredQuery) {
      _restoredQuery = true;
      _query = prefs.get(kScenesQuery);
    }

    void setQuery(String value) {
      setState(() => _query = value);
      prefs.set(kScenesQuery, value);
    }

    final config = store.config;
    final active = store.status?.activeScene;

    if (config == null) return const Center(child: CircularProgressIndicator());

    final hasScenes = config.scenes.isNotEmpty;
    if (!hasScenes) {
      return Scaffold(
        body: _EmptyState(onCreate: () => _create(context, store)),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () => _create(context, store),
          icon: const Icon(Icons.add),
          label: const Text('New scene'),
        ),
      );
    }

    final showGlobalScene = matchesQuery(_query, [_globalSceneKeywords]);
    final showRepeatTiming = matchesQuery(_query, [_repeatTimingKeywords]);
    final visibleScenes = [
      for (final scene in config.scenes)
        if (matchesQuery(_query, _sceneHaystack(config, scene))) scene,
    ];
    final matchCount =
        (showGlobalScene ? 1 : 0) + (showRepeatTiming ? 1 : 0) + visibleScenes.length;
    final totalCount = 2 + config.scenes.length;
    final nothingMatches = matchCount == 0;

    return Scaffold(
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
            child: Row(
              children: [
                Expanded(
                  child: HubSearchField(
                    value: _query,
                    onChanged: setQuery,
                    hintText: 'Search scenes and settings',
                  ),
                ),
                if (_query.isNotEmpty) ...[
                  const SizedBox(width: 12),
                  Text('$matchCount of $totalCount', style: Theme.of(context).textTheme.bodySmall),
                ],
              ],
            ),
          ),
          Expanded(
            child: nothingMatches
                ? Center(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text('Nothing matches "$_query".'),
                        const SizedBox(height: 12),
                        TextButton(
                          onPressed: () => setQuery(''),
                          child: const Text('Clear search'),
                        ),
                      ],
                    ),
                  )
                : ListView(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                    children: [
                      if (showGlobalScene || showRepeatTiming) ...[
                        ResponsiveCardGrid(
                          maxColumns: 2,
                          children: [
                            if (showGlobalScene)
                              Card(
                                child: ListTile(
                                  leading: const Icon(Icons.public),
                                  title: const Text('Global scene'),
                                  subtitle: Text(
                                    config.globalScene == null
                                        ? 'Not set — unbound buttons do nothing when idle, or when the '
                                            'active scene leaves them unbound.'
                                        : 'Falls back to "${config.scene(config.globalScene!)?.name ?? config.globalScene}" '
                                            'for anything the active scene does not bind, and whenever nothing is running.',
                                  ),
                                  trailing: const Icon(Icons.chevron_right),
                                  onTap: () async {
                                    final choice = await _pickGlobalScene(context, config);
                                    if (choice == null) return;
                                    final saved = await store
                                        .saveConfig(config.copy()..globalScene = choice.sceneId);
                                    if (context.mounted && !saved) {
                                      ScaffoldMessenger.of(context).showSnackBar(
                                        SnackBar(content: Text(store.error ?? 'Could not save')),
                                      );
                                    }
                                  },
                                ),
                              ),
                            if (showRepeatTiming)
                              Card(
                                child: ListTile(
                                  leading: const Icon(Icons.speed),
                                  title: const Text('Default repeat timing'),
                                  subtitle: Text(
                                    'Applies to every button that repeats, unless that button sets '
                                    'its own. ${_describeRepeatTiming(config)}',
                                  ),
                                  trailing: const Icon(Icons.chevron_right),
                                  onTap: () async {
                                    final choice = await _pickDefaultRepeatTiming(context, config);
                                    if (choice == null) return;
                                    final saved = await store.saveConfig(
                                      config.copy()
                                        ..defaultRepeatDelay = choice.delay
                                        ..defaultRepeatInterval = choice.interval,
                                    );
                                    if (context.mounted && !saved) {
                                      ScaffoldMessenger.of(context).showSnackBar(
                                        SnackBar(content: Text(store.error ?? 'Could not save')),
                                      );
                                    }
                                  },
                                ),
                              ),
                          ],
                        ),
                        const SizedBox(height: 16),
                      ],
                      if (visibleScenes.isNotEmpty) ...[
                        Text('Scenes', style: Theme.of(context).textTheme.titleMedium),
                        const SizedBox(height: 8),
                        ResponsiveCardGrid(
                          maxColumns: 3,
                          children: [
                            for (final scene in visibleScenes)
                              Card(
                                color: scene.id == active
                                    ? Theme.of(context).colorScheme.primaryContainer
                                    : null,
                                child: ListTile(
                                  leading: Icon(scene.id == active
                                      ? Icons.play_circle_fill
                                      : Icons.movie_filter_outlined),
                                  title: Text(scene.name),
                                  subtitle: Text(
                                    '${scene.devices.length} device(s) · ${scene.bindings.length} rebound button(s)',
                                  ),
                                  onTap: () => _openEditor(context, store, scene),
                                  trailing: Row(
                                    mainAxisSize: MainAxisSize.min,
                                    children: [
                                      // Running a scene needs the engine; editing one
                                      // does not, and stays available below.
                                      Tooltip(
                                        message: store.hubRunning ? '' : 'The hub is stopped',
                                        child: scene.id == active
                                            ? TextButton(
                                                onPressed: store.hubRunning ? store.stopScene : null,
                                                child: const Text('Stop'),
                                              )
                                            : TextButton(
                                                onPressed: store.hubRunning
                                                    ? () => store.activateScene(scene.id)
                                                    : null,
                                                child: const Text('Start'),
                                              ),
                                      ),
                                      IconButton(
                                        icon: const Icon(Icons.delete_outline),
                                        onPressed: () => _delete(context, store, scene),
                                      ),
                                    ],
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ],
                    ],
                  ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _create(context, store),
        icon: const Icon(Icons.add),
        label: const Text('New scene'),
      ),
    );
  }
}

/// Asks which scene should be the global fallback -- a reference to one of
/// the user's own scenes, not a separate bindable set of its own.
///
/// A null `sceneId` in the result means "no fallback"; a null result means
/// the dialog was dismissed without a choice.
Future<({String? sceneId})?> _pickGlobalScene(BuildContext context, HubConfig config) {
  return showDialog<({String? sceneId})>(
    context: context,
    builder: (context) => SimpleDialog(
      title: const Text('Global scene'),
      children: [
        SimpleDialogOption(
          onPressed: () => Navigator.pop(context, (sceneId: null)),
          child: ListTile(
            leading: Icon(
              config.globalScene == null ? Icons.radio_button_checked : Icons.radio_button_unchecked,
            ),
            title: const Text('None'),
            subtitle: const Text('Unbound buttons do nothing when idle'),
            contentPadding: EdgeInsets.zero,
          ),
        ),
        if (config.scenes.isNotEmpty) const Divider(),
        for (final scene in config.scenes)
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, (sceneId: scene.id)),
            child: ListTile(
              leading: Icon(
                scene.id == config.globalScene ? Icons.radio_button_checked : Icons.radio_button_unchecked,
              ),
              title: Text(scene.name),
              subtitle: Text('${scene.bindings.length} button(s) bound'),
              contentPadding: EdgeInsets.zero,
            ),
          ),
      ],
    ),
  );
}

/// One line summarising the config-wide repeat timing, for the subtitle.
String _describeRepeatTiming(HubConfig config) {
  final waits = config.defaultRepeatDelay == 0
      ? 'repeats immediately'
      : 'waits ${config.defaultRepeatDelay.toStringAsFixed(1)}s first';
  final paced = config.defaultRepeatInterval == 0
      ? ''
      : ', then at most every ${config.defaultRepeatInterval.toStringAsFixed(1)}s';
  return 'Right now: $waits$paced.';
}

/// Edits `default_repeat_delay` / `default_repeat_interval` -- the timing
/// every button that repeats follows unless it sets its own.
///
/// The remote reports a held button roughly every 100ms and never says how
/// long it has been down, so this is what stops an ordinary short press from
/// firing three or four times: it is one setting for the whole remote
/// instead of a copy on every binding, because in practice they are all the
/// same number.
Future<({double delay, double interval})?> _pickDefaultRepeatTiming(
  BuildContext context,
  HubConfig config,
) {
  double delay = config.defaultRepeatDelay;
  double interval = config.defaultRepeatInterval;

  return showDialog<({double delay, double interval})>(
    context: context,
    builder: (context) => StatefulBuilder(
      builder: (context, setState) => AlertDialog(
        title: const Text('Default repeat timing'),
        content: SizedBox(
          width: 360,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'The remote reports a held button about every 100ms and never says how '
                'long it has been down. Without a wait, an ordinary short press looks '
                'identical to a hold and fires several times.',
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: Theme.of(context).colorScheme.outline,
                    ),
              ),
              const SizedBox(height: 12),
              LabeledSlider(
                label: 'Wait before repeating',
                value: delay,
                min: 0,
                max: 2.0,
                divisions: 20,
                onChanged: (value) => setState(() => delay = value),
                description: delay == 0
                    ? 'Repeats from the very first packet.'
                    : 'A press shorter than ${delay.toStringAsFixed(1)}s does not repeat at all.',
              ),
              const SizedBox(height: 8),
              LabeledSlider(
                label: 'Slowest repeat',
                value: interval,
                min: 0,
                max: 2.0,
                divisions: 20,
                onChanged: (value) => setState(() => interval = value),
                description: interval == 0
                    ? 'Follows the remote, about ten times a second.'
                    : 'At most once every ${interval.toStringAsFixed(1)}s while held.',
              ),
              const SizedBox(height: 8),
              Text(
                'A button can still override this for itself in its own bindings.',
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: Theme.of(context).colorScheme.outline,
                    ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(context, (delay: delay, interval: interval)),
            child: const Text('Save'),
          ),
        ],
      ),
    ),
  );
}

/// Turns a display name into an id the hub will accept (lowercase, `[a-z0-9_]`).
String slugify(String text) {
  final slug = text
      .toLowerCase()
      .replaceAll(RegExp(r'[^a-z0-9]+'), '_')
      .replaceAll(RegExp(r'^_+|_+$'), '');
  return slug.isEmpty ? 'item' : slug;
}

Future<String?> _promptForName(BuildContext context) {
  final controller = TextEditingController();
  return showDialog<String>(
    context: context,
    builder: (context) => AlertDialog(
      title: const Text('New scene'),
      content: TextField(
        controller: controller,
        autofocus: true,
        decoration: const InputDecoration(labelText: 'Name', hintText: 'Watch TV'),
        onSubmitted: (value) => Navigator.pop(context, value.trim()),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: () => Navigator.pop(context, controller.text.trim()),
          child: const Text('Create'),
        ),
      ],
    ),
  );
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.onCreate});

  final VoidCallback onCreate;

  @override
  Widget build(BuildContext context) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.movie_filter_outlined, size: 48),
            const SizedBox(height: 12),
            const Text('No scenes yet'),
            const SizedBox(height: 4),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Text(
                'A scene decides what every button means while it is running — '
                '"Watch TV" can send volume to the receiver, "Music" to the amp.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
            const SizedBox(height: 16),
            FilledButton.icon(onPressed: onCreate, icon: const Icon(Icons.add), label: const Text('New scene')),
          ],
        ),
      );
}

// ---------------------------------------------------------------------------

class SceneEditorPage extends StatefulWidget {
  const SceneEditorPage({super.key, required this.scene, required this.store});

  final SceneConfig scene;
  final HubStore store;

  @override
  State<SceneEditorPage> createState() => _SceneEditorPageState();
}

class _SceneEditorPageState extends State<SceneEditorPage> {
  late SceneConfig _scene;
  late TextEditingController _name;

  @override
  void initState() {
    super.initState();
    _scene = widget.scene.copy();
    _name = TextEditingController(text: _scene.name);
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  /// What an unbound button here would do instead.
  ///
  /// Empty when there is no global scene, and also when this scene *is* the
  /// global scene -- falling back to itself would just repeat "not bound
  /// here" for the same keys, which the engine already treats as a no-op.
  Map<String, Binding> _globalFallback(HubConfig config) {
    final globalId = config.globalScene;
    if (globalId == null || globalId == _scene.id) return const {};
    return config.scene(globalId)?.bindings ?? const {};
  }

  @override
  Widget build(BuildContext context) {
    final config = widget.store.config!;
    final globalName = config.globalScene == null || config.globalScene == _scene.id
        ? null
        : config.scene(config.globalScene!)?.name;

    return Scaffold(
      appBar: AppBar(
        title: Text(_scene.name.isEmpty ? 'Scene' : _scene.name),
        actions: [
          FilledButton(
            onPressed: () {
              _scene.name = _name.text.trim();
              Navigator.pop(context, _scene);
            },
            child: const Text('Save'),
          ),
          const SizedBox(width: 12),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _name,
            decoration: const InputDecoration(labelText: 'Name', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 8),
          Text('id: ${_scene.id}', style: Theme.of(context).textTheme.bodySmall),
          const SizedBox(height: 20),

          Text('Devices in this scene', style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 8),
          if (config.devices.isEmpty)
            const Text('No devices configured yet.')
          else
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final device in config.devices)
                  FilterChip(
                    label: Text(device.name),
                    selected: _scene.devices.contains(device.id),
                    onSelected: (selected) => setState(() {
                      selected ? _scene.devices.add(device.id) : _scene.devices.remove(device.id);
                    }),
                  ),
              ],
            ),
          const SizedBox(height: 24),

          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: ActionListEditor(
                title: 'When the scene starts',
                actions: _scene.onStart,
                config: config,
                api: widget.store.api,
                onChanged: (actions) => setState(() => _scene.onStart = actions),
              ),
            ),
          ),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: ActionListEditor(
                title: 'When the scene stops',
                actions: _scene.onStop,
                config: config,
                api: widget.store.api,
                onChanged: (actions) => setState(() => _scene.onStop = actions),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 6, 4, 0),
            child: Text(
              'Switching straight to another scene skips a power-off here for any '
              'device the next scene also uses, or that device\'s power policy '
              'keeps on. Only stopping outright -- or the off button -- runs all '
              'of it.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
          const SizedBox(height: 24),

          Card(
            child: ListTile(
              leading: const Icon(Icons.settings_remote),
              title: const Text('Button bindings'),
              subtitle: Text(
                globalName == null
                    ? '${_scene.bindings.length} button(s) rebound. No global scene is set, so '
                        'anything not bound here does nothing.'
                    : '${_scene.bindings.length} button(s) rebound. Anything not bound here '
                        'falls back to "$globalName".',
              ),
              trailing: const Icon(Icons.chevron_right),
              onTap: () async {
                final edited = await Navigator.push<Map<String, Binding>>(
                  context,
                  MaterialPageRoute(
                    builder: (_) => BindingsPage(
                      title: '${_scene.name} bindings',
                      scopeDescription: 'the "${_scene.name}" scene',
                      bindings: Map.of(_scene.bindings),
                      store: widget.store,
                      // What an unbound button would do instead, so the
                      // picture shows the fallback the subtitle promises.
                      fallback: _globalFallback(config),
                    ),
                  ),
                );
                if (edited != null) setState(() => _scene.bindings = edited);
              },
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------

/// Every button on the remote, with whatever this scope binds it to.
///
/// Shown as the picture of the remote rather than a list: what a button does
/// is a question about a physical layout, and "the one under my thumb" is not
/// a thing a person can find in an alphabetical list of forty-eight names.
class BindingsPage extends StatefulWidget {
  const BindingsPage({
    super.key,
    required this.title,
    required this.scopeDescription,
    required this.bindings,
    required this.store,
    this.fallback = const {},
  });

  final String title;

  /// Where these bindings apply, e.g. "the Watch TV scene".
  final String scopeDescription;

  final Map<String, Binding> bindings;
  final HubStore store;

  /// What an unbound button falls back to -- the global scene's own
  /// bindings, when this page is editing a different scene. Displayed,
  /// never edited here.
  final Map<String, Binding> fallback;

  @override
  State<BindingsPage> createState() => _BindingsPageState();
}

class _BindingsPageState extends State<BindingsPage> {
  late Map<String, Binding> _bindings;

  @override
  void initState() {
    super.initState();
    _bindings = Map.of(widget.bindings);
  }

  RemoteKeyStatus _status(String key) {
    final binding = _bindings[key];
    if (binding != null) {
      return RemoteKeyStatus(caption: binding.summary, highlighted: true);
    }
    final inherited = widget.fallback[key];
    return RemoteKeyStatus(
      caption: inherited == null ? null : 'falls back to ${inherited.summary}',
      marked: inherited != null,
    );
  }

  Future<void> _edit(String key, String label) async {
    final edited = await Navigator.push<Binding>(
      context,
      MaterialPageRoute(
        builder: (_) => BindingEditorPage(
          buttonLabel: label,
          binding: _bindings[key] ?? Binding(),
          config: widget.store.config!,
          api: widget.store.api,
          scopeDescription: widget.scopeDescription,
        ),
      ),
    );
    if (edited == null) return;
    setState(() {
      // An emptied binding is removed rather than stored blank, so the
      // button genuinely falls back instead of being bound to nothing.
      edited.isEmpty ? _bindings.remove(key) : _bindings[key] = edited;
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final labels = {for (final b in widget.store.buttons) b.key: b.label};

    return Scaffold(
      appBar: AppBar(
        title: Text(widget.title),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, _bindings),
            child: const Text('Done'),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Tap a button to choose what it does in ${widget.scopeDescription}.',
                  style: theme.textTheme.bodyMedium,
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 16,
                  runSpacing: 4,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    RemoteLegendDot(
                      color: scheme.primary,
                      label: '${_bindings.length} bound here',
                    ),
                    if (widget.fallback.isNotEmpty)
                      RemoteLegendDot(
                        color: scheme.tertiary,
                        label: '${widget.fallback.keys.where((k) => !_bindings.containsKey(k)).length}'
                            ' falling back to global',
                      ),
                  ],
                ),
              ],
            ),
          ),
          Expanded(
            child: RemoteBoard(
              buttons: widget.store.buttons,
              status: _status,
              onTap: (key) => _edit(key, labels[key] ?? key),
              hint: 'Tap a button to choose what it does',
              emptyCaption: 'not bound',
            ),
          ),
        ],
      ),
    );
  }
}
