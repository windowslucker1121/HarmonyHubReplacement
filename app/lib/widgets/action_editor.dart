/// Editing a macro: an ordered list of actions.
///
/// Device commands come from the hub rather than being typed, because the
/// backend is the only thing that knows what a device accepts. That turns a
/// mistyped command into an impossible state instead of a binding that
/// silently does nothing when pressed.
library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/config.dart';
import '../api/models.dart';

class ActionListEditor extends StatelessWidget {
  const ActionListEditor({
    super.key,
    required this.title,
    required this.actions,
    required this.config,
    required this.api,
    required this.onChanged,
    this.emptyHint = 'Nothing yet',
  });

  final String title;
  final List<HubAction> actions;
  final HubConfig config;
  final HubApi api;
  final ValueChanged<List<HubAction>> onChanged;
  final String emptyHint;

  Future<void> _add(BuildContext context) async {
    final action = await showActionDialog(context: context, config: config, api: api);
    if (action != null) onChanged([...actions, action]);
  }

  Future<void> _edit(BuildContext context, int index) async {
    final action = await showActionDialog(
      context: context,
      config: config,
      api: api,
      initial: actions[index],
    );
    if (action != null) {
      final updated = [...actions];
      updated[index] = action;
      onChanged(updated);
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text(title, style: Theme.of(context).textTheme.titleSmall),
            const Spacer(),
            TextButton.icon(
              onPressed: () => _add(context),
              icon: const Icon(Icons.add, size: 18),
              label: const Text('Add'),
            ),
          ],
        ),
        if (actions.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Text(emptyHint, style: TextStyle(color: scheme.outline)),
          )
        else
          // The app-wide SelectionArea (see main.dart) turns a drag anywhere
          // in its subtree into a text-selection drag by default, which wins
          // the gesture arena against the drag handle below and reorders
          // nothing. Opting this list out keeps its rows draggable; their
          // text stops being selectable, which matters far less here than
          // reordering does.
          SelectionContainer.disabled(
            child: ReorderableListView(
              shrinkWrap: true,
              buildDefaultDragHandles: false,
              physics: const NeverScrollableScrollPhysics(),
              onReorder: (oldIndex, newIndex) {
                // ReorderableListView reports the target index before the item
                // has been removed, so anything moving down is off by one.
                if (newIndex > oldIndex) newIndex -= 1;
                final updated = [...actions];
                updated.insert(newIndex, updated.removeAt(oldIndex));
                onChanged(updated);
              },
              children: [
                for (var i = 0; i < actions.length; i++)
                  ListTile(
                    key: ValueKey('$title-$i-${actions[i].describe()}'),
                    dense: true,
                    leading: ReorderableDragStartListener(
                      index: i,
                      child: const Icon(Icons.drag_handle, size: 20),
                    ),
                    title: Text(actions[i].describe()),
                    subtitle: Text('${i + 1}. ${actions[i].type}'),
                    onTap: () => _edit(context, i),
                    trailing: IconButton(
                      icon: const Icon(Icons.delete_outline, size: 20),
                      onPressed: () => onChanged([...actions]..removeAt(i)),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }
}

/// Builds or edits one action. Returns null if cancelled.
Future<HubAction?> showActionDialog({
  required BuildContext context,
  required HubConfig config,
  required HubApi api,
  HubAction? initial,
}) {
  return showDialog<HubAction>(
    context: context,
    builder: (context) => _ActionDialog(config: config, api: api, initial: initial),
  );
}

class _ActionDialog extends StatefulWidget {
  const _ActionDialog({required this.config, required this.api, this.initial});

  final HubConfig config;
  final HubApi api;
  final HubAction? initial;

  @override
  State<_ActionDialog> createState() => _ActionDialogState();
}

class _ActionDialogState extends State<_ActionDialog> {
  late String _type;
  String? _deviceId;
  String? _command;
  String? _sceneId;
  late TextEditingController _seconds;
  late String _direction;

  List<CommandInfo> _commands = [];
  bool _loadingCommands = false;
  String? _commandError;

  /// The command's parameters, keyed by name. Seeded from the action being
  /// edited (if any) and topped up with each parameter's own default --
  /// which is also what an older saved binding gets for a parameter that
  /// did not exist when it was bound.
  Map<String, dynamic> _params = {};

  /// Which command [_params] currently belongs to, so switching the command
  /// dropdown resets to that command's own defaults instead of carrying an
  /// unrelated command's values along -- while the *first* sync, right
  /// after commands load, keeps whatever the action being edited already had.
  String? _paramsCommand;

  final Map<String, TextEditingController> _paramControllers = {};

  @override
  void initState() {
    super.initState();
    final initial = widget.initial;
    _type = initial?.type ?? 'device';
    _deviceId = initial?.device ?? widget.config.devices.firstOrNull?.id;
    _command = initial?.command;
    _sceneId = initial?.scene;
    _seconds = TextEditingController(text: (initial?.seconds ?? 1.0).toString());
    _direction = initial?.direction ?? 'up';
    _params = Map<String, dynamic>.from(initial?.params ?? {});
    _paramsCommand = initial?.type == 'device' ? initial?.command : null;
    if (_type == 'device' && _deviceId != null) _loadCommands(_deviceId!);
  }

  @override
  void dispose() {
    _seconds.dispose();
    _disposeParamControllers();
    super.dispose();
  }

  CommandInfo? get _selectedCommand {
    for (final command in _commands) {
      if (command.name == _command) return command;
    }
    return null;
  }

  Map<String, dynamic> _defaultParams(CommandInfo? command) {
    final properties = (command?.params?['properties'] as Map?)?.cast<String, dynamic>();
    if (properties == null) return {};
    final defaults = <String, dynamic>{};
    for (final entry in properties.entries) {
      final prop = (entry.value as Map).cast<String, dynamic>();
      if (prop.containsKey('default')) defaults[entry.key] = prop['default'];
    }
    return defaults;
  }

  void _disposeParamControllers() {
    for (final controller in _paramControllers.values) {
      controller.dispose();
    }
    _paramControllers.clear();
  }

  void _syncParamsWithCommand() {
    if (_command == _paramsCommand) {
      final defaults = _defaultParams(_selectedCommand);
      for (final entry in defaults.entries) {
        _params.putIfAbsent(entry.key, () => entry.value);
      }
      return;
    }
    _disposeParamControllers();
    _params = _defaultParams(_selectedCommand);
    _paramsCommand = _command;
  }

  bool get _requiredParamsFilled {
    final required = ((_selectedCommand?.params?['required'] as List?) ?? []).cast<String>();
    for (final name in required) {
      final value = _params[name];
      if (value == null) return false;
      if (value is String && value.trim().isEmpty) return false;
    }
    return true;
  }

  Future<void> _loadCommands(String deviceId) async {
    setState(() {
      _loadingCommands = true;
      _commandError = null;
      _commands = [];
    });
    try {
      final commands = await widget.api.deviceCommands(deviceId);
      if (!mounted) return;
      setState(() {
        _commands = commands;
        // Keep the existing choice if it survived; otherwise pick the first
        // so the dialog never sits in an unsaveable state.
        if (_command == null || !commands.any((c) => c.name == _command)) {
          _command = commands.firstOrNull?.name;
        }
        _syncParamsWithCommand();
      });
    } catch (err) {
      if (!mounted) return;
      setState(() => _commandError = 'Could not load commands: $err');
    } finally {
      if (mounted) setState(() => _loadingCommands = false);
    }
  }

  bool get _valid => switch (_type) {
        'device' => _deviceId != null && _command != null && _requiredParamsFilled,
        'scene' => true, // a null scene is the Off action
        'adjust' => true, // direction always has a value
        _ => double.tryParse(_seconds.text) != null,
      };

  HubAction _build() => switch (_type) {
        'device' => HubAction(
            type: 'device',
            device: _deviceId,
            command: _command,
            params: Map<String, dynamic>.from(_params),
          ),
        'scene' => HubAction.scene(_sceneId),
        // The fallback device/target have no editor of their own yet, so
        // editing an existing adjust action keeps whatever it already had
        // rather than a UI silently dropping it.
        'adjust' => HubAction.adjust(
            _direction,
            device: widget.initial?.type == 'adjust' ? widget.initial?.device : null,
            target: widget.initial?.type == 'adjust' ? widget.initial?.target : null,
          ),
        _ => HubAction.delay(double.parse(_seconds.text)),
      };

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(widget.initial == null ? 'Add action' : 'Edit action'),
      content: SizedBox(
        width: 420,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SegmentedButton<String>(
              segments: const [
                ButtonSegment(value: 'device', label: Text('Device'), icon: Icon(Icons.tv)),
                ButtonSegment(value: 'scene', label: Text('Scene'), icon: Icon(Icons.movie_filter_outlined)),
                ButtonSegment(value: 'adjust', label: Text('Adjust'), icon: Icon(Icons.tune)),
                ButtonSegment(value: 'delay', label: Text('Wait'), icon: Icon(Icons.timer_outlined)),
              ],
              selected: {_type},
              onSelectionChanged: (values) {
                setState(() => _type = values.first);
                if (_type == 'device' && _deviceId != null) _loadCommands(_deviceId!);
              },
            ),
            const SizedBox(height: 20),
            if (_type == 'device') ..._deviceFields(),
            if (_type == 'scene') _sceneField(),
            if (_type == 'adjust') ..._adjustFields(context),
            if (_type == 'delay')
              TextField(
                controller: _seconds,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(
                  labelText: 'Seconds',
                  helperText: 'Equipment often ignores a command sent right after power-on.',
                  border: OutlineInputBorder(),
                ),
                onChanged: (_) => setState(() {}),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: _valid ? () => Navigator.pop(context, _build()) : null,
          child: const Text('Save'),
        ),
      ],
    );
  }

  List<Widget> _deviceFields() {
    if (widget.config.devices.isEmpty) {
      return [const Text('Add a device first — there is nothing to send a command to.')];
    }
    return [
      DropdownButtonFormField<String>(
        isExpanded: true,
        initialValue: _deviceId,
        decoration: const InputDecoration(labelText: 'Device', border: OutlineInputBorder()),
        items: [
          for (final device in widget.config.devices)
            DropdownMenuItem(value: device.id, child: Text('${device.name}  (${device.backend})')),
        ],
        onChanged: (value) {
          setState(() => _deviceId = value);
          if (value != null) _loadCommands(value);
        },
      ),
      const SizedBox(height: 16),
      if (_loadingCommands)
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 12),
          child: LinearProgressIndicator(),
        )
      else if (_commandError != null)
        Text(_commandError!, style: TextStyle(color: Theme.of(context).colorScheme.error))
      else if (_commands.isEmpty)
        const Text('This device offers no commands yet. Check its configuration.')
      else
        Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            DropdownButtonFormField<String>(
              isExpanded: true,
              initialValue: _command,
              decoration: const InputDecoration(labelText: 'Command', border: OutlineInputBorder()),
              items: [
                for (final command in _commands)
                  DropdownMenuItem(value: command.name, child: Text(command.label)),
              ],
              onChanged: (value) => setState(() {
                _command = value;
                _syncParamsWithCommand();
              }),
            ),
            ..._paramFields(),
          ],
        ),
    ];
  }

  /// One form field per parameter in the selected command's JSON Schema --
  /// an enum becomes a dropdown, a boolean a switch, everything else a text
  /// field. Without this, a command's parameters -- `key`'s direction,
  /// `launch_app`'s target -- could be offered but never actually set.
  List<Widget> _paramFields() {
    final schema = _selectedCommand?.params;
    final properties = (schema?['properties'] as Map?)?.cast<String, dynamic>();
    if (properties == null || properties.isEmpty) return [];
    final required = ((schema?['required'] as List?) ?? []).cast<String>();

    return [
      for (final entry in properties.entries)
        Padding(
          padding: const EdgeInsets.only(top: 12),
          child: _paramField(
            entry.key,
            (entry.value as Map).cast<String, dynamic>(),
            required.contains(entry.key),
          ),
        ),
    ];
  }

  Widget _paramField(String name, Map<String, dynamic> prop, bool isRequired) {
    final title = (prop['title'] as String?) ?? name;
    final label = isRequired ? title : '$title (optional)';
    final description = prop['description'] as String?;
    final enumValues = (prop['enum'] as List?)?.cast<String>();

    if (enumValues != null) {
      return DropdownButtonFormField<String>(
        isExpanded: true,
        initialValue: _params[name] as String?,
        decoration: InputDecoration(labelText: label, helperText: description, border: const OutlineInputBorder()),
        items: [for (final value in enumValues) DropdownMenuItem(value: value, child: Text(value))],
        onChanged: (value) => setState(() => _params[name] = value),
      );
    }

    if (prop['type'] == 'boolean') {
      return SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: Text(label),
        subtitle: description == null ? null : Text(description),
        value: (_params[name] as bool?) ?? false,
        onChanged: (value) => setState(() => _params[name] = value),
      );
    }

    final isNumber = prop['type'] == 'number' || prop['type'] == 'integer';
    final controller = _paramControllers.putIfAbsent(
      name,
      () => TextEditingController(text: (_params[name] ?? '').toString()),
    );
    return TextField(
      controller: controller,
      keyboardType: isNumber ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
      decoration: InputDecoration(labelText: label, helperText: description, border: const OutlineInputBorder()),
      onChanged: (value) => setState(() {
        _params[name] = isNumber ? num.tryParse(value) : value;
      }),
    );
  }

  Widget _sceneField() => DropdownButtonFormField<String?>(
        isExpanded: true,
        initialValue: _sceneId,
        decoration: const InputDecoration(labelText: 'Scene', border: OutlineInputBorder()),
        items: [
          const DropdownMenuItem(value: null, child: Text('Stop the active scene (Off)')),
          for (final scene in widget.config.scenes)
            DropdownMenuItem(value: scene.id, child: Text(scene.name)),
        ],
        onChanged: (value) => setState(() => _sceneId = value),
      );

  /// Steps whatever the engine is currently focused on -- the last device a
  /// SmartHome key touched -- rather than naming one here. There is
  /// deliberately no device/target picker: which device that is changes
  /// from press to press, and a fallback for "nothing touched yet" is a
  /// hand-edited corner case rather than something this dialog needs to
  /// offer routinely.
  List<Widget> _adjustFields(BuildContext context) => [
        Text(
          'Steps whatever was touched last by a SmartHome key -- a light\'s '
          'brightness, a speaker\'s volume -- up or down.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
        const SizedBox(height: 12),
        SegmentedButton<String>(
          segments: const [
            ButtonSegment(value: 'up', label: Text('Up'), icon: Icon(Icons.add)),
            ButtonSegment(value: 'down', label: Text('Down'), icon: Icon(Icons.remove)),
          ],
          selected: {_direction},
          onSelectionChanged: (values) => setState(() => _direction = values.first),
        ),
      ];
}
