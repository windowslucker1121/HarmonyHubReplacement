/// Teaching an infrared device its own commands, by capturing them off the
/// original remote.
///
/// Distinct from [openLearnPage] (`learn_screen.dart`), which names *RF*
/// signatures the physical Harmony remote has already been seen to send.
/// This instead drives the hub's own IR receiver to capture a command from
/// a *different* remote -- the one that came with the TV, the soundbar, the
/// projector -- and sends it back out again once it is saved.
///
/// The name picked for a command matters beyond labelling it: naming a
/// command after a real button key (`volume_up`, `channel_down`, ...) is
/// what lets `IrBackend.suggested_bindings()` map it straight onto the
/// remote's own button the moment it is saved, so "Map the remote to this
/// device" on the device screen is pre-filled without a separate step. The
/// dropdown below exists to make that the easy choice rather than something
/// that has to be known in advance.
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/models.dart';
import '../state/hub_store.dart';
import '../widgets/selectable_route.dart';
import 'scenes_screen.dart' show slugify;

Future<void> openIrLearnPage(
  BuildContext context, {
  required HubStore store,
  required String deviceId,
  required String deviceName,
  required BackendInfo backend,
}) =>
    pushSelectable<void>(
      context,
      IrLearnPage(
        store: store,
        deviceId: deviceId,
        deviceName: deviceName,
        backend: backend,
      ),
    );

/// Button-key prefixes that usually want to keep firing while the button is
/// held -- volume and channel step, a D-pad direction -- rather than once
/// per press. Only a starting point: the switch on the save step can always
/// override it for the one command that is the exception.
const _repeatablePrefixes = ['volume_', 'channel_', 'up_arrow', 'down_arrow', 'left_arrow', 'right_arrow'];

bool _defaultRepeatable(String name) => _repeatablePrefixes.any(name.startsWith);

class IrLearnPage extends StatefulWidget {
  const IrLearnPage({
    super.key,
    required this.store,
    required this.deviceId,
    required this.deviceName,
    required this.backend,
  });

  final HubStore store;
  final String deviceId;
  final String deviceName;
  final BackendInfo backend;

  @override
  State<IrLearnPage> createState() => _IrLearnPageState();
}

class _IrLearnPageState extends State<IrLearnPage> {
  List<CommandInfo> _commands = [];
  bool _loadingCommands = true;

  final _labelController = TextEditingController();
  String? _selectedButtonKey; // null means "custom name", derived from the label instead

  LearnStatus? _status;
  Timer? _poll;
  bool _busy = false;
  bool _repeatable = false;

  @override
  void initState() {
    super.initState();
    _loadCommands();
  }

  @override
  void dispose() {
    _poll?.cancel();
    // Courtesy only -- a fire-and-forget best effort to free the receiver
    // for another device rather than leaving it held until the job's own
    // timeout elapses. Nothing here can await it, and nothing needs to.
    if (_status?.isBusy ?? false) {
      widget.store.api.cancelLearn(widget.deviceId);
    }
    _labelController.dispose();
    super.dispose();
  }

  Future<void> _loadCommands() async {
    try {
      final commands = await widget.store.api.deviceCommands(widget.deviceId);
      if (mounted) setState(() => _commands = commands);
    } catch (_) {
      // Leave whatever was already showing; a transient failure here is not
      // worth surfacing over the learn flow itself.
    } finally {
      if (mounted) setState(() => _loadingCommands = false);
    }
  }

  void _snack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  // ------------------------------------------------------------------

  String get _name {
    final key = _selectedButtonKey;
    if (key != null) return key;
    return slugify(_labelController.text);
  }

  bool get _canStart => _labelController.text.trim().isNotEmpty && _name.isNotEmpty;

