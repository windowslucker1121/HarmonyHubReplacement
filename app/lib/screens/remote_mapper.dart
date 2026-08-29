/// Pointing physical remote buttons at one device's commands, on the picture
/// of the remote rather than through a list.
///
/// Nothing here knows what an Android TV is. It is handed a device's command
/// list and, optionally, that device's own idea of which button suits which
/// command; everything else -- the picture, the picker, the overwrite
/// warnings -- is the same for any backend that can say what it can do.
///
/// The rule the whole screen exists to enforce: only buttons picked here are
/// written. A scene's other bindings are left exactly as they were, which is
/// what makes this safe to run against a scene that already works.
library;

import 'package:flutter/material.dart';

import '../api/config.dart';
import '../api/models.dart';
import '../widgets/remote_diagram.dart';

/// One button's pending assignment: either a specific command, or "adjust"
/// -- follow whatever the remote is currently focused on, rather than a
/// command fixed to this device.
class _Pick {
  _Pick.command(this.command, {required this.repeat}) : direction = null;

  _Pick.adjust(this.direction, {this.repeat = true}) : command = null;

  final CommandInfo? command;

  /// "up" or "down" when this is an adjust pick.
  final String? direction;

  /// Whether holding the button should repeat the command.
  bool repeat;

  static const _adjustLabel = 'follows the last device touched';

  String get label {
    if (command != null) return command!.label;
    return direction == 'down' ? 'Turn down ($_adjustLabel)' : 'Turn up ($_adjustLabel)';
  }
}

class RemoteMapperPage extends StatefulWidget {
  const RemoteMapperPage({
    super.key,
    required this.buttons,
    required this.deviceId,
    required this.deviceName,
    required this.commands,
    required this.suggested,
    this.suggestedAdjust = const {},
    required this.existing,
    required this.targetName,
    this.preassignSuggested = false,
  });

  /// Every physical button, from `buttons.json`.
  final List<ButtonInfo> buttons;

  final String deviceId;
  final String deviceName;

  /// What the device can be asked to do.
  final List<CommandInfo> commands;

  /// The device's own suggestion, button key to command name. May be empty:
  /// a backend that offers none simply means every button starts blank.
  final Map<String, String> suggested;

  /// The device's suggestion for the SmartHome +/- keys: button key to
  /// `"up"`/`"down"`. Separate from [suggested] because the value is not a
  /// command -- an adjust binding steps whatever the engine is focused on
  /// at press time, not something fixed to this device.
  final Map<String, String> suggestedAdjust;

  /// What the target scene already binds. Used to warn before overwriting,
  /// never modified here.
  final Map<String, Binding> existing;

  /// The scene these assignments are headed for, for the header line.
  final String targetName;

  /// Start with every suggestion already picked. True for a new scene, where
  /// there is nothing to overwrite and accepting the lot is the common case.
  final bool preassignSuggested;

  @override
  State<RemoteMapperPage> createState() => _RemoteMapperPageState();
}

class _RemoteMapperPageState extends State<RemoteMapperPage> {
  final Map<String, _Pick> _picks = {};

  late final Map<String, CommandInfo> _byName = {for (final c in widget.commands) c.name: c};
  late final Map<String, ButtonInfo> _byKey = {for (final b in widget.buttons) b.key: b};

  @override
  void initState() {
    super.initState();
    if (widget.preassignSuggested) _fillFromSuggestions();
  }

  /// The command this device would pick for a button, if it has an opinion.
  CommandInfo? _commandSuggestionFor(String key) => _byName[widget.suggested[key]];

  void _fillFromSuggestions() {
    for (final entry in widget.suggested.entries) {
      final command = _byName[entry.value];
      // A suggestion naming a command the device no longer offers is stale,
      // not an error: skip it rather than binding something that would fail.
      if (command == null) continue;
      _picks[entry.key] = _Pick.command(command, repeat: command.repeatable);
    }
    for (final entry in widget.suggestedAdjust.entries) {
      _picks[entry.key] = _Pick.adjust(entry.value);
    }
  }

  int get _overwrites => _picks.keys.where(widget.existing.containsKey).length;

  RemoteKeyStatus _status(String key) {
    final pick = _picks[key];
    final existing = widget.existing[key];

    if (pick != null) {
      return RemoteKeyStatus(
        caption: existing == null ? pick.label : '${pick.label}  (replaces ${existing.summary})',
        highlighted: true,
        marked: existing != null,
      );
    }
    return RemoteKeyStatus(
      caption: existing == null ? null : 'keeps ${existing.summary}',
      marked: existing != null,
    );
  }

