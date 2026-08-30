/// Choosing which of a Home Assistant's entities belong on the remote.
///
/// Every other backend has a command list written in Python, because every
/// other backend talks to one box that does a fixed number of things. A Home
/// Assistant has hundreds of entities and they differ in every house, so the
/// list has to be narrowed by hand — and typing entity ids into the device
/// form's JSON box would be exactly the error-prone step the generated form
/// exists to avoid.
///
/// So: a searchable list, grouped by domain, with the already-chosen ones
/// ticked. Each entity picked becomes a small set of commands back in the
/// binding editor, which is the only place the choice is felt.
library;

import 'package:flutter/material.dart';

import '../api/models.dart';
import '../widgets/selectable_route.dart';

/// Picks entities. Returns the chosen ids in a stable order, or null if
/// the user backed out.
Future<List<String>?> showEntityPicker({
  required BuildContext context,
  required String deviceName,
  required List<EntityInfo> entities,
  required List<String> selected,
}) {
  return pushSelectable<List<String>>(
    context,
    EntityPickerPage(
      deviceName: deviceName,
      entities: entities,
      selected: selected,
    ),
  );
}

class EntityPickerPage extends StatefulWidget {
  const EntityPickerPage({
    super.key,
    required this.deviceName,
    required this.entities,
    required this.selected,
  });

  final String deviceName;
  final List<EntityInfo> entities;
  final List<String> selected;

  @override
  State<EntityPickerPage> createState() => _EntityPickerPageState();
}

class _EntityPickerPageState extends State<EntityPickerPage> {
  /// A set for the ticks, a list for the order.
  ///
  /// Order is not cosmetic here: the hub's suggested bindings point the
  /// remote's two bulb keys at the first two lights picked, so "the first
  /// one" has to mean something. Keeping the order someone chose in is the
  /// only answer that will not surprise them.
  late final List<String> _chosen = [...widget.selected];
  late final TextEditingController _search = TextEditingController();

  /// Entities the hub no longer reports — renamed or removed in Home
  /// Assistant since they were picked. Shown rather than quietly dropped:
  /// every binding pointing at one is broken, and this screen is where that
  /// becomes visible.
  late final List<String> _stale = [
    for (final id in widget.selected)
      if (!widget.entities.any((e) => e.entityId == id)) id,
  ];

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  List<EntityInfo> get _matching {
    final query = _search.text.trim().toLowerCase();
    if (query.isEmpty) return widget.entities;
    return widget.entities
        .where((e) =>
            e.name.toLowerCase().contains(query) || e.entityId.toLowerCase().contains(query))
        .toList();
  }

