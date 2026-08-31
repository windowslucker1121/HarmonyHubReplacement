/// Editing a [HubValue] and a [HubCondition] built from two of them.
///
/// Three kinds of value cover everywhere a scene needs one: a fixed literal,
/// a device's live state, or something a `set` action remembered earlier in
/// the macro. Both sides of a condition, and a `set` action's own source,
/// are all edited with the same [ValueEditor] -- there is exactly one way to
/// build a value anywhere in the app, rather than a condition's editor and a
/// restore-parameter's editor slowly drifting apart.
library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/config.dart';
import '../api/models.dart';

class ValueEditor extends StatefulWidget {
  const ValueEditor({
    super.key,
    required this.config,
    required this.api,
    required this.value,
    required this.onChanged,
    this.label,
    this.sceneLiteralPicker = false,
  });

  final HubConfig config;
  final HubApi api;
  final HubValue value;
  final ValueChanged<HubValue> onChanged;
  final String? label;

  /// When true, a 'literal' value is picked from the configured scenes
  /// (plus "no scene -- idle" for `""`) rather than typed as free text.
  /// [ConditionEditor] sets this on whichever side is *not* the
  /// [HubValue.transition] one, since a literal being compared against a
  /// scene switch means a scene id, not arbitrary text -- the same "never
  /// type an id" principle the device/command dropdowns already follow.
  final bool sceneLiteralPicker;

  @override
  State<ValueEditor> createState() => _ValueEditorState();
}

class _ValueEditorState extends State<ValueEditor> {
  late String _kind; // 'literal' | 'state' | 'var' | 'transition'
  late final TextEditingController _literalController;
  late final TextEditingController _varController;
  late String _edge; // 'from' | 'to', for the 'transition' kind

  List<BackendInfo> _backends = [];
  bool _loadingBackends = false;

  String? _deviceId;
  String? _target;
  List<StateTargetInfo> _targets = [];
  bool _loadingTargets = false;
  String? _targetsError;

  String? _currentValue;
  bool _loadingCurrent = false;

  List<VariableInfo> _knownVariables = [];

  @override
  void initState() {
    super.initState();
    _kind = widget.value.type;
    _literalController = TextEditingController(text: widget.value.value ?? '');
    _varController = TextEditingController(text: widget.value.name ?? '');
    _deviceId = widget.value.device;
    _target = widget.value.target;
    _edge = widget.value.edge ?? 'from';
    _loadBackends();
    if (_kind == 'state' && _deviceId != null) _loadTargets(_deviceId!);
    if (_kind == 'var') _loadVariables();
  }

  @override
  void dispose() {
    _literalController.dispose();
    _varController.dispose();
    super.dispose();
  }

  /// Devices whose backend can answer a condition -- the same `readable`
  /// flag `/api/backends` reports for the same reason `pairable` is on
  /// there: the app should not keep its own list of which backend names
  /// happen to support it.
  List<DeviceConfig> get _readableDevices {
    final readableBackends = _backends.where((b) => b.readable).map((b) => b.name).toSet();
    return widget.config.devices.where((d) => readableBackends.contains(d.backend)).toList();
  }

  Future<void> _loadBackends() async {
    setState(() => _loadingBackends = true);
    try {
      final backends = await widget.api.backends();
      if (!mounted) return;
      setState(() => _backends = backends);
    } catch (_) {
      // A failed lookup just leaves the device list empty -- this is a
      // secondary fetch inside an already-open dialog, not worth its own
      // error banner.
    } finally {
      if (mounted) setState(() => _loadingBackends = false);
    }
  }

