/// Device list and editor.
///
/// The settings form is generated from the backend's JSON Schema rather than
/// written per backend, which is what lets a new backend appear here without
/// any change to the app.
library;

import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/config.dart';
import '../api/models.dart';
import '../main.dart';
import '../state/hub_store.dart';
import '../state/ui_prefs.dart';
import '../widgets/responsive_card_grid.dart';
import '../widgets/search_field.dart';
import '../widgets/section_card.dart';
import 'entity_picker.dart';
import 'ir_learn_screen.dart';
import 'remote_mapper.dart';
import 'scenes_screen.dart' show slugify;

class DevicesScreen extends StatefulWidget {
  const DevicesScreen({super.key});

  @override
  State<DevicesScreen> createState() => _DevicesScreenState();
}

/// Config keys worth matching a device search against, beyond name/id/
/// backend. Deliberately an allowlist rather than every value in
/// `device.config`: that map also holds backend credentials (a Home
/// Assistant long-lived token, say), and the schema carries nothing marking
/// a field as secret, so search must only look at fields that are plainly
/// not one.
const _searchableConfigKeys = {'host', 'address', 'ip', 'url', 'port', 'entity_id'};

class _DevicesScreenState extends State<DevicesScreen> {
  String _query = '';

  /// Seeded from [UiPrefs] once, on the first build -- see the identical
  /// note on `_HomeShellState._ensureTab` for why this can't just be a
  /// field initializer.
  bool _restoredQuery = false;

  Future<void> _openEditor(BuildContext context, HubStore store, DeviceConfig device) async {
    final edited = await Navigator.push<DeviceConfig>(
      context,
      MaterialPageRoute(builder: (_) => DeviceEditorPage(device: device, store: store)),
    );
    if (edited == null) return;

    final config = store.config!.copy();
    final index = config.devices.indexWhere((d) => d.id == device.id);
    index >= 0 ? config.devices[index] = edited : config.devices.add(edited);

    if (!await store.saveConfig(config) && context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(store.error ?? 'Could not save')),
      );
    }
  }

  Future<void> _create(BuildContext context, HubStore store) async {
    final backend = store.backends.firstOrNull;
    if (backend == null) return;
    await _openEditor(
      context,
      store,
      DeviceConfig(id: '', name: '', backend: backend.name),
    );
  }

  Future<void> _delete(BuildContext context, HubStore store, DeviceConfig device) async {
    final config = store.config!.copy()..devices.removeWhere((d) => d.id == device.id);
    if (!await store.saveConfig(config) && context.mounted) {
      // The hub rejects a delete that would orphan an action, and says which
      // one — far more useful than letting the app guess.
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(store.error ?? 'Could not delete')),
      );
    }
  }

  Iterable<String?> _haystack(DeviceConfig device, DeviceStatus? status) sync* {
    yield device.name;
    yield device.id;
    yield device.backend;
    yield status?.detail;
    for (final entry in device.config.entries) {
      if (!_searchableConfigKeys.contains(entry.key)) continue;
      final value = entry.value;
      if (value is String || value is num) yield '$value';
    }
  }

  @override
  Widget build(BuildContext context) {
    final store = HubScope.of(context);
    final prefs = PrefsScope.of(context);
    if (!_restoredQuery) {
      _restoredQuery = true;
      _query = prefs.get(kDevicesQuery);
    }

    void setQuery(String value) {
      setState(() => _query = value);
      prefs.set(kDevicesQuery, value);
    }

    final config = store.config;
    if (config == null) return const Center(child: CircularProgressIndicator());

    final statuses = {for (final s in store.status?.devices ?? <DeviceStatus>[]) s.id: s};
    final hasDevices = config.devices.isNotEmpty;
    final visible = hasDevices
        ? [
            for (final device in config.devices)
              if (matchesQuery(_query, _haystack(device, statuses[device.id]))) device,
          ]
        : <DeviceConfig>[];

    return Scaffold(
      body: !hasDevices
          ? Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.devices_other, size: 48),
                  const SizedBox(height: 12),
                  const Text('No devices yet'),
                  const SizedBox(height: 16),
                  FilledButton.icon(
                    onPressed: () => _create(context, store),
                    icon: const Icon(Icons.add),
                    label: const Text('Add device'),
                  ),
                ],
              ),
            )
          : Column(
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
                  child: Row(
                    children: [
                      Expanded(
                        child: HubSearchField(
                          value: _query,
                          onChanged: setQuery,
                          hintText: 'Search ${config.devices.length} device(s)',
                        ),
                      ),
                      if (_query.isNotEmpty) ...[
                        const SizedBox(width: 12),
                        Text(
                          '${visible.length} of ${config.devices.length}',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ],
                  ),
                ),
                Expanded(
                  child: visible.isEmpty
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
                      : SingleChildScrollView(
                          padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                          child: ResponsiveCardGrid(
                            maxColumns: 3,
                            children: [
                              for (final device in visible)
                                Card(
                                  child: ListTile(
                                    leading: Icon(
                                      statuses[device.id]?.ok == true
                                          ? Icons.check_circle
                                          : Icons.error_outline,
                                      color: statuses[device.id]?.ok == true
                                          ? Colors.greenAccent
                                          : Theme.of(context).colorScheme.error,
                                    ),
                                    title: Text(device.name),
                                    subtitle: Text(
                                      '${device.backend} · ${statuses[device.id]?.detail ?? 'not running'}',
                                    ),
                                    onTap: () => _openEditor(context, store, device),
                                    trailing: IconButton(
                                      icon: const Icon(Icons.delete_outline),
                                      onPressed: () => _delete(context, store, device),
                                    ),
                                  ),
                                ),
                            ],
                          ),
                        ),
                ),
              ],
            ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _create(context, store),
        icon: const Icon(Icons.add),
        label: const Text('Add device'),
      ),
    );
  }
}