  void _toggle(String entityId, bool on) {
    setState(() {
      on ? _chosen.add(entityId) : _chosen.remove(entityId);
    });
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final matching = _matching;

    // Grouped by domain, which is how someone thinks about them: all the
    // lights together, all the scenes together.
    final byDomain = <String, List<EntityInfo>>{};
    for (final entity in matching) {
      byDomain.putIfAbsent(entity.domain, () => []).add(entity);
    }
    final domains = byDomain.keys.toList()..sort();

    return Scaffold(
      appBar: AppBar(
        title: Text('Entities for ${widget.deviceName}'),
        actions: [
          FilledButton(
            onPressed: () => Navigator.pop(context, _chosen),
            child: Text(_chosen.isEmpty ? 'Save' : 'Save ${_chosen.length}'),
          ),
          const SizedBox(width: 12),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
            child: TextField(
              controller: _search,
              decoration: InputDecoration(
                prefixIcon: const Icon(Icons.search),
                hintText: 'Search ${widget.entities.length} entities',
                border: const OutlineInputBorder(),
                isDense: true,
                suffixIcon: _search.text.isEmpty
                    ? null
                    : IconButton(
                        icon: const Icon(Icons.clear),
                        onPressed: () => setState(() => _search.clear()),
                      ),
              ),
              onChanged: (_) => setState(() {}),
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    _chosen.isEmpty
                        ? 'Nothing picked yet. Each entity becomes a few commands.'
                        : '${_chosen.length} picked',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
                if (_chosen.isNotEmpty)
                  TextButton(
                    onPressed: () => setState(_chosen.clear),
                    child: const Text('Clear'),
                  ),
              ],
            ),
          ),
          if (_stale.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
              child: Card(
                color: scheme.errorContainer,
                child: ListTile(
                  leading: Icon(Icons.link_off, color: scheme.onErrorContainer),
                  title: Text(
                    '${_stale.length} picked ${_stale.length == 1 ? 'entity is' : 'entities are'} '
                    'no longer in Home Assistant',
                    style: TextStyle(color: scheme.onErrorContainer),
                  ),
                  subtitle: Text(
                    '${_stale.join(', ')} — every binding using ${_stale.length == 1 ? 'it' : 'them'} '
                    'will fail. Removing ${_stale.length == 1 ? 'it' : 'them'} here does not '
                    'remove those bindings.',
                    style: TextStyle(color: scheme.onErrorContainer),
                  ),
                  trailing: TextButton(
                    onPressed: () => setState(() {
                      _chosen.removeWhere(_stale.contains);
                      _stale.clear();
                    }),
                    child: const Text('Remove'),
                  ),
                ),
              ),
            ),
          const SizedBox(height: 8),
          Expanded(
            child: matching.isEmpty
                ? Center(
                    child: Text(
                      widget.entities.isEmpty
                          ? 'Home Assistant reported nothing that can be controlled.'
                          : 'Nothing matches "${_search.text}".',
                      style: TextStyle(color: scheme.outline),
                    ),
                  )
                : ListView.builder(
                    padding: const EdgeInsets.only(bottom: 24),
                    itemCount: domains.length,
                    itemBuilder: (context, index) {
                      final domain = domains[index];
                      final entities = byDomain[domain]!;
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Padding(
                            padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
                            child: Text(
                              '${_domainLabel(domain)}  (${entities.length})',
                              style: Theme.of(context)
                                  .textTheme
                                  .titleSmall
                                  ?.copyWith(color: scheme.primary),
                            ),
                          ),
                          for (final entity in entities)
                            CheckboxListTile(
                              dense: true,
                              value: _chosen.contains(entity.entityId),
                              onChanged: (on) => _toggle(entity.entityId, on ?? false),
                              title: Text(entity.name),
                              subtitle: Text(
                                entity.state.isEmpty
                                    ? entity.entityId
                                    : '${entity.entityId} · ${entity.state}',
                                style: const TextStyle(fontSize: 12),
                              ),
                              secondary: Icon(_domainIcon(domain), size: 20),
                            ),
                        ],
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}

String _domainLabel(String domain) => switch (domain) {
      'input_boolean' => 'Toggles',
      'media_player' => 'Media players',
      'input_button' => 'Buttons',
      _ => '${domain[0].toUpperCase()}${domain.substring(1).replaceAll('_', ' ')}s',
    };

IconData _domainIcon(String domain) => switch (domain) {
      'light' => Icons.lightbulb_outline,
      'switch' => Icons.power_settings_new,
      'input_boolean' => Icons.toggle_on_outlined,
      'scene' => Icons.auto_awesome_outlined,
      'script' => Icons.play_circle_outline,
      'automation' => Icons.bolt_outlined,
      'cover' => Icons.blinds_outlined,
      'media_player' => Icons.speaker_outlined,
      'climate' => Icons.thermostat_outlined,
      'lock' => Icons.lock_outline,
      'fan' => Icons.mode_fan_off_outlined,
      'vacuum' => Icons.cleaning_services_outlined,
      'button' || 'input_button' => Icons.radio_button_checked,
      _ => Icons.category_outlined,
    };
