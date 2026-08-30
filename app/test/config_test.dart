/// Configuration round-tripping.
///
/// These matter more than they look: every one of these objects is read from
/// the hub, edited, and written straight back. A field dropped by `toJson`
/// would silently delete part of someone's configuration on the next save.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:harmony_hub_app/api/config.dart';
import 'package:harmony_hub_app/api/models.dart';
import 'package:harmony_hub_app/api/settings.dart';
import 'package:harmony_hub_app/screens/scenes_screen.dart' show slugify;

const Map<String, dynamic> fullConfig = {
  'version': 1,
  'devices': [
    {
      'id': 'tv',
      'name': 'Living Room TV',
      'backend': 'http',
      'config': {'base_url': 'http://10.0.0.5', 'commands': {}},
      'power_policy': 'leave_on',
      'power_on_command': 'on',
      'power_off_command': 'off',
    }
  ],
  'scenes': [
    {
      'id': 'watch_tv',
      'name': 'Watch TV',
      'icon': 'tv',
      'devices': ['tv'],
      'on_start': [
        {'type': 'device', 'device': 'tv', 'command': 'on', 'params': {}},
        {'type': 'delay', 'seconds': 2.0},
      ],
      'on_stop': [
        {'type': 'device', 'device': 'tv', 'command': 'off', 'params': {}}
      ],
      'bindings': {
        'volume_up': {
          'on_press': [
            {'type': 'device', 'device': 'tv', 'command': 'on', 'params': {}}
          ],
          'on_repeat': [],
          'on_hold': [],
          'on_release': [],
          'hold_seconds': 0.6,
          // An explicit override for this one button.
          'repeat_delay': 0.2,
          'repeat_interval': 0.1,
          'repeat_accel': 4.0,
          'repeat_accel_seconds': 1.5,
        },
        'power': {
          'on_press': [
            {'type': 'scene', 'scene': null}
          ],
          'on_repeat': [],
          'on_hold': [],
          'on_release': [],
          'hold_seconds': 0.6,
          // No override: this button follows the config-wide default below.
          'repeat_delay': null,
          'repeat_interval': null,
          'repeat_accel': null,
          'repeat_accel_seconds': null,
        },
      },
    }
  ],
  // A reference to the scene above, not a second bindable set of its own.
  'global_scene': 'watch_tv',
  'default_scene': null,
  'default_repeat_delay': 0.5,
  'default_repeat_interval': 0.0,
  'default_repeat_accel': 1.0,
  'default_repeat_accel_seconds': 3.0,
};