  Future<void> _loadTargets(String deviceId) async {
    setState(() {
      _loadingTargets = true;
      _targetsError = null;
      _targets = [];
      _currentValue = null;
    });
    try {
      final targets = await widget.api.deviceReadable(deviceId);
      if (!mounted) return;
      setState(() {
        _targets = targets;
        if (_target == null || !targets.any((t) => t.target == _target)) {
          _target = targets.firstOrNull?.target;
        }
      });
      // The device picker's own `onChanged` already emitted once, with
      // `_target` still empty -- targets load asynchronously, so there was
      // nothing else it could have sent yet. This is what actually tells
      // the parent which target got auto-selected once the answer is in;
      // without it, a device with exactly one target -- picking it is never
      // a separate tap -- would save with an empty target forever.
      _emit();
      if (_target != null) _loadCurrentValue(deviceId, _target!);
    } catch (err) {
      if (!mounted) return;
      setState(() => _targetsError = 'Could not load: $err');
    } finally {
      if (mounted) setState(() => _loadingTargets = false);
    }
  }

  Future<void> _loadCurrentValue(String deviceId, String target) async {
    setState(() => _loadingCurrent = true);
    try {
      final value = await widget.api.deviceState(deviceId, target);
      if (!mounted) return;
      setState(() => _currentValue = value);
    } catch (_) {
      if (!mounted) return;
      setState(() => _currentValue = null);
    } finally {
      if (mounted) setState(() => _loadingCurrent = false);
    }
  }

  Future<void> _loadVariables() async {
    try {
      final variables = await widget.api.variables();
      if (!mounted) return;
      setState(() => _knownVariables = variables);
    } catch (_) {
      // Best-effort suggestions only; a variable can still be typed by hand.
    }
  }

