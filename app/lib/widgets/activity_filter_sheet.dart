/// The activity log's filter & sort popup.
///
/// Every section it shows is read off `ActivityFilter.dimensionsOf`, not
/// listed by hand -- so a new kind of hub event shows up here as soon as one
/// arrives, with no change to this file.
library;

import 'package:flutter/material.dart';

import '../state/activity_filter.dart';
import '../state/hub_store.dart';

Future<void> showActivityFilterDialog(BuildContext context, HubStore store) {
  final queryController = TextEditingController(text: store.activityFilter.query);

  return showDialog<void>(
    context: context,
    builder: (context) => StatefulBuilder(
      builder: (context, setState) {
        final filter = store.activityFilter;

        void update(void Function(ActivityFilter filter) mutate) {
          store.updateActivityFilter(mutate);
          setState(() {});
        }

        final dimensions = filter.dimensionsOf(store.events);

        return AlertDialog(
          title: const Text('Filter & sort activity'),
          content: SizedBox(
            width: 360,
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: queryController,
                    decoration: const InputDecoration(
                      labelText: 'Search',
                      prefixIcon: Icon(Icons.search),
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                    onChanged: (value) => update((f) => f.query = value),
                  ),
                  const SizedBox(height: 16),
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(
                        child: DropdownButtonFormField<String>(
                          initialValue: filter.sortBy,
                          isExpanded: true,
                          decoration: const InputDecoration(
                            labelText: 'Sort by',
                            isDense: true,
                            border: OutlineInputBorder(),
                          ),
                          items: [
                            const DropdownMenuItem(value: 'at', child: Text('Time')),
                            for (final dimension in dimensions.keys)
                              DropdownMenuItem(value: dimension, child: Text(dimensionLabel(dimension))),
                          ],
                          onChanged: (value) {
                            if (value != null) update((f) => f.sortBy = value);
                          },
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton.filledTonal(
                        tooltip: filter.ascending ? 'Ascending' : 'Descending',
                        icon: Icon(filter.ascending ? Icons.arrow_upward : Icons.arrow_downward),
                        onPressed: () => update((f) => f.ascending = !f.ascending),
                      ),
                    ],
                  ),
                  for (final dimension in dimensions.keys)
                    _DimensionSection(
                      dimension: dimension,
                      values: dimensions[dimension]!,
                      filter: filter,
                      onChanged: update,
                    ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: filter.isActive
                  ? () => update((f) {
                        f.clear();
                        queryController.text = '';
                      })
                  : null,
              child: const Text('Reset'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Done'),
            ),
          ],
        );
      },
    ),
  );
}

class _DimensionSection extends StatelessWidget {
  const _DimensionSection({
    required this.dimension,
    required this.values,
    required this.filter,
    required this.onChanged,
  });

  final String dimension;
  final List<String> values;
  final ActivityFilter filter;
  final void Function(void Function(ActivityFilter filter) mutate) onChanged;

  @override
  Widget build(BuildContext context) {
    final allHidden = values.every((v) => filter.isHidden(dimension, v));

    return Padding(
      padding: const EdgeInsets.only(top: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(dimensionLabel(dimension), style: Theme.of(context).textTheme.titleSmall),
              ),
              TextButton(
                onPressed: () => onChanged((f) {
                  if (allHidden) {
                    f.showAll(dimension);
                  } else {
                    f.hideAll(dimension, values);
                  }
                }),
                child: Text(allHidden ? 'Select all' : 'Clear'),
              ),
            ],
          ),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              for (final value in values)
                FilterChip(
                  label: Text(dimensionValueLabel(dimension, value)),
                  selected: !filter.isHidden(dimension, value),
                  onSelected: (_) => onChanged((f) => f.toggle(dimension, value)),
                ),
            ],
          ),
        ],
      ),
    );
  }
}