  Future<void> _assign(String key) async {
    final button = _byKey[key];
    if (button == null) return;

    final current = _picks[key];
    final result = await showDialog<_PickerResult>(
      context: context,
      builder: (_) => _CommandPicker(
        buttonLabel: button.label,
        deviceName: widget.deviceName,
        commands: widget.commands,
        // Tapping "Left Arrow" lands on the device's own choice for it --
        // usually right, and one tap away from anything else.
        selected: current?.command ?? _commandSuggestionFor(key),
        suggested: _commandSuggestionFor(key),
        suggestedAdjustDirection: widget.suggestedAdjust[key],
        adjustSelected: current?.direction != null,
        repeat: current?.repeat,
        replaces: widget.existing[key]?.summary,
        canClear: _picks.containsKey(key),
      ),
    );
    if (result == null) return;

    setState(() {
      if (result.cleared) {
        _picks.remove(key);
      } else if (result.direction != null) {
        _picks[key] = _Pick.adjust(result.direction!, repeat: result.repeat);
      } else if (result.command != null) {
        _picks[key] = _Pick.command(result.command!, repeat: result.repeat);
      }
    });
  }

  /// Only the picked buttons, as bindings. Everything else is left alone.
  Map<String, Binding> _result() => {
        for (final entry in _picks.entries) entry.key: _bindingFor(entry.value),
      };

