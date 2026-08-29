/// Editing what one button does.
///
/// The four phases are shown together because the interesting decisions are
/// about how they relate: whether a button should ramp while held, and
/// whether a long press should mean something different from a tap.
library;

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/config.dart';
import '../widgets/action_editor.dart';
import '../widgets/labeled_slider.dart';

class BindingEditorPage extends StatefulWidget {
  const BindingEditorPage({
    super.key,
    required this.buttonLabel,
    required this.binding,
    required this.config,
    required this.api,
    required this.scopeDescription,
  });

  final String buttonLabel;
  final Binding binding;
  final HubConfig config;
  final HubApi api;

  /// Where this binding lives, e.g. "Watch TV" or "every scene".
  final String scopeDescription;

  @override
  State<BindingEditorPage> createState() => _BindingEditorPageState();
}

class _BindingEditorPageState extends State<BindingEditorPage> {
  late Binding _binding;

  /// Whether this button has its own repeat timing rather than following
  /// the config-wide default. Tracked separately from whether the fields are
  /// null so switching the toggle off can clear them without also losing
  /// track of "the user turned this on", which a null check alone can't do.
  late bool _customRepeatTiming;

  static const _phases = [
    (
      key: 'press',
      title: 'On press',
      hint: 'What a normal tap does.',
    ),
    (
      key: 'repeat',
      title: 'While held',
      hint: 'Fires over and over while the button is down. Good for volume, wrong for power. '
          'The timing is set below.',
    ),
    (
      key: 'hold',
      title: 'On long press',
      hint: 'Adding anything here delays the tap action until the button is released.',
    ),
    (
      key: 'release',
      title: 'On release',
      hint: 'Runs when the button comes back up.',
    ),
  ];

  @override
  void initState() {
    super.initState();
    _binding = widget.binding.copy();
    _customRepeatTiming = _binding.repeatDelay != null || _binding.repeatInterval != null;
  }

  void _setCustomRepeatTiming(bool custom) {
    setState(() {
      _customRepeatTiming = custom;
      if (custom) {
        // Start from what this button is actually doing right now -- the
        // config default -- rather than from zero, so turning the toggle on
        // does not silently change the button's behaviour.
        _binding.repeatDelay ??= widget.config.defaultRepeatDelay;
        _binding.repeatInterval ??= widget.config.defaultRepeatInterval;
      } else {
        _binding.repeatDelay = null;
        _binding.repeatInterval = null;
      }
    });
  }

  /// What this button will actually do while it follows the remote-wide
  /// default, read live off `config` so it stays right if that default is
  /// changed from the Scenes tab.
  String _describeDefaultTiming(HubConfig config) {
    final delay = config.defaultRepeatDelay;
    final interval = config.defaultRepeatInterval;
    final waits = delay == 0
        ? 'repeats from the first packet'
        : 'waits ${delay.toStringAsFixed(1)}s before repeating';
    final paced = interval == 0 ? '' : ', at most once every ${interval.toStringAsFixed(1)}s';
    return 'Follows the remote-wide default: $waits$paced.';
  }

  void _setPhase(String phase, List<HubAction> actions) {
    setState(() {
      switch (phase) {
        case 'press':
          _binding.onPress = actions;
        case 'repeat':
          _binding.onRepeat = actions;
        case 'hold':
          _binding.onHold = actions;
        default:
          _binding.onRelease = actions;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.buttonLabel),
        actions: [
          // Emptying every phase by hand would do the same thing, but the
          // list this editor is reached from used to offer a one-tap clear
          // and the picture that replaced it has nowhere to put one.
          if (!_binding.isEmpty)
            TextButton(
              onPressed: () => Navigator.pop(context, Binding()),
              child: const Text('Unbind'),
            ),
          TextButton(
            onPressed: () => Navigator.pop(context, _binding),
            child: const Text('Done'),
          ),
          const SizedBox(width: 8),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            'Applies in ${widget.scopeDescription}.',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 16),
          for (final phase in _phases) ...[
            Card(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    ActionListEditor(
                      title: phase.title,
                      actions: _binding.phase(phase.key),
                      config: widget.config,
                      api: widget.api,
                      onChanged: (actions) => _setPhase(phase.key, actions),
                      emptyHint: 'Nothing — this phase is ignored',
                    ),
                    Text(
                      phase.hint,
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                            color: Theme.of(context).colorScheme.outline,
                          ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
          ],
          // Only shown when something actually repeats, for the same reason
          // the hold threshold below is: a slider that changes nothing is
          // just another thing to wonder about.
          if (_binding.onRepeat.isNotEmpty)
            Card(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Repeat timing', style: Theme.of(context).textTheme.titleSmall),
                    const SizedBox(height: 4),
                    Text(
                      'The remote reports a held button about every 100ms and never says how '
                      'long it has been down. Without a wait, an ordinary short press looks '
                      'identical to a hold and fires three or four times.',
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                            color: Theme.of(context).colorScheme.outline,
                          ),
                    ),
                    const SizedBox(height: 8),
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      value: _customRepeatTiming,
                      onChanged: _setCustomRepeatTiming,
                      title: const Text('Custom timing for this button'),
                      subtitle: Text(
                        _customRepeatTiming
                            ? 'Ignores the remote-wide default below.'
                            : _describeDefaultTiming(widget.config),
                      ),
                    ),
                    if (_customRepeatTiming) ...[
                      const SizedBox(height: 4),
                      LabeledSlider(
                        label: 'Wait before repeating',
                        value: _binding.repeatDelay!,
                        min: 0,
                        max: 2.0,
                        divisions: 20,
                        onChanged: (value) => setState(() => _binding.repeatDelay = value),
                        description: _binding.repeatDelay == 0
                            ? 'Repeats from the very first packet. A quick press will fire this '
                                'several times.'
                            : 'A press shorter than ${_binding.repeatDelay!.toStringAsFixed(1)}s '
                                'does not repeat at all.',
                      ),
                      const SizedBox(height: 8),
                      LabeledSlider(
                        label: 'Slowest repeat',
                        value: _binding.repeatInterval!,
                        min: 0,
                        max: 2.0,
                        divisions: 20,
                        onChanged: (value) => setState(() => _binding.repeatInterval = value),
                        description: _binding.repeatInterval == 0
                            ? 'Follows the remote, about ten times a second.'
                            : 'At most once every ${_binding.repeatInterval!.toStringAsFixed(1)}s '
                                'while held.',
                      ),
                    ],
                  ],
                ),
              ),
            ),
          if (_binding.onRepeat.isNotEmpty) const SizedBox(height: 12),
          if (_binding.onHold.isNotEmpty)
            Card(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Hold threshold', style: Theme.of(context).textTheme.titleSmall),
                    Slider(
                      value: _binding.holdSeconds,
                      min: 0.2,
                      max: 3.0,
                      divisions: 28,
                      label: '${_binding.holdSeconds.toStringAsFixed(1)}s',
                      onChanged: (value) => setState(() => _binding.holdSeconds = value),
                    ),
                    Text(
                      'A press longer than ${_binding.holdSeconds.toStringAsFixed(1)}s counts as a hold. '
                      'Until then the tap action is held back, so this is also how long a tap waits '
                      'before it fires.',
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                            color: Theme.of(context).colorScheme.outline,
                          ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}
