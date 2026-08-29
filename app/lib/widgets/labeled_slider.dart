/// A labelled slider with the sentence explaining what the value means.
///
/// The sentence changes with the value rather than describing the control in
/// the abstract: "a press shorter than 0.5s does not repeat" is something a
/// person can check against what their thumb just did. Shared between the
/// per-button repeat timing in the binding editor and the config-wide
/// defaults in the Scenes screen, since both are "seconds, with off at zero."
library;

import 'package:flutter/material.dart';

class LabeledSlider extends StatelessWidget {
  const LabeledSlider({
    super.key,
    required this.label,
    required this.value,
    required this.min,
    required this.max,
    required this.divisions,
    required this.onChanged,
    required this.description,
  });

  final String label;
  final double value;
  final double min;
  final double max;
  final int divisions;
  final ValueChanged<double> onChanged;
  final String description;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(child: Text(label, style: Theme.of(context).textTheme.bodyMedium)),
            Text(
              value == 0 ? 'off' : '${value.toStringAsFixed(1)}s',
              style: Theme.of(context).textTheme.labelLarge,
            ),
          ],
        ),
        Slider(
          value: value.clamp(min, max),
          min: min,
          max: max,
          divisions: divisions,
          label: value == 0 ? 'off' : '${value.toStringAsFixed(1)}s',
          onChanged: onChanged,
        ),
        Text(
          description,
          style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: Theme.of(context).colorScheme.outline,
              ),
        ),
      ],
    );
  }
}
