/// Teaching the hub what the buttons on your remote are called.
///
/// A Harmony remote identifies each button by a four-byte signature, and
/// there is no formula turning that into "Volume Up" -- somebody has to press
/// the button and say what it is. That used to mean two terminal commands and
/// a capture file; this is the same job done by pressing buttons and typing
/// names.
///
/// It needs no capture step of its own because the hub already publishes an
/// unlearned press under its own hex rather than dropping it. That decision
/// -- made so a new button stays visible in the live view -- is exactly what
/// makes this screen possible: the signatures are already streaming past.
library;

import 'package:flutter/material.dart';

import '../api/models.dart';
import '../state/hub_store.dart';
import 'scenes_screen.dart' show slugify;

/// Opens the learning screen. Reached from the live view's empty state and
/// from Settings, which are the two places you notice buttons are missing.
Future<void> openLearnPage(BuildContext context, HubStore store) => Navigator.push(
      context,
      MaterialPageRoute<void>(builder: (_) => LearnPage(store: store)),
    );

/// A signature seen on the wire that nobody has named yet.
class _Pending {
  _Pending({required this.signature, required String suggested})
      : controller = TextEditingController(text: suggested);

  final String signature;
  final TextEditingController controller;

  /// How many times it has been pressed since this screen opened. Shown
  /// because pressing a button twice and seeing the count rise is the
  /// clearest possible confirmation that the right row is the right button.
  int presses = 1;

  String get label => controller.text.trim();

  bool get isNamed => label.isNotEmpty;

  String get key => slugify(label);
}

class LearnPage extends StatefulWidget {
  const LearnPage({super.key, required this.store});

  final HubStore store;

  @override
  State<LearnPage> createState() => _LearnPageState();
}

class _LearnPageState extends State<LearnPage> {
  /// Unnamed signatures, in the order they were first pressed.
  final Map<String, _Pending> _pending = {};

  /// Signatures the user has waved away. Kept rather than forgotten so a
  /// stray press does not keep reappearing on every event.
  final Set<String> _ignored = {};

  /// The most recent press of a button that *is* already known, so the user
  /// gets confirmation rather than silence when they press something twice.
  String? _lastKnownPress;

  bool _saving = false;

  @override
  void initState() {
    super.initState();
    widget.store.addListener(_onStoreChanged);
    _harvest();
  }

  @override
  void dispose() {
    widget.store.removeListener(_onStoreChanged);
    for (final pending in _pending.values) {
      pending.controller.dispose();
    }
    super.dispose();
  }

  void _onStoreChanged() {
    if (mounted) _harvest();
  }

  /// Picks new signatures out of the live event log.
  ///
  /// Reads the whole log each time rather than hooking the stream directly:
  /// the store is the only thing subscribed to the socket, and re-reading a
  /// few hundred events is free next to the alternative of a second listener
  /// that could miss what arrived before this screen opened.
  void _harvest() {
    final known = widget.store.knownButtonKeys;
    var changed = false;
    String? lastKnown;

    // Oldest first, so the rows end up in the order the buttons were pressed.
    for (final event in widget.store.events.reversed) {
      if (event.type != 'button') continue;
      final signature = event.button;
      if (signature == null) continue;

      if (known.contains(signature)) {
        lastKnown = event.label ?? signature;
        continue;
      }
      if (_ignored.contains(signature)) continue;

      final existing = _pending[signature];
      if (existing == null) {
        _pending[signature] = _Pending(
          signature: signature,
          // The hub already decodes the HID usage, so most buttons arrive
          // with a usable name and this screen is mostly confirmation.
          suggested: _suggest(event.label, signature),
        );
        changed = true;
      } else if (event.phase == 'press') {
        existing.presses++;
        changed = true;
      }
    }

    if (lastKnown != _lastKnownPress) {
      _lastKnownPress = lastKnown;
      changed = true;
    }
    if (changed) setState(() {});
  }

  /// A name to start from. Anything the HID tables could not name comes
  /// through looking like its own signature, which is no use as a label.
  String _suggest(String? label, String signature) {
    if (label == null) return '';
    final looksRaw = label.startsWith('<') || label.toUpperCase() == signature.toUpperCase();
    return looksRaw ? '' : label;
  }

  // ------------------------------------------------------------------