// ---------------------------------------------------------------------------

class DeviceEditorPage extends StatefulWidget {
  const DeviceEditorPage({super.key, required this.device, required this.store});

  final DeviceConfig device;
  final HubStore store;

  @override
  State<DeviceEditorPage> createState() => _DeviceEditorPageState();
}

class _DeviceEditorPageState extends State<DeviceEditorPage> {
  late DeviceConfig _device;
  late TextEditingController _name;
  final Map<String, TextEditingController> _fields = {};
  List<CommandInfo> _commands = [];
  Map<String, String> _suggested = {};

  /// The device's suggestion for the SmartHome +/- keys: button key to
  /// `"up"`/`"down"`, separate from [_suggested] because the value is not a
  /// command name.
  Map<String, String> _suggestedAdjust = {};
  bool _busy = false;

  bool get _isNew => widget.device.id.isEmpty;

  BackendInfo? get _backend =>
      widget.store.backends.where((b) => b.name == _device.backend).firstOrNull;

  @override
  void initState() {
    super.initState();
    _device = widget.device.copy();
    _name = TextEditingController(text: _device.name);
    _rebuildFields();
    if (!_isNew) _loadCommands();
  }

  @override
  void dispose() {
    _name.dispose();
    for (final controller in _fields.values) {
      controller.dispose();
    }
    super.dispose();
  }

  /// One controller per schema property, seeded from the saved value.
  ///
  /// Objects and arrays get a JSON text field: generating a full nested form
  /// would be a lot of machinery for settings that are edited once, and a
  /// backend can always ship a flatter schema if it wants nicer fields.
  void _rebuildFields() {
    for (final controller in _fields.values) {
      controller.dispose();
    }
    _fields.clear();
    for (final entry in (_backend?.properties ?? {}).entries) {
      final schema = (entry.value as Map).cast<String, dynamic>();
      final value = _device.config[entry.key] ?? schema['default'];
      _fields[entry.key] = TextEditingController(
        text: value == null
            ? ''
            : (schema['type'] == 'object' || schema['type'] == 'array')
                ? const JsonEncoder.withIndent('  ').convert(value)
                : '$value',
      );
    }
  }

