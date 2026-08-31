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
import 'value_editor.dart';

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

  /// Condition for an `if` or `wait_for` action. Seeded (once the type is
  /// actually chosen) rather than left null indefinitely, since every field
  /// below assumes it exists the moment `_type` says it should.
  HubCondition? _condition;

  /// An `if` action's two branches, edited on a pushed page -- see
  /// `_IfBranchesPage` -- rather than inline, since two nested reorderable
  /// lists do not fit inside this dialog's fixed width.
  List<HubAction> _then = [];
  List<HubAction> _otherwise = [];

  late TextEditingController _varName;
  HubValue? _setValue;

  late TextEditingController _timeout;
  late TextEditingController _poll;
  late String _onTimeout;

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

    // Copied, never the original action's own objects: this dialog is a
    // draft, and mutating `condition`/`then`/`otherwise` in place (which
    // `ConditionEditor` and `ActionListEditor` both do) must not reach the
    // scene's real config until "Save" is actually pressed.
    _condition = (initial?.type == 'if' || initial?.type == 'wait_for') ? initial?.condition?.copy() : null;
    _then = initial?.type == 'if' ? initial!.then.map((a) => a.copy()).toList() : <HubAction>[];
    _otherwise = initial?.type == 'if' ? initial!.otherwise.map((a) => a.copy()).toList() : <HubAction>[];

    _varName = TextEditingController(text: initial?.type == 'set' ? (initial?.varName ?? '') : '');
    _setValue = initial?.type == 'set' ? initial?.value?.copy() : null;

    _timeout = TextEditingController(
      text: ((initial?.type == 'wait_for' ? initial?.timeout : null) ?? 10.0).toString(),
    );
    _poll = TextEditingController(
      text: ((initial?.type == 'wait_for' ? initial?.poll : null) ?? 0.5).toString(),
    );
    _onTimeout = (initial?.type == 'wait_for' ? initial?.onTimeout : null) ?? 'continue';

    if (_type == 'device' && _deviceId != null) _loadCommands(_deviceId!);
  }

  @override
  void dispose() {
    _seconds.dispose();
    _varName.dispose();
    _timeout.dispose();
    _poll.dispose();
    _disposeParamControllers();
    super.dispose();
  }

  /// Switches the action type, seeding whatever the newly-chosen type needs
  /// that a blank draft would not otherwise have -- a condition for `if` and
  /// `wait_for`, a value for `set`. Left in place if it already exists, so
  /// switching away and back does not lose what was entered.
  void _onTypeChanged(String type) {
    setState(() {
      _type = type;
      if ((type == 'if' || type == 'wait_for')) {
        _condition ??= HubCondition(left: HubValue.literal(''));
      }
      if (type == 'set') {
        _setValue ??= HubValue.literal('');
      }
    });
    if (type == 'device' && _deviceId != null) _loadCommands(_deviceId!);
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
        'if' => conditionIsValid(_condition),
        'set' => _varName.text.trim().isNotEmpty && valueIsValid(_setValue),
        'wait_for' => conditionIsValid(_condition) &&
            double.tryParse(_timeout.text) != null &&
            double.tryParse(_poll.text) != null,
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
        'if' => HubAction.ifAction(_condition!, then: _then, otherwise: _otherwise),
        'set' => HubAction.set(_varName.text.trim(), _setValue!),
        'wait_for' => HubAction.waitFor(
            _condition!,
            timeout: double.parse(_timeout.text),
            poll: double.parse(_poll.text),
            onTimeout: _onTimeout,
          ),
        _ => HubAction.delay(double.parse(_seconds.text)),
      };

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(widget.initial == null ? 'Add action' : 'Edit action'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              // Two rows rather than one seven-way row: `SegmentedButton`
              // sizes to its natural width and does not wrap, so seven
              // segments together would overflow this dialog's fixed width.
              // Each row shows its own selection only when `_type` is one of
              // its own segments (`emptySelectionAllowed`) -- `SegmentedButton`
              // asserts if asked to select a value that is not among its own.
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'device', label: Text('Device'), icon: Icon(Icons.tv)),
                  ButtonSegment(value: 'scene', label: Text('Scene'), icon: Icon(Icons.movie_filter_outlined)),
                  ButtonSegment(value: 'adjust', label: Text('Adjust'), icon: Icon(Icons.tune)),
                  ButtonSegment(value: 'delay', label: Text('Wait'), icon: Icon(Icons.timer_outlined)),
                ],
                emptySelectionAllowed: true,
                selected: {'device', 'scene', 'adjust', 'delay'}.contains(_type) ? {_type} : <String>{},
                onSelectionChanged: (values) => _onTypeChanged(values.first),
              ),
              const SizedBox(height: 8),
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'if', label: Text('If'), icon: Icon(Icons.call_split)),
                  ButtonSegment(value: 'set', label: Text('Remember'), icon: Icon(Icons.bookmark_add_outlined)),
                  ButtonSegment(value: 'wait_for', label: Text('Wait for'), icon: Icon(Icons.hourglass_top)),
                ],
                emptySelectionAllowed: true,
                selected: {'if', 'set', 'wait_for'}.contains(_type) ? {_type} : <String>{},
                onSelectionChanged: (values) => _onTypeChanged(values.first),
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
              if (_type == 'if') ..._ifFields(context),
              if (_type == 'set') ..._setFields(),
              if (_type == 'wait_for') ..._waitForFields(),
            ],
          ),
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

  /// The condition, plus two rows that push a full page for editing the
  /// branches themselves -- two nested reorderable action lists do not fit
  /// inside this dialog's fixed width, the same reason the binding editor's
  /// per-button macros already live on their own page rather than in a
  /// dialog.
  List<Widget> _ifFields(BuildContext context) => [
        ConditionEditor(
          config: widget.config,
          api: widget.api,
          condition: _condition!,
          onChanged: (condition) => setState(() => _condition = condition),
        ),
        const SizedBox(height: 16),
        Card(
          margin: EdgeInsets.zero,
          child: Column(
            children: [
              ListTile(
                leading: const Icon(Icons.check_circle_outline),
                title: const Text('Then'),
                subtitle: Text(_then.isEmpty ? 'Nothing yet' : '${_then.length} step(s)'),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => _editBranches(context),
              ),
              const Divider(height: 1),
              ListTile(
                leading: const Icon(Icons.highlight_off),
                title: const Text('Otherwise'),
                subtitle: Text(_otherwise.isEmpty ? 'Nothing yet' : '${_otherwise.length} step(s)'),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => _editBranches(context),
              ),
            ],
          ),
        ),
      ];

  Future<void> _editBranches(BuildContext context) async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => _IfBranchesPage(
          config: widget.config,
          api: widget.api,
          then: _then,
          otherwise: _otherwise,
          onThenChanged: (actions) => setState(() => _then = actions),
          onOtherwiseChanged: (actions) => setState(() => _otherwise = actions),
        ),
      ),
    );
  }

  List<Widget> _setFields() => [
        TextField(
          controller: _varName,
          decoration: const InputDecoration(
            labelText: 'Variable name',
            helperText: 'Lowercase letters, numbers and underscores -- how a "Variable" value '
                'elsewhere finds this again.',
            border: OutlineInputBorder(),
          ),
          onChanged: (_) => setState(() {}),
        ),
        const SizedBox(height: 16),
        ValueEditor(
          config: widget.config,
          api: widget.api,
          value: _setValue!,
          label: 'Value to remember',
          onChanged: (value) => setState(() => _setValue = value),
        ),
      ];

  List<Widget> _waitForFields() => [
        ConditionEditor(
          config: widget.config,
          api: widget.api,
          condition: _condition!,
          onChanged: (condition) => setState(() => _condition = condition),
        ),
        const SizedBox(height: 16),
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: _timeout,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(labelText: 'Time out after (s)', border: OutlineInputBorder()),
                onChanged: (_) => setState(() {}),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(
                controller: _poll,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(labelText: 'Check every (s)', border: OutlineInputBorder()),
                onChanged: (_) => setState(() {}),
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        DropdownButtonFormField<String>(
          isExpanded: true,
          initialValue: _onTimeout,
          decoration: const InputDecoration(labelText: 'If it times out', border: OutlineInputBorder()),
          items: const [
            DropdownMenuItem(value: 'continue', child: Text('Continue with the rest of the macro')),
            DropdownMenuItem(value: 'stop', child: Text('Continue, but log it as a failure')),
          ],
          onChanged: (value) => setState(() => _onTimeout = value ?? 'continue'),
        ),
      ];
}