void main() {
  group('serialisation', () {
    test('a full config survives a round trip unchanged', () {
      final decoded = HubConfig.fromJson(fullConfig);

      expect(decoded.toJson(), equals(fullConfig));
    });

    test('device settings and power policy are preserved', () {
      final device = HubConfig.fromJson(fullConfig).device('tv')!;

      expect(device.powerPolicy, 'leave_on');
      expect(device.config['base_url'], 'http://10.0.0.5');
      expect(device.powerOnCommand, 'on');
    });

    test('an action only emits the keys its own type uses', () {
      // The hub rejects unknown fields, so a stray `device` key on a delay
      // action would come back as a 422 rather than being ignored.
      expect(HubAction.delay(1.5).toJson().keys, equals({'type', 'seconds'}));
      expect(HubAction.scene(null).toJson().keys, equals({'type', 'scene'}));
      expect(
        HubAction.device('tv', 'on').toJson().keys,
        equals({'type', 'device', 'command', 'params'}),
      );
      expect(
        HubAction.adjust('up').toJson().keys,
        equals({'type', 'direction', 'device', 'target'}),
      );
    });

    test('an adjust action round-trips with no fallback', () {
      final action = HubAction.fromJson(HubAction.adjust('up').toJson());

      expect(action.type, 'adjust');
      expect(action.direction, 'up');
      expect(action.device, isNull);
      expect(action.target, isNull);
      expect(action.describe(), 'Turn up (follows the last device touched)');
    });

    test('an adjust action preserves its fallback device and target', () {
      final json = HubAction.adjust('down', device: 'house', target: 'light.kitchen').toJson();
      final action = HubAction.fromJson(json);

      expect(action.direction, 'down');
      expect(action.device, 'house');
      expect(action.target, 'light.kitchen');
      expect(action.describe(), contains('Turn down'));
      expect(action.describe(), contains('house'));
    });

    test('a null scene action survives, because that is the Off button', () {
      final action = HubAction.fromJson({'type': 'scene', 'scene': null});

      expect(action.toJson()['scene'], isNull);
      expect(action.describe(), contains('Stop'));
    });

    test('copies are independent of the original', () {
      final original = HubConfig.fromJson(fullConfig);
      final copy = original.copy();

      copy.scenes.first.name = 'Changed';
      copy.scenes.first.bindings['volume_up']!.onPress.clear();

      expect(original.scenes.first.name, 'Watch TV');
      expect(original.scenes.first.bindings['volume_up']!.onPress, hasLength(1));
    });
  });

  group('binding', () {
    test('an empty binding is reported as empty', () {
      expect(Binding().isEmpty, isTrue);
      expect(Binding(onRelease: [HubAction.delay(1)]).isEmpty, isFalse);
    });

    test('the summary prefers the press action', () {
      final binding = Binding(
        onPress: [HubAction.device('tv', 'on')],
        onHold: [HubAction.device('tv', 'off')],
      );

      expect(binding.summary, 'tv → on');
    });

    test('the summary falls back through the other phases', () {
      expect(Binding(onHold: [HubAction.device('tv', 'off')]).summary, startsWith('hold:'));
      expect(Binding(onRepeat: [HubAction.device('tv', 'up')]).summary, startsWith('repeat:'));
      expect(Binding().summary, 'nothing');
    });

    test('phase lookup returns the right list', () {
      final binding = Binding(onRepeat: [HubAction.delay(1)]);

      expect(binding.phase('repeat'), hasLength(1));
      expect(binding.phase('press'), isEmpty);
    });

    test('repeat timing is unset by default, following the config-wide value', () {
      // Not 0.5/0.0 -- those live on HubConfig now, and a binding that
      // never touches them must not silently carry its own copy.
      final binding = Binding();

      expect(binding.repeatDelay, isNull);
      expect(binding.repeatInterval, isNull);
      expect(binding.repeatAccel, isNull);
      expect(binding.repeatAccelSeconds, isNull);
    });

    test('repeat acceleration defaults to off on a fresh config', () {
      final config = HubConfig();

      expect(config.defaultRepeatAccel, 1.0);
      expect(config.defaultRepeatAccelSeconds, 3.0);
    });
  });

  group('settings', () {
    const fullSettings = {
      'host': '127.0.0.1',
      'port': 9000,
      'ui_dir': null,
      'config_path': 'hub_config.json',
      'buttons_path': 'buttons.json',
      'autostart': true,
      'source': 'radio',
      'replay_path': null,
      'replay_speed': 1.0,
      'replay_loop': true,
      'address': '17129BFCB6',
      'channel': 62,
      'probe_interval': 0.0,
      'allow_ack': true,
      'csn_pin': 'C0',
      'ce_pin': 'D4',
      'ir_rx_pin': 17,
      'ir_tx_pin': 18,
      'ir_pigpio_host': 'localhost',
      'ir_pigpio_port': 8888,
      'verbose': false,
      'github_updates_enabled': false,
      'github_repo': 'someone/somewhere',
      'update_check_interval_hours': 12.0,
    };

    test('settings survive a round trip unchanged', () {
      // The hub rejects unknown keys, so a field dropped here would be a 422
      // on every save rather than a silently lost setting.
      expect(HubSettings.fromJson(fullSettings).toJson(), equals(fullSettings));
    });

    test('only bind settings need the process restarted', () {
      final live = HubSettings();

      expect((live.copy()..port = 9000).needsProcessRestart(live), isTrue);
      expect((live.copy()..host = '127.0.0.1').needsProcessRestart(live), isTrue);
      // Everything else applies by restarting the hub, which the page survives.
      expect((live.copy()..source = 'radio').needsProcessRestart(live), isFalse);
    });

    test('runtime status reads the state the screens branch on', () {
      final status = RuntimeStatus.fromJson({
        'state': 'failed',
        'detail': 'no address',
        'problems': ['Source is \'radio\' but no remote address is set.'],
        'host': '0.0.0.0',
        'port': 8765,
      });

      expect(status.isFailed, isTrue);
      expect(status.isRunning, isFalse);
      expect(status.problems, hasLength(1));
    });

    test('a state with no hub block still reads as running', () {
      // A hub predating this field must not be read as "stopped", which
      // would leave the app disabling itself against a healthy hub.
      final status = HubStatus.fromJson(const {
        'active_scene': null,
        'scenes': [],
        'devices': [],
        'button_count': 0,
      });

      expect(status.hub.isRunning, isTrue);
    });
  });

  group('slugify', () {
    test('produces ids the hub will accept', () {
      // The hub constrains ids to ^[a-z0-9_]+$; anything else is a 422.
      expect(slugify('Watch TV'), 'watch_tv');
      expect(slugify('  Movie Night!  '), 'movie_night');
      expect(slugify('Küche & Bad'), 'k_che_bad');
    });

    test('never returns an empty id', () {
      expect(slugify('!!!'), 'item');
    });
  });
}