  void _emit() {
    switch (_kind) {
      case 'state':
        widget.onChanged(HubValue.state(_deviceId ?? '', _target ?? ''));
      case 'var':
        widget.onChanged(HubValue.variable(_varController.text.trim()));
      case 'transition':
        widget.onChanged(HubValue.transition(_edge));
      default:
        widget.onChanged(HubValue.literal(_literalController.text));
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (widget.label != null) ...[
          Text(widget.label!, style: Theme.of(context).textTheme.labelMedium),
          const SizedBox(height: 4),
        ],
        SegmentedButton<String>(
          segments: const [
            ButtonSegment(value: 'literal', label: Text('Fixed'), icon: Icon(Icons.edit_outlined)),
            ButtonSegment(value: 'state', label: Text('Device'), icon: Icon(Icons.sensors_outlined)),
            ButtonSegment(value: 'var', label: Text('Variable'), icon: Icon(Icons.bookmark_outline)),
            ButtonSegment(value: 'transition', label: Text('Scene change'), icon: Icon(Icons.swap_horiz)),
          ],
          selected: {_kind},
          onSelectionChanged: (values) {
            setState(() => _kind = values.first);
            if (_kind == 'state' && _deviceId != null) _loadTargets(_deviceId!);
            if (_kind == 'var') _loadVariables();
            _emit();
          },
        ),
        const SizedBox(height: 12),
        if (_kind == 'literal') _literalField(),
        if (_kind == 'state') ..._stateFields(scheme),
        if (_kind == 'var') ..._varFields(),
        if (_kind == 'transition') _transitionField(),
      ],
    );
  }

  Widget _literalField() {
    if (!widget.sceneLiteralPicker) {
      return TextField(
        controller: _literalController,
        decoration: const InputDecoration(labelText: 'Value', border: OutlineInputBorder()),
        onChanged: (_) => _emit(),
      );
    }
    // A literal compared against a scene switch means a scene id, never
    // arbitrary text -- picked the same way a command or a device is,
    // rather than typed and risking a typo that silently never matches.
    final scenes = widget.config.scenes;
    final validValues = {'', ...scenes.map((s) => s.id)};
    final current = _literalController.text;
    return DropdownButtonFormField<String>(
      isExpanded: true,
      initialValue: validValues.contains(current) ? current : null,
      decoration: const InputDecoration(labelText: 'Scene', border: OutlineInputBorder()),
      items: [
        const DropdownMenuItem(value: '', child: Text('(no scene -- idle)')),
        for (final scene in scenes) DropdownMenuItem(value: scene.id, child: Text(scene.name)),
      ],
      onChanged: (value) => setState(() {
        _literalController.text = value ?? '';
        _emit();
      }),
    );
  }

  Widget _transitionField() => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'from', label: Text('The scene being left'), icon: Icon(Icons.logout)),
              ButtonSegment(value: 'to', label: Text('The scene being entered'), icon: Icon(Icons.login)),
            ],
            selected: {_edge},
            onSelectionChanged: (values) {
              setState(() => _edge = values.first);
              _emit();
            },
          ),
          const SizedBox(height: 6),
          Text(
            'Only meaningful inside "When the scene starts" / "When the scene stops" -- '
            'resolves to no scene everywhere else, including a plain button.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      );

  List<Widget> _stateFields(ColorScheme scheme) {
    if (_loadingBackends) {
      return const [Padding(padding: EdgeInsets.symmetric(vertical: 8), child: LinearProgressIndicator())];
    }
    final devices = _readableDevices;
    if (devices.isEmpty) {
      return const [Text('No device here can report its state yet.')];
    }
    return [
      DropdownButtonFormField<String>(
        isExpanded: true,
        initialValue: _deviceId,
        decoration: const InputDecoration(labelText: 'Device', border: OutlineInputBorder()),
        items: [for (final d in devices) DropdownMenuItem(value: d.id, child: Text(d.name))],
        onChanged: (value) {
          setState(() {
            _deviceId = value;
            _target = null;
          });
          if (value != null) _loadTargets(value);
          _emit();
        },
      ),
      const SizedBox(height: 12),
      if (_loadingTargets)
        const Padding(padding: EdgeInsets.symmetric(vertical: 8), child: LinearProgressIndicator())
      else if (_targetsError != null)
        Text(_targetsError!, style: TextStyle(color: scheme.error))
      else if (_targets.isEmpty)
        const Text('This device has nothing to check.')
      else ...[
        DropdownButtonFormField<String>(
          isExpanded: true,
          initialValue: _target,
          decoration: const InputDecoration(labelText: 'State', border: OutlineInputBorder()),
          items: [for (final t in _targets) DropdownMenuItem(value: t.target, child: Text(t.label))],
          onChanged: (value) {
            setState(() => _target = value);
            if (value != null && _deviceId != null) _loadCurrentValue(_deviceId!, value);
            _emit();
          },
        ),
        const SizedBox(height: 6),
        Row(
          children: [
            Icon(Icons.info_outline, size: 14, color: scheme.outline),
            const SizedBox(width: 4),
            Expanded(
              child: Text(
                _loadingCurrent
                    ? 'Reading…'
                    : (_currentValue == null ? 'Currently: unknown' : 'Currently: $_currentValue'),
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
            IconButton(
              icon: const Icon(Icons.refresh, size: 16),
              tooltip: 'Read again',
              onPressed: (_deviceId != null && _target != null)
                  ? () => _loadCurrentValue(_deviceId!, _target!)
                  : null,
            ),
          ],
        ),
      ],
    ];
  }

  List<Widget> _varFields() => [
        TextField(
          controller: _varController,
          decoration: const InputDecoration(
            labelText: 'Variable name',
            helperText: 'Whatever a "Remember a value" action earlier in the macro stored under this name.',
            border: OutlineInputBorder(),
          ),
          onChanged: (_) => _emit(),
        ),
        if (_knownVariables.isNotEmpty) ...[
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final v in _knownVariables)
                ActionChip(
                  label: Text('\$${v.name}  (${v.value})'),
                  onPressed: () {
                    setState(() => _varController.text = v.name);
                    _emit();
                  },
                ),
            ],
          ),
        ],
      ];
}

/// Edits one [HubCondition]: [ValueEditor] either side of an operator, plus
/// what to do when the left side (or the right) cannot be read.
class ConditionEditor extends StatefulWidget {
  const ConditionEditor({
    super.key,
    required this.config,
    required this.api,
    required this.condition,
    required this.onChanged,
  });

  final HubConfig config;
  final HubApi api;

  /// Mutated in place as the user edits, the same way the action editor's
  /// own param map is -- [onChanged] fires afterwards purely to trigger the
  /// parent's rebuild, not to hand back a different object.
  final HubCondition condition;
  final ValueChanged<HubCondition> onChanged;

  @override
  State<ConditionEditor> createState() => _ConditionEditorState();
}

