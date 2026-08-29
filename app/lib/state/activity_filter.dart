/// Filtering and sorting for the activity log.
///
/// The set of things you can filter by is not hard-coded here -- it is read
/// off whatever events are actually flowing (see `HubEvent.facets`), so a
/// field the hub starts sending tomorrow becomes a filterable dimension
/// today with no change to this file.
library;

import '../api/models.dart';

class ActivityFilter {
  /// Dimension -> values hidden from the log. Empty (the default) hides
  /// nothing. Storing exclusions rather than inclusions is what lets a
  /// value nobody has configured yet -- a new event type, say -- show up
  /// already visible instead of already filtered out.
  final Map<String, Set<String>> hidden = {};

  String query = '';

  /// Dimension to sort by, or 'at' for chronological -- the default.
  String sortBy = 'at';

  /// Oldest/lowest first when true. Newest/highest first (the default) when
  /// false.
  bool ascending = false;

  bool get isActive =>
      query.trim().isNotEmpty ||
      hidden.values.any((values) => values.isNotEmpty) ||
      sortBy != 'at' ||
      ascending;

  void clear() {
    hidden.clear();
    query = '';
    sortBy = 'at';
    ascending = false;
  }

  /// This filter's state, for [UiPrefs] (`kActivityFilter`) to persist. Not
  /// used for talking to the hub -- this never leaves the browser.
  Map<String, dynamic> toJson() => {
        'hidden': hidden.map((key, values) => MapEntry(key, values.toList())),
        'query': query,
        'sort_by': sortBy,
        'ascending': ascending,
      };

  /// Restores state saved by [toJson]. Reads each field independently and
  /// falls back to that field's current value on anything unexpected, so a
  /// document mangled by a future build or a hand-edited value degrades one
  /// field at a time rather than discarding the whole filter.
  void applyJson(Object? json) {
    if (json is! Map) return;

    final rawHidden = json['hidden'];
    if (rawHidden is Map) {
      hidden.clear();
      for (final entry in rawHidden.entries) {
        final key = entry.key;
        final value = entry.value;
        if (key is String && value is List) {
          hidden[key] = value.whereType<String>().toSet();
        }
      }
    }

    final rawQuery = json['query'];
    if (rawQuery is String) query = rawQuery;

    final rawSortBy = json['sort_by'];
    if (rawSortBy is String) sortBy = rawSortBy;

    final rawAscending = json['ascending'];
    if (rawAscending is bool) ascending = rawAscending;
  }

  void toggle(String dimension, String value) {
    final values = hidden.putIfAbsent(dimension, () => {});
    if (!values.add(value)) values.remove(value);
  }

  bool isHidden(String dimension, String value) => hidden[dimension]?.contains(value) ?? false;

  void showAll(String dimension) => hidden[dimension]?.clear();

  void hideAll(String dimension, Iterable<String> values) => hidden[dimension] = values.toSet();

  /// Every dimension seen across [events], each with its observed values in
  /// sorted order. A value that is hidden but no longer present in [events]
  /// is kept too, so a filter someone set up does not silently vanish just
  /// because nothing matching it happened recently.
  Map<String, List<String>> dimensionsOf(List<HubEvent> events) {
    final out = <String, Set<String>>{};
    for (final event in events) {
      for (final entry in event.facets.entries) {
        out.putIfAbsent(entry.key, () => {}).add(entry.value);
      }
    }
    for (final entry in hidden.entries) {
      if (entry.value.isEmpty) continue;
      out.putIfAbsent(entry.key, () => {}).addAll(entry.value);
    }
    return {
      for (final entry in out.entries) entry.key: entry.value.toList()..sort(),
    };
  }

  List<HubEvent> apply(List<HubEvent> events) {
    var result = events.where(_visible).toList();
    final needle = query.trim().toLowerCase();
    if (needle.isNotEmpty) {
      result = result.where((event) => event.searchText.contains(needle)).toList();
    }
    result.sort(_compare);
    return result;
  }

  bool _visible(HubEvent event) {
    for (final entry in hidden.entries) {
      if (entry.value.isEmpty) continue;
      final value = event.facets[entry.key];
      // An event that does not carry this dimension at all is never hidden
      // by it -- hiding scene=standby must not also swallow every button
      // press, which has no scene of its own.
      if (value == null) continue;
      if (entry.value.contains(value)) return false;
    }
    return true;
  }

  int _compare(HubEvent a, HubEvent b) {
    if (sortBy != 'at') {
      final av = a.facets[sortBy];
      final bv = b.facets[sortBy];
      if ((av == null) != (bv == null)) {
        return av == null ? 1 : -1; // missing values always sort last
      }
      if (av != null && bv != null && av != bv) {
        final cmp = av.compareTo(bv);
        return ascending ? cmp : -cmp;
      }
    } else if (a.at != b.at) {
      final cmp = a.at.compareTo(b.at);
      return ascending ? cmp : -cmp;
    }
    return b.at.compareTo(a.at); // tiebreak: newest first
  }
}

/// A dimension key turned into words worth showing someone -- known ones get
/// a specific label, anything new falls back to title-casing its name.
String dimensionLabel(String dimension) {
  const known = {
    'type': 'Event type',
    'phase': 'Phase',
    'scene': 'Scene',
    'action': 'Action',
    'button': 'Button',
    'ok': 'Outcome',
  };
  return known[dimension] ?? _titleCase(dimension);
}

/// A value within a dimension, turned into words -- `ok`'s booleans read as
/// prose rather than `true`/`false`, everything else is title-cased.
String dimensionValueLabel(String dimension, String value) {
  if (dimension == 'ok') return value == 'true' ? 'OK' : 'Failed';
  return _titleCase(value);
}

String _titleCase(String text) => text
    .split(RegExp(r'[_\s]+'))
    .where((word) => word.isNotEmpty)
    .map((word) => word[0].toUpperCase() + word.substring(1))
    .join(' ');
