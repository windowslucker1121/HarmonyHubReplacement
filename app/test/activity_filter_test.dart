/// The activity log's filter/sort logic.
///
/// The point of `ActivityFilter` is that its dimensions are discovered, not
/// listed by hand -- so these tests lean on events carrying fields the class
/// has never heard of, to prove a new kind of hub event needs no Dart change
/// to become filterable.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:harmony_hub_app/api/models.dart';
import 'package:harmony_hub_app/state/activity_filter.dart';

HubEvent _event(Map<String, dynamic> json) => HubEvent.fromJson({
      'at': DateTime.now().toIso8601String(),
      ...json,
    });

void main() {
  group('dimensionsOf', () {
    test('discovers dimensions and values from event fields alone', () {
      final events = [
        _event({'type': 'button', 'phase': 'press'}),
        _event({'type': 'scene', 'scene': 'watch_tv', 'ok': true}),
      ];

      final dims = ActivityFilter().dimensionsOf(events);

      expect(dims['type'], ['button', 'scene']);
      expect(dims['phase'], ['press']);
      expect(dims['scene'], ['watch_tv']);
      expect(dims['ok'], ['true']);
    });

    test('picks up a field this class has never seen, automatically', () {
      final events = [
        _event({'type': 'macro', 'device': 'projector', 'severity': 'warn'}),
      ];

      final dims = ActivityFilter().dimensionsOf(events);

      expect(dims['device'], ['projector']);
      expect(dims['severity'], ['warn']);
    });

    test('excludes free-text and timestamp fields', () {
      final events = [
        _event({'type': 'status', 'detail': 'Engine started', 'label': 'Volume Up'}),
      ];

      final dims = ActivityFilter().dimensionsOf(events);

      expect(dims.containsKey('at'), isFalse);
      expect(dims.containsKey('detail'), isFalse);
      expect(dims.containsKey('label'), isFalse);
    });

    test('keeps a stale hidden value visible so an active filter never disappears', () {
      final filter = ActivityFilter()..toggle('type', 'button');
      final events = [_event({'type': 'scene'})];

      final dims = filter.dimensionsOf(events);

      expect(dims['type'], containsAll(['button', 'scene']));
    });
  });

  group('apply', () {
    test('hiding a value removes only events carrying that value', () {
      final events = [
        _event({'type': 'button', 'phase': 'press'}),
        _event({'type': 'scene'}),
      ];
      final filter = ActivityFilter()..toggle('type', 'button');

      final visible = filter.apply(events);

      expect(visible.map((e) => e.type), ['scene']);
    });

    test('hiding a value never hides an event missing that dimension', () {
      final events = [
        _event({'type': 'button', 'scene': 'watch_tv'}),
        _event({'type': 'button'}), // no scene of its own
      ];
      final filter = ActivityFilter()..toggle('scene', 'watch_tv');

      final visible = filter.apply(events);

      expect(visible.length, 1);
      expect(visible.first.scene, isNull);
    });

    test('search matches against the summary text', () {
      final events = [
        _event({'type': 'button', 'label': 'Volume Up', 'phase': 'press'}),
        _event({'type': 'button', 'label': 'Mute', 'phase': 'press'}),
      ];
      final filter = ActivityFilter()..query = 'volume';

      final visible = filter.apply(events);

      expect(visible.length, 1);
      expect(visible.first.label, 'Volume Up');
    });

    test('default sort is newest first', () {
      final older = _event({'type': 'button', 'at': '2026-01-01T00:00:00.000'});
      final newer = _event({'type': 'button', 'at': '2026-01-02T00:00:00.000'});

      final visible = ActivityFilter().apply([older, newer]);

      expect(visible, [newer, older]);
    });

    test('sorting by a discovered dimension ties back to newest-first', () {
      final buttonOld = _event({'type': 'button', 'at': '2026-01-01T00:00:00.000'});
      final buttonNew = _event({'type': 'button', 'at': '2026-01-03T00:00:00.000'});
      final scene = _event({'type': 'scene', 'at': '2026-01-02T00:00:00.000'});
      final filter = ActivityFilter()..sortBy = 'type';

      final visible = filter.apply([buttonOld, scene, buttonNew]);

      // Descending alphabetical by type ('scene' before 'button'); within a
      // type, newest first.
      expect(visible, [scene, buttonNew, buttonOld]);
    });

    test('ascending flips direction', () {
      final a = _event({'type': 'action'});
      final b = _event({'type': 'button'});
      final filter = ActivityFilter()
        ..sortBy = 'type'
        ..ascending = true;

      final visible = filter.apply([b, a]);

      expect(visible.map((e) => e.type), ['action', 'button']);
    });
  });

  group('isActive / clear', () {
    test('a fresh filter is not active', () {
      expect(ActivityFilter().isActive, isFalse);
    });

    test('a hidden value, a query, or a non-default sort each count as active', () {
      expect((ActivityFilter()..toggle('type', 'button')).isActive, isTrue);
      expect((ActivityFilter()..query = 'x').isActive, isTrue);
      expect((ActivityFilter()..sortBy = 'type').isActive, isTrue);
      expect((ActivityFilter()..ascending = true).isActive, isTrue);
    });

    test('clear resets everything', () {
      final filter = ActivityFilter()
        ..toggle('type', 'button')
        ..query = 'x'
        ..sortBy = 'type'
        ..ascending = true;

      filter.clear();

      expect(filter.isActive, isFalse);
    });
  });

  group('labels', () {
    test('known dimensions get a specific label', () {
      expect(dimensionLabel('ok'), 'Outcome');
      expect(dimensionLabel('phase'), 'Phase');
    });

    test('an unknown dimension title-cases its name', () {
      expect(dimensionLabel('device_zone'), 'Device Zone');
    });

    test('ok values read as prose', () {
      expect(dimensionValueLabel('ok', 'true'), 'OK');
      expect(dimensionValueLabel('ok', 'false'), 'Failed');
    });

    test('other values are title-cased', () {
      expect(dimensionValueLabel('phase', 'repeat'), 'Repeat');
    });
  });
}