class _ConditionEditorState extends State<ConditionEditor> {
  static const _ops = [
    ('is', 'is'),
    ('is_not', 'is not'),
    ('contains', 'contains'),
    ('in', 'is part of'),
    ('gt', 'is greater than'),
    ('lt', 'is less than'),
    ('known', 'can be read'),
    ('unknown', 'cannot be read'),
  ];

  /// `known`/`unknown` ask whether `left` could be read at all -- a
  /// question a `transition` value always answers the same way (see
  /// `HubValue.transition`'s docstring), so offering them there is a
  /// control that can never do anything.
  static const _opsWithoutTransitionLeft = {'known', 'unknown'};

  @override
  Widget build(BuildContext context) {
    final condition = widget.condition;
    final needsRight = condition.needsRight;
    final leftIsTransition = condition.left.type == 'transition';
    final rightIsTransition = condition.right?.type == 'transition';
    final ops = leftIsTransition
        ? _ops.where((op) => !_opsWithoutTransitionLeft.contains(op.$1)).toList()
        : _ops;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ValueEditor(
          key: const ValueKey('condition-left'),
          config: widget.config,
          api: widget.api,
          value: condition.left,
          label: 'Check',
          sceneLiteralPicker: rightIsTransition,
          onChanged: (value) => setState(() {
            condition.left = value;
            // `known`/`unknown` stop making sense the moment the left side
            // becomes a transition -- reset rather than leave the saved
            // condition on an operator the dropdown below no longer even
            // offers, which the display-only fallback there cannot fix on
            // its own.
            if (value.type == 'transition' && _opsWithoutTransitionLeft.contains(condition.op)) {
              condition.op = 'is';
              condition.right ??= HubValue.literal('');
            }
            widget.onChanged(condition);
          }),
        ),
        const SizedBox(height: 12),
        DropdownButtonFormField<String>(
          isExpanded: true,
          initialValue: ops.any((op) => op.$1 == condition.op) ? condition.op : ops.first.$1,
          decoration: const InputDecoration(labelText: 'Compared how', border: OutlineInputBorder()),
          items: [for (final (value, label) in ops) DropdownMenuItem(value: value, child: Text(label))],
          onChanged: (value) {
            if (value == null) return;
            setState(() {
              condition.op = value;
              if (condition.needsRight) condition.right ??= HubValue.literal('');
              widget.onChanged(condition);
            });
          },
        ),
        if (needsRight) ...[
          const SizedBox(height: 12),
          ValueEditor(
            key: const ValueKey('condition-right'),
            config: widget.config,
            api: widget.api,
            value: condition.right ?? HubValue.literal(''),
            label: 'Against',
            sceneLiteralPicker: leftIsTransition,
            onChanged: (value) => setState(() {
              condition.right = value;
              widget.onChanged(condition);
            }),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            isExpanded: true,
            initialValue: condition.onUnreadable,
            decoration: const InputDecoration(labelText: 'If it cannot be read', border: OutlineInputBorder()),
            items: const [
              DropdownMenuItem(value: 'run', child: Text('Act as if this should run')),
              DropdownMenuItem(value: 'skip', child: Text('Act as if it should not')),
            ],
            onChanged: (value) {
              if (value == null) return;
              setState(() {
                condition.onUnreadable = value;
                widget.onChanged(condition);
              });
            },
          ),
        ],
      ],
    );
  }
}

/// Whether [value] is complete enough to save -- a 'state' needs a device
/// and target, a 'var' needs a name; a 'literal' is always valid, even empty.
bool valueIsValid(HubValue? value) {
  if (value == null) return false;
  return switch (value.type) {
    'state' => (value.device ?? '').isNotEmpty && (value.target ?? '').isNotEmpty,
    'var' => (value.name ?? '').trim().isNotEmpty,
    _ => true,
  };
}

/// Whether [condition] is complete enough to save.
bool conditionIsValid(HubCondition? condition) {
  if (condition == null) return false;
  if (!valueIsValid(condition.left)) return false;
  if (condition.needsRight && !valueIsValid(condition.right)) return false;
  return true;
}