  List<_Pending> get _rows => _pending.values.toList();

  List<_Pending> get _saveable => _rows.where((p) => p.isNamed).toList();

  Future<void> _save() async {
    final ready = _saveable;
    if (ready.isEmpty) return;

    setState(() => _saving = true);

    // Rows that resolve to the same key are one button with several
    // signatures, which the map supports on purpose -- the same key can
    // report differently depending on the active activity.
    final merged = <String, ButtonInfo>{};
    for (final row in ready) {
      final existing = merged[row.key];
      if (existing == null) {
        merged[row.key] = ButtonInfo(
          key: row.key,
          label: row.label,
          signatures: [row.signature],
        );
      } else {
        existing.signatures.add(row.signature);
      }
    }

    final saved = await widget.store.learnButtons(merged.values.toList());
    if (!mounted) return;

    setState(() {
      _saving = false;
      if (saved) {
        for (final row in ready) {
          _pending.remove(row.signature)?.controller.dispose();
        }
      }
    });

    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          saved
              ? 'Learned ${merged.length} button${merged.length == 1 ? '' : 's'}'
              : widget.store.error ?? 'Could not save',
        ),
      ),
    );
  }

  void _discard(_Pending row) {
    setState(() {
      _ignored.add(row.signature);
      _pending.remove(row.signature)?.controller.dispose();
    });
  }

  Future<void> _forget(ButtonInfo button) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Forget "${button.label}"?'),
        content: const Text(
          'The hub will stop recognising this button until it is learned again. '
          'Anything bound to it stops working.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Forget')),
        ],
      ),
    );
    if (confirmed != true) return;

    if (!await widget.store.forgetButton(button.key) && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(widget.store.error ?? 'Could not forget it')),
      );
    }
  }

  // ------------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final store = widget.store;
    final rows = _rows;
    final ready = _saveable.length;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Learn the remote'),
        actions: [
          if (_ignored.isNotEmpty)
            TextButton(
              onPressed: () {
                setState(_ignored.clear);
                // Re-reads the log, which is where the discarded signatures
                // still are -- clearing the set alone would only hide this
                // button.
                _harvest();
              },
              child: Text('Un-ignore ${_ignored.length}'),
            ),
          const SizedBox(width: 8),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _ListeningCard(store: store, lastKnownPress: _lastKnownPress, waiting: rows.isEmpty),
          const SizedBox(height: 16),
          if (rows.isNotEmpty) ...[
            Row(
              children: [
                Text('New buttons', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Name each one, then save. Leave a name blank to skip it for now.',
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            for (final row in rows)
              _PendingRow(
                row: row,
                known: store.buttons,
                onChanged: () => setState(() {}),
                onDiscard: () => _discard(row),
              ),
            const SizedBox(height: 8),
          ],
          _KnownButtons(buttons: store.buttons, onForget: _forget, lastPressed: _lastKnownPress),
          const SizedBox(height: 80),
        ],
      ),
      bottomNavigationBar: rows.isEmpty
          ? null
          : SafeArea(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
                child: Row(
                  children: [
                    Text(
                      '${rows.length} waiting · $ready named',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                    const Spacer(),
                    FilledButton.icon(
                      onPressed: ready == 0 || _saving ? null : _save,
                      icon: const Icon(Icons.save),
                      label: Text(ready == 0 ? 'Save' : 'Save $ready'),
                    ),
                  ],
                ),
              ),
            ),
    );
  }
}

// ----------------------------------------------------------------------

/// Says whether the hub is actually in a position to hear the remote.
///
/// Without this the screen is indistinguishable from a broken one: you press
/// buttons, nothing appears, and there is no way to tell whether the radio is
/// off, the remote is out of range, or the app is at fault.
class _ListeningCard extends StatelessWidget {
  const _ListeningCard({
    required this.store,
    required this.lastKnownPress,
    required this.waiting,
  });

  final HubStore store;
  final String? lastKnownPress;
  final bool waiting;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final source = store.settings?.source ?? 'none';