/// A full page for an `if` action's `then` and `otherwise` branches -- two
/// [ActionListEditor]s, the same "one card per macro" layout the scene
/// editor already uses for `on_start`/`on_stop`. Pushed from the action
/// dialog rather than shown inline: two nested `ReorderableListView`s do not
/// fit inside a fixed-width dialog, and this lets each branch be reordered
/// or have its own nested `if` added exactly the way a top-level macro can.
class _IfBranchesPage extends StatefulWidget {
  const _IfBranchesPage({
    required this.config,
    required this.api,
    required this.then,
    required this.otherwise,
    required this.onThenChanged,
    required this.onOtherwiseChanged,
  });

  final HubConfig config;
  final HubApi api;
  final List<HubAction> then;
  final List<HubAction> otherwise;
  final ValueChanged<List<HubAction>> onThenChanged;
  final ValueChanged<List<HubAction>> onOtherwiseChanged;

  @override
  State<_IfBranchesPage> createState() => _IfBranchesPageState();
}

class _IfBranchesPageState extends State<_IfBranchesPage> {
  late List<HubAction> _then;
  late List<HubAction> _otherwise;

  @override
  void initState() {
    super.initState();
    _then = widget.then;
    _otherwise = widget.otherwise;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('If / Otherwise')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: ActionListEditor(
                title: 'Then',
                actions: _then,
                config: widget.config,
                api: widget.api,
                emptyHint: 'Nothing yet -- runs no actions when the condition holds.',
                onChanged: (actions) {
                  setState(() => _then = actions);
                  widget.onThenChanged(actions);
                },
              ),
            ),
          ),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: ActionListEditor(
                title: 'Otherwise',
                actions: _otherwise,
                config: widget.config,
                api: widget.api,
                emptyHint: 'Nothing yet -- runs no actions when the condition does not hold.',
                onChanged: (actions) {
                  setState(() => _otherwise = actions);
                  widget.onOtherwiseChanged(actions);
                },
              ),
            ),
          ),
        ],
      ),
    );
  }
}