  Binding _bindingFor(_Pick pick) {
    HubAction buildAction() => pick.command != null
        ? HubAction.device(widget.deviceId, pick.command!.name)
        : HubAction.adjust(pick.direction!);
    return Binding(onPress: [buildAction()], onRepeat: pick.repeat ? [buildAction()] : []);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    // On a phone the two secondary actions become icons: the Assign button
    // is the one that must never be squeezed out.
    final compact = MediaQuery.sizeOf(context).width < 620;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Map the remote'),
        actions: [
          if (widget.suggested.isNotEmpty || widget.suggestedAdjust.isNotEmpty)
            compact
                ? IconButton(
                    onPressed: () => setState(_fillFromSuggestions),
                    icon: const Icon(Icons.auto_fix_high),
                    tooltip: 'Fill from suggestions',
                  )
                : TextButton.icon(
                    onPressed: () => setState(_fillFromSuggestions),
                    icon: const Icon(Icons.auto_fix_high),
                    label: const Text('Fill from suggestions'),
                  ),
          if (_picks.isNotEmpty)
            compact
                ? IconButton(
                    onPressed: () => setState(_picks.clear),
                    icon: const Icon(Icons.backspace_outlined),
                    tooltip: 'Clear all picks',
                  )
                : TextButton(
                    onPressed: () => setState(_picks.clear),
                    child: const Text('Clear'),
                  ),
          const SizedBox(width: 8),
          FilledButton(
            onPressed: _picks.isEmpty ? null : () => Navigator.pop(context, _result()),
            child: Text('Assign ${_picks.length}'),
          ),
          const SizedBox(width: 12),
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
                  'Tap a button to point it at ${widget.deviceName}. '
                  'Only the buttons you assign are written to "${widget.targetName}".',
                  style: theme.textTheme.bodyMedium,
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 16,
                  runSpacing: 4,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    RemoteLegendDot(color: scheme.primary, label: '${_picks.length} assigned'),
                    RemoteLegendDot(
                      color: scheme.tertiary,
                      label: widget.existing.isEmpty
                          ? 'nothing bound here yet'
                          : '${widget.existing.length} already bound in this scene',
                    ),
                    if (_overwrites > 0)
                      Text(
                        '$_overwrites will be replaced',
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: scheme.tertiary,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                  ],
                ),
              ],
            ),
          ),
          Expanded(
            child: RemoteBoard(
              buttons: widget.buttons,
              status: _status,
              onTap: _assign,
              hint: 'Tap a button to choose what it should do',
              emptyCaption: 'not assigned',
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------

/// What the picker came back with: a specific command, the adjust option, or
/// [cleared] for "unassign". The dialog returning null at all (rather than a
/// `_PickerResult`) means the user changed their mind.
class _PickerResult {
  const _PickerResult.command(this.command, this.repeat)
      : direction = null,
        cleared = false;

  const _PickerResult.adjust(this.direction, this.repeat)
      : command = null,
        cleared = false;

  const _PickerResult.cleared()
      : command = null,
        direction = null,
        repeat = false,
        cleared = true;

  final CommandInfo? command;
  final String? direction;
  final bool repeat;
  final bool cleared;
}

/// Choosing one command for one button -- or, when this device suggested it,
/// the adjust option that steps whatever the remote is currently focused on.
///
/// A searchable list rather than a dropdown: a device with fifty commands is
/// normal, and scrolling a menu that long to find "fast forward" is not.
class _CommandPicker extends StatefulWidget {
  const _CommandPicker({
    required this.buttonLabel,
    required this.deviceName,
    required this.commands,
    required this.selected,
    required this.suggested,
    required this.repeat,
    required this.replaces,
    required this.canClear,
    this.suggestedAdjustDirection,
    this.adjustSelected = false,
  });

  final String buttonLabel;
  final String deviceName;
  final List<CommandInfo> commands;
  final CommandInfo? selected;
  final CommandInfo? suggested;
  final bool? repeat;
  final String? replaces;
  final bool canClear;

  /// "up"/"down" when this device suggests this button follow the engine's
  /// focus rather than a fixed command -- the SmartHome +/- keys' case. Only
  /// then does the adjust option appear at all.
  final String? suggestedAdjustDirection;

  /// Whether the pick already open when this dialog was launched was the
  /// adjust option, rather than a command.
  final bool adjustSelected;

  @override
  State<_CommandPicker> createState() => _CommandPickerState();
}

class _CommandPickerState extends State<_CommandPicker> {
  late CommandInfo? _selected = widget.selected;
  late bool _adjust = widget.adjustSelected;
  late bool _repeat =
      widget.repeat ?? (widget.adjustSelected ? true : widget.selected?.repeatable ?? false);
  String _filter = '';

  bool get _hasSelection => _selected != null || _adjust;

  /// Whether the current pick is safe to fire on every repeat tick -- an
  /// adjust action always is, a command only if it says so itself. Power and
  /// select must never repeat: on Android TV, repeating "select" is what
  /// turns a held OK button into ten taps a second instead of a long press.
  bool get _canRepeat => _adjust || (_selected?.repeatable ?? false);

  /// [_repeat] as it will actually be saved -- clamped here rather than only
  /// where it is displayed, so a stale `true` left over from switching away
  /// from a repeatable command can never reach a command that disallows it.
  bool get _effectiveRepeat => _repeat && _canRepeat;

  List<CommandInfo> get _visible {
    final needle = _filter.trim().toLowerCase();
    if (needle.isEmpty) return widget.commands;
    return widget.commands
        .where((c) => c.label.toLowerCase().contains(needle) || c.name.toLowerCase().contains(needle))
        .toList();
  }

  void _select(CommandInfo command) {
    setState(() {
      _selected = command;
      _adjust = false;
      // Following the command's own answer, until the user overrides it.
      if (widget.repeat == null || command != widget.selected) _repeat = command.repeatable;
    });
  }

  void _selectAdjust() {
    setState(() {
      _selected = null;
      _adjust = true;
      _repeat = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final visible = _visible;
    // A dialog does not shrink its child, so the fixed size a long command
    // list wants has to be clamped to what the screen actually has.
    final screen = MediaQuery.sizeOf(context);

    return AlertDialog(
      title: Text(widget.buttonLabel),
      content: SizedBox(
        width: screen.width < 460 ? screen.width - 100 : 420,
        height: screen.height < 620 ? screen.height * 0.6 : 480,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (widget.replaces != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  'Already bound in this scene: ${widget.replaces}',
                  style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.tertiary),
                ),
              ),
            TextField(
              autofocus: true,
              decoration: InputDecoration(
                prefixIcon: const Icon(Icons.search),
                hintText: 'Search ${widget.deviceName}',
                border: const OutlineInputBorder(),
                isDense: true,
              ),
              onChanged: (value) => setState(() => _filter = value),
            ),
            const SizedBox(height: 8),
            if (widget.suggestedAdjustDirection != null)
              ListTile(
                dense: true,
                selected: _adjust,
                leading: Icon(
                  _adjust ? Icons.radio_button_checked : Icons.radio_button_unchecked,
                  color: _adjust ? theme.colorScheme.primary : null,
                ),
                title: Text(
                  widget.suggestedAdjustDirection == 'down'
                      ? 'Turn down (follows the last device touched)'
                      : 'Turn up (follows the last device touched)',
                ),
                subtitle: Text('suggested for this button', style: TextStyle(color: theme.colorScheme.primary)),
                onTap: _selectAdjust,
              ),
            Expanded(
              child: visible.isEmpty
                  ? Center(
                      child: Text('No command matches', style: theme.textTheme.bodySmall),
                    )
                  : ListView.builder(
                      itemCount: visible.length,
                      itemBuilder: (context, index) {
                        final command = visible[index];
                        final chosen = !_adjust && command.name == _selected?.name;
                        return ListTile(
                          dense: true,
                          selected: chosen,
                          leading: Icon(
                            chosen ? Icons.radio_button_checked : Icons.radio_button_unchecked,
                            color: chosen ? theme.colorScheme.primary : null,
                          ),
                          title: Text(command.label),
                          subtitle: command.name == widget.suggested?.name
                              ? Text('suggested for this button',
                                  style: TextStyle(color: theme.colorScheme.primary))
                              : (command.description.isEmpty ? null : Text(command.description)),
                          onTap: () => _select(command),
                        );
                      },
                    ),
            ),
            if (_hasSelection)
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                title: const Text('Repeat while held'),
                subtitle: _canRepeat
                    ? null
                    : Text(
                        "${_selected?.label ?? 'This'} isn't safe to repeat -- it would fire "
                        'over and over while the button is down.',
                        style: theme.textTheme.bodySmall,
                      ),
                value: _effectiveRepeat,
                onChanged: _canRepeat ? (value) => setState(() => _repeat = value) : null,
              ),
          ],
        ),
      ),
      actions: [
        if (widget.canClear)
          TextButton(
            onPressed: () => Navigator.pop(context, const _PickerResult.cleared()),
            child: const Text('Unassign'),
          ),
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: !_hasSelection
              ? null
              : () => Navigator.pop(
                    context,
                    _adjust
                        ? _PickerResult.adjust(widget.suggestedAdjustDirection!, _effectiveRepeat)
                        : _PickerResult.command(_selected!, _effectiveRepeat),
                  ),
          child: const Text('Assign'),
        ),
      ],
    );
  }
}