    final (IconData icon, String title, String detail, bool ok) = switch (true) {
      _ when !store.hubRunning => (
          Icons.pause_circle_outline,
          'The hub is stopped',
          'It has to be running to hear the remote. Start it on the Settings tab.',
          false,
        ),
      _ when source == 'none' => (
          Icons.radio_button_unchecked,
          'Not listening to a remote',
          'The event source is set to "None", so no real button presses arrive. '
              'Set it to Radio on the Settings tab.',
          false,
        ),
      _ when source == 'replay' => (
          Icons.play_circle_outline,
          'Listening to a recording',
          'Buttons from the capture file will appear below as it plays.',
          true,
        ),
      _ => (
          Icons.settings_remote,
          'Listening',
          'Press a button on your Harmony remote and it will appear below.',
          true,
        ),
    };

    return Card(
      color: ok ? null : scheme.errorContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: ok ? scheme.primary : scheme.onErrorContainer),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: Theme.of(context).textTheme.titleMedium?.copyWith(
                              color: ok ? null : scheme.onErrorContainer,
                            ),
                      ),
                      Text(
                        detail,
                        style: Theme.of(context).textTheme.bodySmall?.copyWith(
                              color: ok ? null : scheme.onErrorContainer,
                            ),
                      ),
                    ],
                  ),
                ),
                // Deliberately not a spinner: nothing is loading, we are
                // waiting on a person. A spinner would claim work is
                // happening and never stop claiming it.
                if (ok && waiting)
                  Text('waiting', style: Theme.of(context).textTheme.labelSmall),
              ],
            ),
            if (ok && lastKnownPress != null) ...[
              const SizedBox(height: 12),
              Text(
                'Last press: $lastKnownPress — already learned, nothing to do.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _PendingRow extends StatelessWidget {
  const _PendingRow({
    required this.row,
    required this.known,
    required this.onChanged,
    required this.onDiscard,
  });

  final _Pending row;
  final List<ButtonInfo> known;
  final VoidCallback onChanged;
  final VoidCallback onDiscard;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // Typing a name that resolves to a button already known is not a clash:
    // it attaches this signature to that button. Worth saying out loud,
    // because it is also how you would do it by accident.
    final merges = row.isNamed ? known.where((b) => b.key == row.key).firstOrNull : null;

    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Text(
                        row.signature,
                        style: theme.textTheme.labelMedium?.copyWith(
                          fontFamily: 'monospace',
                          color: theme.colorScheme.onSurfaceVariant,
                        ),
                      ),
                      const SizedBox(width: 8),
                      if (row.presses > 1)
                        Text('pressed ${row.presses}×', style: theme.textTheme.labelSmall),
                    ],
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: row.controller,
                    onChanged: (_) => onChanged(),
                    decoration: InputDecoration(
                      labelText: 'What is this button called?',
                      hintText: 'e.g. Volume Up',
                      border: const OutlineInputBorder(),
                      isDense: true,
                      helperMaxLines: 2,
                      helperText: switch (true) {
                        _ when !row.isNamed => 'Unnamed — will be skipped when you save.',
                        _ when merges != null =>
                          'Adds this signature to the existing "${merges.label}".',
                        _ => 'Saves as ${row.key}',
                      },
                    ),
                  ),
                ],
              ),
            ),
            IconButton(
              tooltip: 'Not a button I want',
              onPressed: onDiscard,
              icon: const Icon(Icons.close),
            ),
          ],
        ),
      ),
    );
  }
}

class _KnownButtons extends StatelessWidget {
  const _KnownButtons({required this.buttons, required this.onForget, required this.lastPressed});

  final List<ButtonInfo> buttons;
  final void Function(ButtonInfo) onForget;
  final String? lastPressed;

  @override
  Widget build(BuildContext context) {
    if (buttons.isEmpty) {
      return const SizedBox.shrink();
    }
    final scheme = Theme.of(context).colorScheme;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Already learned (${buttons.length})', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 4),
        Text(
          'Press one to confirm it is recognised. Forgetting a button breaks anything bound to it.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
        const SizedBox(height: 8),
        Card(
          child: Column(
            children: [
              for (final button in buttons)
                ListTile(
                  dense: true,
                  selected: button.label == lastPressed,
                  selectedTileColor: scheme.primaryContainer,
                  title: Text(button.label),
                  subtitle: Text(
                    '${button.key} · ${button.signatures.join(', ')}',
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 11),
                  ),
                  trailing: IconButton(
                    tooltip: 'Forget this button',
                    icon: const Icon(Icons.delete_outline),
                    onPressed: () => onForget(button),
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}