  /// A device that isn't running yet simply has no commands to offer; that is
  /// normal while it is being set up, not an error to report. The two loads
  /// are kept apart so a backend with no suggestions still gets its commands.
  Future<void> _loadCommands() async {
    try {
      final commands = await widget.store.api.deviceCommands(_device.id);
      if (mounted) setState(() => _commands = commands);
    } catch (_) {
      // Nothing to show yet.
    }
    try {
      final suggested = await widget.store.api.suggestedBindings(_device.id);
      if (mounted) {
        setState(() {
          _suggested = suggested.bindings;
          _suggestedAdjust = suggested.adjust;
        });
      }
    } catch (_) {
      // Mapping is an offer, not a requirement.
    }
  }

  void _snack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// A one-field dialog, used for the pairing secret and for the scene name.
  Future<String?> _ask({
    required String title,
    required String label,
    required String action,
    String initial = '',
    String? help,
    bool digitsOnly = false,
    bool multiline = false,
  }) {
    final controller = TextEditingController(text: initial);
    return showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (help != null) ...[
              Text(help, style: Theme.of(context).textTheme.bodySmall),
              const SizedBox(height: 16),
            ],
            TextField(
              controller: controller,
              autofocus: true,
              keyboardType: digitsOnly ? TextInputType.number : null,
              // A Home Assistant token is a couple of hundred characters, and
              // reading one back to check it in a single-line box is hopeless.
              maxLines: multiline ? 4 : 1,
              style: multiline ? const TextStyle(fontFamily: 'monospace', fontSize: 12) : null,
              decoration: InputDecoration(
                labelText: label,
                border: const OutlineInputBorder(),
                alignLabelWithHint: multiline,
              ),
              onSubmitted: multiline ? null : (value) => Navigator.pop(context, value),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text),
            child: Text(action),
          ),
        ],
      ),
    );
  }

  /// A confirm-only dialog, for a handshake with nothing to type back —
  /// see [_pair] and `Pairable.pairInputLabel` on the hub side.
  Future<bool?> _confirm({required String title, required String action, String? help}) {
    return showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: help == null ? null : Text(help),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: Text(action)),
        ],
      ),
    );
  }

  /// Fills in the address from whatever announces itself on the network.
  ///
  /// Discovery is protocol-specific — there is no way to ask "what is out
  /// there" without knowing what to listen for — so this is gated on
  /// [BackendInfo.discoverable] rather than pretending to be general. That
  /// flag, and which field its result fills in, come from the hub: the app
  /// never keeps its own list of which backend names happen to have a
  /// `/discover` route, the same way it does not keep one for `pairable`.
  Future<void> _discover() async {
    final field = _backend?.discoverField;
    if (field == null || field.isEmpty) return;

    setState(() => _busy = true);
    List<DiscoveredDevice> found = [];
    try {
      found = await widget.store.api.discover(_device.backend);
    } catch (err) {
      _snack('$err');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    if (!mounted || found.isEmpty) {
      if (found.isEmpty) {
        _snack('Nothing answered. Type the address instead — the device may be off or asleep.');
      }
      return;
    }

    final chosen = await showDialog<DiscoveredDevice>(
      context: context,
      builder: (context) => SimpleDialog(
        title: const Text('Found on the network'),
        children: [
          for (final device in found)
            SimpleDialogOption(
              onPressed: () => Navigator.pop(context, device),
              child: ListTile(
                title: Text(device.name),
                subtitle: Text(
                  device.version.isEmpty ? device.host : '${device.host} · ${device.version}',
                ),
                contentPadding: EdgeInsets.zero,
              ),
            ),
        ],
      ),
    );
    if (chosen != null && mounted) setState(() => _fields[field]?.text = chosen.host);
  }

  /// Runs the backend's one-time handshake, in the backend's own words.
  ///
  /// Both halves of this are the same for every pairable backend — ask the
  /// device to begin, send back what the user supplies — but the words are
  /// not: an Android TV shows a six-digit code on screen, a Home Assistant
  /// issues a long token from a web page. So the copy comes from the hub,
  /// and this screen stays ignorant of which is which.
  Future<void> _pair() async {
    setState(() => _busy = true);
    final started = await widget.store.api.pairStart(_device.id);
    if (mounted) setState(() => _busy = false);
    if (!started.ok) {
      _snack(started.detail);
      return;
    }

    final backend = _backend;
    final what = (backend?.pairLabel ?? '').isEmpty ? 'Pair' : backend!.pairLabel;
    final inputLabel = backend?.pairInputLabel ?? '';

    final String code;
    if (inputLabel.isEmpty) {
      // Nothing to type back — the hint already said what to do on the
      // device itself (accept a prompt, press a button). Just confirm once
      // that is done, rather than asking for input the backend has no use
      // for.
      final confirmed = await _confirm(
        title: '$what — ${_device.name}',
        action: 'Done',
        help: started.detail,
      );
      if (confirmed != true) return;
      code = '';
    } else {
      final typed = await _ask(
        title: '$what — ${_device.name}',
        label: inputLabel,
        action: 'Connect',
        help: started.detail,
        // Only a code is all digits, and only a long value wants a taller box,
        // so one flag from the backend answers both.
        digitsOnly: !(backend?.pairInputMultiline ?? false),
        multiline: backend?.pairInputMultiline ?? false,
      );
      if (typed == null || typed.trim().isEmpty) return;
      code = typed.trim();
    }

    setState(() => _busy = true);
    final finished = await widget.store.api.pairFinish(_device.id, code);
    if (!mounted) return;
    setState(() => _busy = false);
    _snack(finished.ok ? 'Paired' : finished.detail);
    if (finished.ok) {
      await widget.store.refreshStatus();
      await _loadCommands();
    }
  }

  /// Opens the capture-and-name flow for a backend implementing `Learnable`.
  ///
  /// Commands are reloaded on return rather than pushed live during the
  /// learn screen's own lifetime, the same way `_pair` refreshes afterwards
  /// rather than mid-handshake -- what changed only matters once this screen
  /// is looking at it again.
  Future<void> _learnCommands() async {
    final backend = _backend;
    if (backend == null) return;
    await openIrLearnPage(
      context,
      store: widget.store,
      deviceId: _device.id,
      deviceName: _device.name,
      backend: backend,
    );
    await _loadCommands();
  }

  /// Narrows a Home Assistant down to the entities worth putting on a remote.
  ///
  /// Written straight into the form's `entities` field rather than saved, so
  /// it behaves like every other edit on this page: nothing is committed
  /// until Save. The list itself has to come from the running device, which
  /// is why this is disabled with the hub stopped.
  Future<void> _chooseEntities() async {
    setState(() => _busy = true);
    List<EntityInfo> entities = [];
    try {
      entities = await widget.store.api.deviceEntities(_device.id);
    } catch (err) {
      _snack('$err');
      if (mounted) setState(() => _busy = false);
      return;
    }
    if (!mounted) return;
    setState(() => _busy = false);

    final chosen = await showEntityPicker(
      context: context,
      deviceName: _device.name,
      entities: entities,
      selected: _currentEntities(),
    );
    if (chosen == null || !mounted) return;

    setState(() {
      _fields['entities']?.text = const JsonEncoder.withIndent('  ').convert(chosen);
    });
    _snack(chosen.isEmpty
        ? 'No entities picked — save to clear them'
        : '${chosen.length} entities picked. Save to apply.');
  }

  /// Whatever the entities field holds right now, however it got there.
  ///
  /// Read from the form rather than from `_device.config` so a pick made,
  /// then reopened before saving, starts from what is on screen.
  List<String> _currentEntities() {
    final text = _fields['entities']?.text.trim() ?? '';
    if (text.isEmpty) return [];
    try {
      final decoded = jsonDecode(text);
      if (decoded is List) return decoded.map((e) => '$e').toList();
    } catch (_) {
      // Hand-edited into something unparseable; the picker starts empty and
      // Save will report the JSON error as it always would.
    }
    return [];
  }

  /// Which scene the mapping should land in: a new one, or one that exists.
  ///
  /// A null `sceneId` in the result means "a new scene"; a null result means
  /// the user backed out.
  Future<({String? sceneId})?> _chooseTarget() {
    final scenes = widget.store.config?.scenes ?? [];
    return showDialog<({String? sceneId})>(
      context: context,
      builder: (context) => SimpleDialog(
        title: const Text('Map into which scene?'),
        children: [
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, (sceneId: null)),
            child: const ListTile(
              leading: Icon(Icons.add),
              title: Text('A new scene'),
              subtitle: Text('Starts from this device’s own suggestions'),
              contentPadding: EdgeInsets.zero,
            ),
          ),
          if (scenes.isNotEmpty) const Divider(),
          for (final scene in scenes)
            SimpleDialogOption(
              onPressed: () => Navigator.pop(context, (sceneId: scene.id)),
              child: ListTile(
                leading: const Icon(Icons.movie_filter_outlined),
                title: Text(scene.name),
                subtitle: Text(
                  scene.bindings.isEmpty
                      ? 'nothing bound yet'
                      : '${scene.bindings.length} buttons already bound',
                ),
                contentPadding: EdgeInsets.zero,
              ),
            ),
        ],
      ),
    );
  }

  /// Points remote buttons at this device, on the picture of the remote.
  ///
  /// Saved through the ordinary config route, so the result is an ordinary
  /// scene: editable button by button afterwards, and validated by the hub
  /// exactly like one built by hand. Only the buttons picked in the mapper
  /// are written -- an existing scene keeps every binding not chosen there.
  Future<void> _mapRemote() async {
    final choice = await _chooseTarget();
    if (choice == null || !mounted) return;

    SceneConfig? scene;
    String targetName;
    if (choice.sceneId == null) {
      final name = await _ask(
        title: 'New scene',
        label: 'Scene name',
        action: 'Continue',
        initial: 'Watch ${_device.name}',
      );
      if (name == null || name.trim().isEmpty || !mounted) return;
      targetName = name.trim();
      if (widget.store.config!.scene(slugify(targetName)) != null) {
        _snack('A scene called "$targetName" already exists');
        return;
      }
    } else {
      scene = widget.store.config!.scene(choice.sceneId!);
      if (scene == null) return;
      targetName = scene.name;
    }

    final assignments = await Navigator.push<Map<String, Binding>>(
      context,
      MaterialPageRoute(
        builder: (_) => RemoteMapperPage(
          buttons: widget.store.buttons,
          deviceId: _device.id,
          deviceName: _device.name,
          commands: _commands,
          suggested: _suggested,
          suggestedAdjust: _suggestedAdjust,
          existing: scene?.bindings ?? const {},
          targetName: targetName,
          // Nothing to lose in a scene that does not exist yet, so start from
          // the suggestions; an existing scene starts blank so that nothing
          // is overwritten without being chosen.
          preassignSuggested: scene == null,
        ),
      ),
    );
    if (assignments == null || assignments.isEmpty || !mounted) return;

    final config = widget.store.config!.copy();
    if (scene == null) {
      config.scenes.add(SceneConfig(
        id: slugify(targetName),
        name: targetName,
        devices: [_device.id],
        bindings: assignments,
      ));
    } else {
      final target = config.scene(scene.id)!;
      final replaced = assignments.keys.where(target.bindings.containsKey).length;
      target.bindings.addAll(assignments);
      // A scene driving this device should list it, or the scene screen would
      // show a device-less scene that nonetheless commands one.
      if (!target.devices.contains(_device.id)) target.devices.add(_device.id);
      if (replaced > 0) _snack('$replaced existing binding(s) replaced');
    }

    if (await widget.store.saveConfig(config)) {
      _snack('${assignments.length} buttons now drive ${_device.name} in "$targetName"');
    } else {
      _snack(widget.store.error ?? 'Could not save');
    }
  }

  /// Reads the form back into `_device.config`, or returns the first problem.
  String? _collect() {
    final config = <String, dynamic>{};
    for (final entry in (_backend?.properties ?? {}).entries) {
      final schema = (entry.value as Map).cast<String, dynamic>();
      final text = _fields[entry.key]!.text.trim();
      if (text.isEmpty) continue;

      switch (schema['type']) {
        case 'number':
        case 'integer':
          final number = num.tryParse(text);
          if (number == null) return '${entry.key} must be a number';
          config[entry.key] = number;
        case 'boolean':
          config[entry.key] = text.toLowerCase() == 'true';
        case 'object':
        case 'array':
          try {
            config[entry.key] = jsonDecode(text);
          } catch (err) {
            return '${entry.key} is not valid JSON: $err';
          }
        default:
          config[entry.key] = text;
      }
    }
    _device.config = config;
    _device.name = _name.text.trim();
    if (_device.name.isEmpty) return 'Give the device a name';
    if (_isNew) _device.id = slugify(_device.name);
    return null;
  }

  Future<void> _test(String command) async {
    final result = await widget.store.api.testCommand(_device.id, command);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(result.ok ? 'Sent $command' : result.detail)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final backends = widget.store.backends;
    final status = widget.store.status?.devices
        .where((s) => s.id == _device.id)
        .firstOrNull;

    return Scaffold(
      appBar: AppBar(
        title: Text(_isNew ? 'Add device' : _device.name),
        actions: [
          FilledButton(
            onPressed: () {
              final problem = _collect();
              if (problem != null) {
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(problem)));
                return;
              }
              Navigator.pop(context, _device);
            },
            child: const Text('Save'),
          ),
          const SizedBox(width: 12),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_busy) ...[
            const LinearProgressIndicator(),
            const SizedBox(height: 16),
          ],

          SectionCard(
            title: 'Identity',
            trailing: status == null
                ? null
                : Chip(
                    avatar: Icon(
                      status.ok ? Icons.check_circle : Icons.error_outline,
                      size: 18,
                      color: status.ok ? Colors.greenAccent : Theme.of(context).colorScheme.error,
                    ),
                    label: Text(status.detail),
                    visualDensity: VisualDensity.compact,
                  ),
            children: [
              TextField(
                controller: _name,
                decoration: const InputDecoration(
                  labelText: 'Name',
                  hintText: 'Living Room TV',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                isExpanded: true,
                initialValue: _device.backend,
                decoration:
                    const InputDecoration(labelText: 'Backend', border: OutlineInputBorder()),
                items: [
                  for (final backend in backends)
                    DropdownMenuItem(value: backend.name, child: Text(backend.label)),
                ],
                onChanged: _isNew
                    ? (value) {
                        if (value == null) return;
                        setState(() {
                          _device.backend = value;
                          _device.config = {};
                          _rebuildFields();
                        });
                      }
                    // Changing backend on a saved device would invalidate every
                    // command already bound to it, so it is add-time only.
                    : null,
              ),
              if (_backend != null) ...[
                const SizedBox(height: 6),
                Text(_backend!.description, style: Theme.of(context).textTheme.bodySmall),
              ],
            ],
          ),
          const SizedBox(height: 16),

          // Everything the backend itself needs to reach the device -- its
          // schema-driven fields -- lives together, the same way a device's
          // connection details belong together wherever they're configured.
          SectionCard(
            title: 'Connection',
            children: [
              if (_backend?.discoverable ?? false) ...[
                Align(
                  alignment: Alignment.centerLeft,
                  child: OutlinedButton.icon(
                    onPressed: _busy ? null : _discover,
                    icon: const Icon(Icons.wifi_find),
                    label: const Text('Find on the network'),
                  ),
                ),
                const SizedBox(height: 16),
              ],
              if (_fields.isEmpty)
                const Text('This backend needs no settings.')
              else
                for (final entry in (_backend?.properties ?? {}).entries) ...[
                  _field(entry.key, (entry.value as Map).cast<String, dynamic>()),
                  // The entities field is a JSON box like any other array, and
                  // hand-typing a dozen entity ids into it is exactly the step
                  // this button exists to remove. Left visible underneath so a
                  // pick can still be checked, and edited if it has to be.
                  if (entry.key == 'entities') ...[
                    const SizedBox(height: 8),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: FilledButton.tonalIcon(
                        onPressed: (_busy || _isNew || !widget.store.hubRunning)
                            ? null
                            : _chooseEntities,
                        icon: const Icon(Icons.checklist),
                        label: const Text('Choose entities'),
                      ),
                    ),
                    if (_isNew)
                      Padding(
                        padding: const EdgeInsets.only(top: 6),
                        child: Text(
                          'Save the device first, then connect it — the list comes from '
                          'the instance itself.',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      )
                    else if (!widget.store.hubRunning)
                      Padding(
                        padding: const EdgeInsets.only(top: 6),
                        child: Text(
                          'Listing entities asks Home Assistant what it has, so it needs '
                          'the hub running. Start it from Settings.',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ),
                  ],
                  const SizedBox(height: 16),
                ],
            ],
          ),
          const SizedBox(height: 16),

          SectionCard(
            title: 'Behaviour',
            subtitle: 'How freely scenes may switch this device on and off.',
            children: [
              DropdownButtonFormField<String>(
                isExpanded: true,
                initialValue: _device.powerPolicy,
                decoration:
                    const InputDecoration(labelText: 'Power policy', border: OutlineInputBorder()),
                items: const [
                  DropdownMenuItem(value: 'managed', child: Text('Managed — follow the scene')),
                  DropdownMenuItem(
                      value: 'leave_on', child: Text('Leave on — off only on an explicit stop')),
                  DropdownMenuItem(
                      value: 'manual', child: Text('Manual — never send power commands')),
                ],
                onChanged: (value) => setState(() => _device.powerPolicy = value ?? 'managed'),
              ),
            ],
          ),

          // Every word here comes from the backend. The handshake generalises;
          // "a code on its screen" does not, and would be plainly wrong for a
          // Home Assistant token pasted out of a browser.
          if ((_backend?.pairable ?? false) && !_isNew) ...[
            const SizedBox(height: 16),
            SectionCard(
              title: _backend!.pairLabel.isEmpty ? 'Pairing' : _backend!.pairLabel,
              subtitle: widget.store.hubRunning
                  ? _backend!.pairHint
                  : 'This talks to the device, so it needs the hub running. '
                      'Start it from Settings.',
              children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: FilledButton.tonalIcon(
                    onPressed: _busy || !widget.store.hubRunning ? null : _pair,
                    icon: const Icon(Icons.link),
                    label:
                        Text(_backend!.pairLabel.isEmpty ? 'Pair this device' : _backend!.pairLabel),
                  ),
                ),
              ],
            ),
          ],

          // Learning, trying a command and the hub-not-running notice are all
          // "talking to the device right now", so they live in one place.
          if (((_backend?.learnable ?? false) && !_isNew) ||
              (!widget.store.hubRunning && !_isNew) ||
              _commands.isNotEmpty) ...[
            const SizedBox(height: 16),
            SectionCard(
              title: _backend?.learnLabel.isNotEmpty == true ? _backend!.learnLabel : 'Commands',
              children: [
                if ((_backend?.learnable ?? false) && !_isNew) ...[
                  // Learning is its own screen rather than a button here, the
                  // same way mapping the remote is: it is a whole flow
                  // (capture, confirm, name, save, repeat), not a single
                  // request-response like pairing.
                  Text(
                    widget.store.hubRunning
                        ? _backend!.learnHint
                        : 'Learning talks to the receiver, so it needs the hub running. '
                            'Start it from Settings.',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 8),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: FilledButton.tonalIcon(
                      onPressed: _busy || !widget.store.hubRunning ? null : _learnCommands,
                      icon: const Icon(Icons.sensors),
                      label: const Text('Learn commands'),
                    ),
                  ),
                  if (!widget.store.hubRunning || _commands.isNotEmpty) const SizedBox(height: 16),
                ],
                if (!widget.store.hubRunning && !_isNew)
                  _Note(
                    icon: Icons.info_outline,
                    text: 'Mapping the remote and trying a command both ask this device what '
                        'it can do, so they need the hub running. Everything above can still '
                        'be edited and saved.',
                  ),
                if (_commands.isNotEmpty) ...[
                  Text(
                    'Sends it right now, so the device can be checked while it is being set up.',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      for (final command in _commands)
                        ActionChip(
                            label: Text(command.label), onPressed: () => _test(command.name)),
                    ],
                  ),
                ],
              ],
            ),
          ],

          if (_commands.isNotEmpty && !_isNew) ...[
            const SizedBox(height: 16),
            SectionCard(
              title: 'Remote mapping',
              subtitle: _suggested.isEmpty && _suggestedAdjust.isEmpty
                  ? 'Point remote buttons at this device on the picture of the remote, '
                      'into a new scene or one that already exists.'
                  : 'Point remote buttons at this device on the picture of the remote. '
                      'This device suggests ${_suggested.length + _suggestedAdjust.length} of '
                      'them; only what you pick gets written.',
              children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: FilledButton.tonalIcon(
                    onPressed: _busy ? null : _mapRemote,
                    icon: const Icon(Icons.settings_remote),
                    label: const Text('Map the remote to this device'),
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }

  Widget _field(String key, Map<String, dynamic> schema) {
    final type = schema['type'];
    final title = (schema['title'] ?? key) as String;
    final help = schema['description'] as String?;

    if (type == 'boolean') {
      final controller = _fields[key]!;
      return SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: Text(title),
        subtitle: help == null ? null : Text(help),
        value: controller.text.toLowerCase() == 'true',
        onChanged: (value) => setState(() => controller.text = '$value'),
      );
    }

    final options = (schema['enum'] as List?)?.map((option) => '$option').toList();
    if (options != null && options.isNotEmpty) {
      final controller = _fields[key]!;
      final current = controller.text.trim();
      // A hand-edited config can hold a value this list has never heard of.
      // Offering it rather than dropping it is what stops merely opening the
      // form from quietly rewriting a setting nobody came here to change.
      final choices = [if (current.isNotEmpty && !options.contains(current)) current, ...options];
      return DropdownButtonFormField<String>(
        isExpanded: true,
        initialValue: choices.contains(current) ? current : null,
        decoration: InputDecoration(
          labelText: title,
          helperText: help,
          helperMaxLines: 3,
          border: const OutlineInputBorder(),
        ),
        items: [
          for (final choice in choices) DropdownMenuItem(value: choice, child: Text(choice)),
        ],
        onChanged: (value) => setState(() => controller.text = value ?? ''),
      );
    }

    final isJson = type == 'object' || type == 'array';
    return TextField(
      controller: _fields[key],
      maxLines: isJson ? 8 : 1,
      style: isJson ? const TextStyle(fontFamily: 'monospace', fontSize: 13) : null,
      keyboardType: (type == 'number' || type == 'integer')
          ? const TextInputType.numberWithOptions(decimal: true)
          : null,
      decoration: InputDecoration(
        labelText: title,
        helperText: help,
        helperMaxLines: 3,
        border: const OutlineInputBorder(),
        alignLabelWithHint: isJson,
      ),
    );
  }
}

class _Note extends StatelessWidget {
  const _Note({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    final colour = Theme.of(context).colorScheme.onSurfaceVariant;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 18, color: colour),
          const SizedBox(width: 8),
          Expanded(
            child: Text(text, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: colour)),
          ),
        ],
      ),
    );
  }
}