  Future<void> _start() async {
    setState(() => _busy = true);
    try {
      final status = await widget.store.api.startLearn(widget.deviceId);
      if (!mounted) return;
      setState(() {
        _status = status;
        _repeatable = _defaultRepeatable(_name);
      });
      _pollStatus();
    } catch (err) {
      _snack('$err');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _pollStatus() {
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 1), (timer) async {
      late final LearnStatus status;
      try {
        status = await widget.store.api.learnStatus(widget.deviceId);
      } catch (_) {
        timer.cancel();
        return;
      }
      if (!mounted) {
        timer.cancel();
        return;
      }
      setState(() => _status = status);
      if (!status.isBusy) timer.cancel();
    });
  }

  Future<void> _cancel() async {
    _poll?.cancel();
    try {
      final status = await widget.store.api.cancelLearn(widget.deviceId);
      if (mounted) setState(() => _status = status);
    } catch (_) {
      // The job finished on its own between the tap and the request.
    }
  }

  Future<void> _test() async {
    setState(() => _busy = true);
    final result = await widget.store.api.verifyLearn(widget.deviceId);
    if (mounted) setState(() => _busy = false);
    _snack(result.ok ? 'Sent' : result.detail);
  }

  Future<void> _save() async {
    final name = _name;
    final label = _labelController.text.trim();
    if (name.isEmpty || label.isEmpty) return;

    setState(() => _busy = true);
    try {
      final commands = await widget.store.api.saveLearned(
        widget.deviceId,
        name: name,
        label: label,
        repeatable: _repeatable,
      );
      if (!mounted) return;
      setState(() {
        _commands = commands;
        _status = null;
        _selectedButtonKey = null;
        _labelController.clear();
      });
      _snack('Saved "$label" -- ready for the next one');
    } catch (err) {
      _snack('$err');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _testSaved(String command) async {
    final result = await widget.store.api.testCommand(widget.deviceId, command);
    _snack(result.ok ? 'Sent $command' : result.detail);
  }

  Future<void> _forget(String name) async {
    setState(() => _busy = true);
    try {
      final commands = await widget.store.api.forgetLearned(widget.deviceId, name);
      if (mounted) setState(() => _commands = commands);
    } catch (err) {
      _snack('$err');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ------------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final buttons = widget.store.buttons;
    final status = _status;

    return Scaffold(
      appBar: AppBar(title: Text('Learn commands -- ${widget.deviceName}')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_busy) ...[const LinearProgressIndicator(), const SizedBox(height: 16)],
          if (widget.backend.learnHint.isNotEmpty) ...[
            Text(widget.backend.learnHint, style: Theme.of(context).textTheme.bodySmall),
            const SizedBox(height: 16),
          ],
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('New command', style: Theme.of(context).textTheme.titleSmall),
                  const SizedBox(height: 12),
                  if (status == null || status.state == 'idle') ..._nameEntry(buttons),
                  if (status != null && status.state != 'idle') _statusPanel(status),
                ],
              ),
            ),
          ),
          const SizedBox(height: 24),
          Text('Already learned', style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 8),
          if (_loadingCommands)
            const Center(child: Padding(padding: EdgeInsets.all(16), child: CircularProgressIndicator()))
          else if (_commands.isEmpty)
            const Text('Nothing learned yet.')
          else
            for (final command in _commands)
              Card(
                child: ListTile(
                  title: Text(command.label),
                  subtitle: Text(
                    command.description.isEmpty
                        ? command.name
                        : '${command.name} · ${command.description}',
                  ),
                  trailing: Wrap(
                    spacing: 4,
                    children: [
                      IconButton(
                        icon: const Icon(Icons.play_arrow),
                        tooltip: 'Send',
                        onPressed: widget.store.hubRunning ? () => _testSaved(command.name) : null,
                      ),
                      IconButton(
                        icon: const Icon(Icons.delete_outline),
                        tooltip: 'Forget',
                        onPressed: () => _forget(command.name),
                      ),
                    ],
                  ),
                ),
              ),
        ],
      ),
    );
  }

  List<Widget> _nameEntry(List<ButtonInfo> buttons) {
    return [
      DropdownButtonFormField<String?>(
        isExpanded: true,
        initialValue: _selectedButtonKey,
        decoration: const InputDecoration(
          labelText: 'Bind to a remote button',
          helperText: 'Picking one is what lets "Map the remote" pre-fill this command later.',
          border: OutlineInputBorder(),
        ),
        items: [
          const DropdownMenuItem(value: null, child: Text('Custom name (not a standard button)')),
          for (final button in buttons)
            DropdownMenuItem(value: button.key, child: Text(button.label)),
        ],
        onChanged: (value) => setState(() {
          _selectedButtonKey = value;
          final button = buttons.where((b) => b.key == value).firstOrNull;
          if (button != null) _labelController.text = button.label;
        }),
      ),
      const SizedBox(height: 12),
      TextField(
        controller: _labelController,
        decoration: const InputDecoration(
          labelText: 'Label',
          hintText: 'Volume Up',
          border: OutlineInputBorder(),
        ),
        onChanged: (_) => setState(() {}),
      ),
      if (_selectedButtonKey == null && _labelController.text.trim().isNotEmpty) ...[
        const SizedBox(height: 6),
        Text(
          'Saved as "$_name" -- type a name matching a real remote button above to have it '
          'bind itself automatically.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ],
      const SizedBox(height: 16),
      Align(
        alignment: Alignment.centerLeft,
        child: FilledButton.icon(
          onPressed: (_busy || !_canStart || !widget.store.hubRunning) ? null : _start,
          icon: const Icon(Icons.sensors),
          label: const Text('Press it now'),
        ),
      ),
      if (!widget.store.hubRunning)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text(
            'Learning talks to the receiver, so it needs the hub running. Start it from Settings.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ),
    ];
  }

  Widget _statusPanel(LearnStatus status) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            if (status.isBusy) ...[
              const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)),
              const SizedBox(width: 12),
            ] else
              Icon(
                status.isCaptured
                    ? Icons.check_circle
                    : status.state == 'mismatch'
                        ? Icons.refresh
                        : Icons.error_outline,
                color: status.isCaptured ? Colors.greenAccent : theme.colorScheme.error,
              ),
            if (!status.isBusy) const SizedBox(width: 12),
            Expanded(child: Text(status.detail, style: theme.textTheme.bodyMedium)),
          ],
        ),
        const SizedBox(height: 12),
        if (status.isCaptured) ...[
          if (status.decoded.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text('${status.decoded} · ${status.pulses} pulses', style: theme.textTheme.bodySmall),
            ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _repeatable,
            onChanged: (value) => setState(() => _repeatable = value),
            title: const Text('Repeat while held'),
            subtitle: const Text('On for volume/channel step and D-pad directions; off for power and menu.'),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            children: [
              if (widget.backend.learnVerifiable)
                OutlinedButton.icon(
                  onPressed: _busy ? null : _test,
                  icon: const Icon(Icons.volume_up),
                  label: const Text('Test'),
                ),
              FilledButton.icon(
                onPressed: _busy ? null : _save,
                icon: const Icon(Icons.save_outlined),
                label: const Text('Save'),
              ),
              TextButton(onPressed: _busy ? null : _cancel, child: const Text('Discard')),
            ],
          ),
        ] else if (status.state == 'mismatch' || status.state == 'failed') ...[
          Wrap(
            spacing: 8,
            children: [
              FilledButton.tonalIcon(
                onPressed: _busy ? null : _start,
                icon: const Icon(Icons.refresh),
                label: const Text('Try again'),
              ),
              TextButton(onPressed: _busy ? null : _cancel, child: const Text('Cancel')),
            ],
          ),
        ] else
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(onPressed: _busy ? null : _cancel, child: const Text('Cancel')),
          ),
      ],
    );
  }
}
