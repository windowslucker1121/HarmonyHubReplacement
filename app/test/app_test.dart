/// Widget tests driving the whole app against a fake hub.
///
/// Serving the built files proves nothing about whether the app runs: a
/// Flutter web build can be delivered perfectly and still throw on its first
/// frame. These build the real widget tree, so a broken screen fails here
/// instead of in a browser.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:harmony_hub_app/api/client.dart';
import 'package:harmony_hub_app/api/config.dart';
import 'package:harmony_hub_app/api/models.dart';
import 'package:harmony_hub_app/api/settings.dart';
import 'package:harmony_hub_app/main.dart';
import 'package:harmony_hub_app/screens/live_screen.dart';
import 'package:harmony_hub_app/screens/remote_mapper.dart';
import 'package:harmony_hub_app/state/hub_store.dart';
import 'package:harmony_hub_app/state/prefs_backend.dart';
import 'package:harmony_hub_app/state/ui_prefs.dart';
import 'package:harmony_hub_app/widgets/remote_diagram.dart';

class FakeApi extends HubApi {
  FakeApi({this.activeScene, this.hubState = 'running', this.focus}) : super(base: 'http://fake');

  String? activeScene;

  /// What `status()` reports the SmartHome +/- keys as following. `null`
  /// (the default) means nothing has been touched yet.
  FocusInfo? focus;

  /// running | stopped | failed. The app has to stay usable in all three,
  /// which is what the runtime layer on the hub side exists to allow.
  String hubState;

  final StreamController<HubEvent> eventController = StreamController.broadcast();
  final List<String> simulated = [];
  final List<String> paired = [];
  final List<String> lifecycle = [];
  HubConfig saved = _config();
  HubSettings savedSettings = HubSettings(source: 'none');
  bool restartedOnSave = false;

  static HubConfig _config() => HubConfig.fromJson({
        'version': 1,
        'devices': [
          {'id': 'tv', 'name': 'Living Room TV', 'backend': 'virtual', 'config': {}},
          {'id': 'shield', 'name': 'Shield', 'backend': 'androidtv', 'config': {'host': '10.0.0.5'}},
          {
            'id': 'house',
            'name': 'House',
            'backend': 'homeassistant',
            'config': {'url': 'http://ha.local:8123', 'entities': ['light.kitchen']}
          },
          {
            'id': 'avr',
            'name': 'Receiver',
            'backend': 'denon',
            'config': {'host': '10.0.0.7', 'transport': 'http'}
          },
          {'id': 'lgtv', 'name': 'Lounge TV', 'backend': 'lgtv', 'config': {'host': '10.0.0.9'}},
        ],
        'scenes': [
          {
            'id': 'watch_tv',
            'name': 'Watch TV',
            'devices': ['tv'],
            'on_start': [
              {'type': 'device', 'device': 'tv', 'command': 'power_on', 'params': {}}
            ],
            'on_stop': [],
            'bindings': {
              'volume_up': {
                'on_press': [
                  {'type': 'device', 'device': 'tv', 'command': 'volume_up', 'params': {}}
                ],
                'on_repeat': [],
                'on_hold': [],
                'on_release': [],
                'hold_seconds': 0.6,
              }
            },
          },
          // An ordinary scene, referenced as the global fallback below --
          // "global" is not a separate bindable set, just a pointer to one
          // of these.
          {
            'id': 'standby',
            'name': 'Standby',
            'devices': [],
            'on_start': [],
            'on_stop': [],
            'bindings': {
              'ac_back': {
                'on_press': [
                  {'type': 'device', 'device': 'tv', 'command': 'power_on', 'params': {}}
                ],
                'on_repeat': [],
                'on_hold': [],
                'on_release': [],
                'hold_seconds': 0.6,
              }
            },
          },
        ],
        'global_scene': 'standby',
      });

  @override
  Future<HubStatus> status() async => HubStatus(
        activeScene: activeScene,
        scenes: [
          SceneSummary(id: 'watch_tv', name: 'Watch TV', devices: ['tv'], boundButtons: 1)
        ],
        devices: [
          DeviceStatus(
            id: 'tv', name: 'Living Room TV', backend: 'virtual',
            running: true, ok: true, detail: 'ready',
          ),
          DeviceStatus(
            id: 'shield', name: 'Shield', backend: 'androidtv',
            running: true, ok: false, detail: 'not paired -- pair this device to use it',
          ),
          DeviceStatus(
            id: 'house', name: 'House', backend: 'homeassistant',
            running: true, ok: true, detail: 'Ravenswood · HA 2026.8.1 · 1 entity',
          ),
          DeviceStatus(
            id: 'avr', name: 'Receiver', backend: 'denon',
            running: true, ok: true, detail: 'on · Blu-ray · 1 input',
          ),
          DeviceStatus(
            id: 'lgtv', name: 'Lounge TV', backend: 'lgtv',
            running: true, ok: false, detail: 'not paired -- pair this device to use it',
          ),
        ],
        buttonCount: 4,
        hub: RuntimeStatus(
          state: hubState,
          detail: hubState == 'failed' ? "source is 'radio' but no remote address is set" : '',
          source: 'Simulated presses only',
          host: '0.0.0.0',
          port: 8765,
          problems: hubState == 'failed' ? const ["Source is 'radio' but no remote address is set."] : const [],
        ),
        focus: focus,
        paused: paused,
      );

  /// Whether button presses are logged but not acted on -- mutable so tests
  /// can flip it via [pauseHub]/[resumeHub] the same way the real API does.
  bool paused = false;

  @override
  Future<void> pauseHub() async => paused = true;

  @override
  Future<void> resumeHub() async => paused = false;

  @override
  Future<HubSettings> settings() async => savedSettings;

  @override
  Future<RuntimeStatus> saveSettings(HubSettings settings, {bool restart = false}) async {
    savedSettings = settings;
    restartedOnSave = restart;
    if (restart) hubState = 'running';
    return RuntimeStatus(state: hubState, host: '0.0.0.0', port: 8765);
  }

  @override
  Future<RuntimeStatus> startHub() async {
    lifecycle.add('start');
    hubState = 'running';
    return RuntimeStatus(state: hubState, host: '0.0.0.0', port: 8765);
  }

  @override
  Future<RuntimeStatus> stopHub() async {
    lifecycle.add('stop');
    hubState = 'stopped';
    return RuntimeStatus(state: hubState, host: '0.0.0.0', port: 8765);
  }

  @override
  Future<RuntimeStatus> restartHub() async {
    lifecycle.add('restart');
    hubState = 'running';
    return RuntimeStatus(state: hubState, host: '0.0.0.0', port: 8765);
  }

  /// `null` means an ordinary (non-deployed) hub -- the default, so most
  /// tests never see the Software card at all, same as a real dev checkout.
  VersionInfo? fakeVersion;
  List<UpdateHistoryEntry> updateHistoryEntries = [];
  bool rolledBack = false;

  @override
  Future<VersionInfo> version() async => fakeVersion ?? VersionInfo(deployed: false, updatesEnabled: true);

  @override
  Future<List<UpdateHistoryEntry>> updateHistory() async => updateHistoryEntries;

  @override
  Future<UpdateResult> rollbackUpdate() async {
    rolledBack = true;
    return UpdateResult(buildId: fakeVersion?.previous, restarting: true);
  }

  /// `null` means no release has been found -- most tests never see the
  /// GitHub side of the Software card at all, same as a hub that has not
  /// checked yet.
  UpdateCheckInfo? fakeAvailableUpdate;
  bool checkedForUpdate = false;
  final List<bool> installRequests = [];

  /// Set to make the first (unforced) [installUpdate] behave like the real
  /// route's 409 when a scene is active -- a later, forced call still
  /// succeeds, the same way the real one does.
  bool refuseInstallWithoutForce = false;

  @override
  Future<UpdateCheckInfo> availableUpdate() async => fakeAvailableUpdate ?? UpdateCheckInfo();

  @override
  Future<UpdateCheckInfo> checkForUpdate() async {
    checkedForUpdate = true;
    return fakeAvailableUpdate ?? UpdateCheckInfo();
  }

  @override
  Future<GithubInstallResult> installUpdate({bool force = false}) async {
    installRequests.add(force);
    if (refuseInstallWithoutForce && !force) {
      throw HubApiException(409, "a scene is active -- retry with ?force=true, or wait until it's idle");
    }
    return GithubInstallResult(buildId: fakeAvailableUpdate?.available?.buildId ?? '', started: true);
  }

  @override
  Future<List<HubCheck>> checks() async => [
        HubCheck(name: 'Configuration file', ok: true, detail: '2 device(s), 2 scene(s)'),
        HubCheck(name: 'Web interface', ok: false, detail: 'no built web UI found'),
      ];

  @override
  Future<List<HubCheck>> trySettings(HubSettings settings) async => [
        HubCheck(
          name: 'Event source',
          ok: settings.source != 'radio' || (settings.address?.isNotEmpty ?? false),
          detail: 'checked ${settings.source}',
        ),
      ];

  /// Every `method` a test passed to [startDiscovery], in call order --
  /// what proves the chooser dialog actually reached the API layer.
  final List<String> discoveryMethods = [];
  DiscoveryStatus discoveryStatusResult = DiscoveryStatus(state: 'idle');

  @override
  Future<DiscoveryStatus> startDiscovery({String method = 'hub'}) async {
    discoveryMethods.add(method);
    discoveryStatusResult = DiscoveryStatus(
      state: 'running',
      method: method,
      detail: method == 'sniff'
          ? 'No Hub needed -- press and release buttons on the remote repeatedly.'
          : 'Put the Harmony Hub into pairing mode (press its pair/reset button).',
    );
    return discoveryStatusResult;
  }

  @override
  Future<DiscoveryStatus> discoveryStatus() async => discoveryStatusResult;

  @override
  Future<DiscoveryStatus> cancelDiscovery() async {
    discoveryStatusResult = DiscoveryStatus(state: 'cancelled', method: discoveryStatusResult.method);
    return discoveryStatusResult;
  }

  /// What the hub knows the remote can send. Mutable so the learning tests
  /// can start from an empty map, the way a fresh install does -- and the
  /// signature lists are growable, because one button legitimately collects
  /// more than one.
  List<ButtonInfo> known = [
    ButtonInfo(key: 'volume_up', label: 'Volume Up', signatures: ['C3E90000']),
    ButtonInfo(key: 'mute', label: 'Mute', signatures: ['C3E20000']),
    ButtonInfo(key: 'left_arrow', label: 'Left Arrow', signatures: ['C1500000']),
    ButtonInfo(key: 'ac_back', label: 'AC Back', signatures: ['C3240000']),
  ];

  @override
  Future<List<ButtonInfo>> learnButtons(List<ButtonInfo> learned) async {
    for (final button in learned) {
      final existing = known.where((b) => b.key == button.key).firstOrNull;
      if (existing == null) {
        known.add(button);
      } else {
        existing.signatures.addAll(button.signatures);
      }
    }
    return known;
  }

  @override
  Future<List<ButtonInfo>> forgetButton(String key) async {
    known.removeWhere((b) => b.key == key);
    return known;
  }

  @override
  Future<List<ButtonInfo>> buttons() async => known;

  @override
  Future<List<BackendInfo>> backends() async => [
        BackendInfo(
          name: 'virtual', label: 'Virtual device', description: 'Records commands.',
          configSchema: const {'type': 'object', 'properties': {}},
        ),
        BackendInfo(
          name: 'androidtv', label: 'Android TV / Google TV', description: 'Talks to a Shield.',
          configSchema: const {
            'type': 'object',
            'properties': {
              'host': {'type': 'string', 'title': 'Address'}
            }
          },
          pairable: true,
          // The real hub always sends a label here -- Android TV's own
          // default is "Code" -- so the fake matches that rather than
          // leaning on the empty-string default, which now means "nothing
          // to type back" (see the LG webOS pairing test below).
          pairInputLabel: 'Code',
          discoverable: true,
          discoverField: 'host',
        ),
        BackendInfo(
          name: 'homeassistant', label: 'Home Assistant', description: 'Lights and scenes.',
          configSchema: const {
            'type': 'object',
            'properties': {
              'url': {'type': 'string', 'title': 'Address'},
              'entities': {'type': 'array', 'title': 'Exposed entities'}
            }
          },
          pairable: true,
          pairLabel: 'Connect to Home Assistant',
          pairHint: 'Home Assistant issues a token instead of showing a code.',
          pairInputLabel: 'Long-lived access token',
          pairInputMultiline: true,
          discoverable: true,
          discoverField: 'url',
        ),
        BackendInfo(
          name: 'denon', label: 'Denon / Marantz AV receiver', description: 'An AV receiver.',
          configSchema: const {
            'type': 'object',
            'properties': {
              'host': {'type': 'string', 'title': 'Address'},
              'transport': {
                'type': 'string',
                'title': 'How to reach it',
                'enum': ['http', 'telnet'],
                'default': 'http',
              },
              'entities': {'type': 'array', 'title': 'Inputs'}
            }
          },
          discoverable: true,
          discoverField: 'host',
        ),
        BackendInfo(
          name: 'lgtv', label: 'LG webOS TV', description: 'An LG TV running webOS.',
          configSchema: const {
            'type': 'object',
            'properties': {
              'host': {'type': 'string', 'title': 'Address'}
            }
          },
          pairable: true,
          pairLabel: 'Pair this TV',
          pairHint: "Accept the connection prompt shown on the TV, using the TV's own remote.",
          // Empty on purpose: webOS asks for a press on the TV, not
          // something typed back here. Exercises the confirm-only branch of
          // DevicesScreen._pair rather than the androidtv/homeassistant
          // text-field branch above.
          pairInputLabel: '',
          discoverable: true,
          discoverField: 'host',
        ),
      ];

  @override
  Future<List<DiscoveredDevice>> discover(String backend) async =>
      discoverResults[backend] ?? [];

  /// What [discover] hands back per backend, settable per test.
  final Map<String, List<DiscoveredDevice>> discoverResults = {};

  @override
  Future<List<EntityInfo>> deviceEntities(String deviceId, {bool controllableOnly = true}) async {
    entityCalls.add(deviceId);
    return [
      EntityInfo(entityId: 'light.kitchen', name: 'Kitchen', domain: 'light', state: 'on'),
      EntityInfo(entityId: 'light.sofa', name: 'Sofa Lamp', domain: 'light', state: 'off'),
      EntityInfo(entityId: 'scene.movie_night', name: 'Movie Night', domain: 'scene'),
    ];
  }

  final List<String> entityCalls = [];

  @override
  Future<HubConfig> config() async => saved;

  @override
  Future<HubConfig> saveConfig(HubConfig config, {bool force = false}) async => saved = config;

  @override
  Future<List<CommandInfo>> deviceCommands(String deviceId) async => deviceId == 'shield'
      ? [
          CommandInfo(name: 'volume_up', label: 'Volume up', description: '', repeatable: true),
          CommandInfo(name: 'back', label: 'Back', description: ''),
          CommandInfo(name: 'dpad_left', label: 'Left', description: '', repeatable: true),
          CommandInfo(name: 'dpad_right', label: 'Right', description: '', repeatable: true),
          CommandInfo(
            name: 'select',
            label: 'Select',
            description: '',
            params: {
              'type': 'object',
              'properties': {
                'direction': {
                  'type': 'string',
                  'title': 'Direction',
                  'enum': ['SHORT', 'START_LONG', 'END_LONG'],
                  'default': 'SHORT',
                },
              },
            },
          ),
          CommandInfo(
            name: 'launch_app',
            label: 'Launch app',
            description: '',
            params: {
              'type': 'object',
              'required': ['app'],
              'properties': {
                'app': {'type': 'string', 'title': 'App link or package name'},
              },
            },
          ),
        ]
      : [CommandInfo(name: 'power_on', label: 'Power On', description: '')];

  @override
  Future<({Map<String, String> bindings, Map<String, String> adjust})> suggestedBindings(
    String deviceId,
  ) async {
    if (deviceId == 'shield') {
      return (
        bindings: {'volume_up': 'volume_up', 'ac_back': 'back', 'left_arrow': 'dpad_left'},
        adjust: const <String, String>{},
      );
    }
    if (deviceId == 'house') {
      // Mirrors the real Home Assistant backend: nothing exposed can be
      // toggled by a fixed command suggestion, but the kitchen light can be
      // stepped, so the +/- keys are suggested instead.
      return (bindings: const <String, String>{}, adjust: {'consumer_0x0ff0': 'up', 'consumer_0x0ff1': 'down'});
    }
    return (bindings: const <String, String>{}, adjust: const <String, String>{});
  }

  @override
  Future<({bool ok, String detail})> pairStart(String deviceId) async {
    paired.add('start');
    if (deviceId == 'lgtv') {
      // Deliberately different wording from the lgtv BackendInfo's own
      // pairHint below -- that one stays on the page, this one is what the
      // confirm dialog shows, and a test asserting on one should not
      // accidentally pass because the other happened to say the same thing.
      return (ok: true, detail: 'A connection prompt just appeared on the TV -- accept it now.');
    }
    return (ok: true, detail: 'Enter the six-digit code shown on the device.');
  }

  @override
  Future<({bool ok, String detail})> pairFinish(String deviceId, String code) async {
    paired.add(code);
    return (ok: true, detail: 'paired');
  }

  @override
  Future<void> activateScene(String sceneId) async {
    activeScene = sceneId;
    eventController.add(HubEvent(type: 'scene', at: DateTime.now(), scene: sceneId, ok: true));
  }

  @override
  Future<void> stopScene() async {
    activeScene = null;
    eventController.add(HubEvent(type: 'scene', at: DateTime.now(), ok: true));
  }

  @override
  Future<void> simulate(String key, {String kind = 'press'}) async {
    simulated.add('$key/$kind');
    eventController.add(
      HubEvent(type: 'button', at: DateTime.now(), button: key, label: key, phase: kind),
    );
  }

  @override
  Stream<HubEvent> events() => eventController.stream;

  @override
  void close() => eventController.close();
}

/// Pumps the app and waits for the initial load to settle. [prefs] lets a
/// test seed device-local UI state (last tab, filters, ...) up front --
/// omitted, the app gets a fresh in-memory store that persists nothing.
Future<HubStore> pumpApp(WidgetTester tester, FakeApi api, {UiPrefs? prefs}) async {
  final store = HubStore(api: api, prefs: prefs);
  await tester.pumpWidget(HarmonyHubApp(store: store, prefs: prefs));
  await tester.pumpAndSettle();
  return store;
}

void main() {
  testWidgets('the live screen lists buttons with what they are bound to', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'));

    // The remote diagram shows icons rather than labels, but each button
    // carries its label + resolved binding as a tooltip -- resolved through
    // the active scene, which is what will actually happen.
    expect(find.byTooltip('Volume Up — tv → volume_up'), findsOneWidget);
    expect(find.byTooltip('Mute — unbound'), findsOneWidget); // Mute is not bound anywhere
  });

  testWidgets('the active scene is shown, and stopping it is offered', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'));

    expect(find.text('Watch TV'), findsWidgets);
    expect(find.text('Stop'), findsOneWidget);
  });

  testWidgets('with nothing touched yet, the SmartHome keys say so', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'));

    expect(find.text('SmartHome +/- keys: nothing touched yet'), findsOneWidget);
  });

  testWidgets('once something is focused, the SmartHome keys say what', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(
      tester,
      FakeApi(
        activeScene: 'watch_tv',
        focus: FocusInfo(device: 'house', target: 'light.kitchen', label: 'Kitchen', canAdjust: true),
      ),
    );

    expect(find.text('SmartHome +/- keys follow Kitchen'), findsOneWidget);
  });

  testWidgets('a focused target that cannot be stepped says so', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(
      tester,
      FakeApi(
        activeScene: 'watch_tv',
        focus: FocusInfo(device: 'house', target: 'switch.amp', label: 'Amplifier', canAdjust: false),
      ),
    );

    expect(find.text('SmartHome +/- keys follow Amplifier (nothing to turn up or down)'), findsOneWidget);
  });

  testWidgets('with no scene running it says so', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());

    expect(find.text('No scene running'), findsOneWidget);
    expect(find.text('Only the global scene\'s bindings are in effect'), findsOneWidget);
  });

  testWidgets('not paused, the live view shows no pause banner', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());

    expect(find.text('Paused'), findsNothing);
    expect(find.widgetWithText(FilledButton, 'Resume'), findsNothing);
  });

  testWidgets('paused, the live view leads with a banner and a Resume button', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi()..paused = true);

    expect(find.text('Paused'), findsOneWidget);
    expect(
      find.text('Button presses are logged but nothing is being sent to a device.'),
      findsOneWidget,
    );
    expect(find.widgetWithText(FilledButton, 'Resume'), findsOneWidget);
  });

  testWidgets('tapping Resume on the banner clears the paused state', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi()..paused = true;
    await pumpApp(tester, api);
    expect(find.text('Paused'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Resume'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));

    expect(api.paused, isFalse);
    expect(find.text('Paused'), findsNothing);
  });

  testWidgets('starting and stopping a scene does not clear the paused banner', (tester) async {
    // Regression: the local status patch on a "scene" event used to rebuild
    // HubStatus without carrying `paused` along, silently resetting it to
    // the constructor's default of false -- so the banner would vanish (and
    // the Settings toggle would show off) even though the engine itself was
    // never actually resumed.
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi()..paused = true;
    await pumpApp(tester, api);
    expect(find.text('Paused'), findsOneWidget);

    api.eventController.add(HubEvent(type: 'scene', at: DateTime.now(), scene: 'watch_tv', ok: true));
    await tester.pump();
    expect(find.text('Paused'), findsOneWidget);

    api.eventController.add(HubEvent(type: 'scene', at: DateTime.now(), ok: true));
    await tester.pump();
    expect(find.text('Paused'), findsOneWidget);

    // The API itself was never asked to resume -- the engine was paused
    // throughout, only the display was ever wrong.
    expect(api.paused, isTrue);
  });

  testWidgets('with no global scene set, idle buttons say they do nothing', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    // `api.saved` and `store.config` are the same object, so this is visible
    // as soon as the live screen next builds -- no save round trip needed.
    api.saved.globalScene = null;
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();
    // Ambiguous while selected: the rail crossfades between a resting and a
    // selected label, so two "Live" texts briefly coexist.
    await tester.tap(find.text('Live').first);
    await tester.pumpAndSettle();

    expect(find.text('No global scene set — unbound buttons do nothing'), findsOneWidget);
  });

  testWidgets('tapping a button sends a press and a release', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi(activeScene: 'watch_tv');
    await pumpApp(tester, api);

    await tester.tap(find.byTooltip('Volume Up — tv → volume_up'));
    await tester.pumpAndSettle();

    // Press alone would leave the engine believing the button is still held.
    expect(api.simulated, equals(['volume_up/press', 'volume_up/release']));
  });

  // ------------------------------------------------------------------
  // Maximized remote
  // ------------------------------------------------------------------

  testWidgets('maximizing shows the remote and the scene, and nothing else', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'));

    await tester.tap(find.byTooltip('Maximize the remote'));
    await tester.pumpAndSettle();

    expect(find.byType(MaximizedRemotePage), findsOneWidget);
    // Exactly one remote diagram is live -- the shell, the activity log
    // and the navigation are all off-screen behind the pushed route.
    expect(find.byType(RemoteDiagram), findsOneWidget);
    expect(find.text('Watch TV'), findsOneWidget);
    expect(find.text('Activity'), findsNothing);
    expect(find.byType(NavigationRail), findsNothing);
  });

  testWidgets('the maximized remote still sends', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi(activeScene: 'watch_tv');
    await pumpApp(tester, api);

    await tester.tap(find.byTooltip('Maximize the remote'));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Volume Up — tv → volume_up'));
    await tester.pumpAndSettle();

    // Same store, same `tap` -- a press and a release actually reached the
    // hub, not a stub that only redraws.
    expect(api.simulated, equals(['volume_up/press', 'volume_up/release']));
  });

  testWidgets('the maximized remote follows the hub', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi(activeScene: 'watch_tv');
    await pumpApp(tester, api);

    await tester.tap(find.byTooltip('Maximize the remote'));
    await tester.pumpAndSettle();

    expect(find.text('Watch TV'), findsOneWidget);

    // The page reads HubScope directly, so an event arriving after the push
    // must still reach it -- proof the maximized view is not a frozen copy.
    api.eventController.add(HubEvent(type: 'scene', at: DateTime.now(), scene: null, ok: true));
    await tester.pumpAndSettle();

    expect(find.text('No scene running'), findsOneWidget);
  });

  testWidgets('exiting the maximized remote returns to the live view', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'));

    await tester.tap(find.byTooltip('Maximize the remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Exit full screen'));
    await tester.pumpAndSettle();

    expect(find.byType(MaximizedRemotePage), findsNothing);
    expect(find.text('Activity'), findsOneWidget);
  });

  testWidgets('the maximized remote fits a phone screen', (tester) async {
    // Same reasoning as "the mapper fits a phone screen" below: a widget
    // test fails on overflow, which is the assertion here. The maximized
    // page is where the compact scene strip and the wider remote board are
    // most likely to collide with a narrow viewport.
    tester.view.physicalSize = const Size(420, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'));

    await tester.tap(find.byTooltip('Maximize the remote'));
    await tester.pumpAndSettle();

    expect(find.byType(MaximizedRemotePage), findsOneWidget);
  });

  testWidgets('a reload while maximized comes back up maximized', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    // Seeded as already-maximized -- the same shape a real reload leaves
    // behind, since `openMaximizedRemote` sets this the moment it opens.
    await pumpApp(
      tester,
      FakeApi(activeScene: 'watch_tv'),
      prefs: UiPrefs.memory({'shell.tab': 'live', 'live.maximized': true}),
    );

    // No tap: the page opens itself, the same way a remembered tab does.
    expect(find.byType(MaximizedRemotePage), findsOneWidget);
  });

  testWidgets('exiting the maximized remote forgets it was open', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final prefs = UiPrefs.memory();
    await pumpApp(tester, FakeApi(activeScene: 'watch_tv'), prefs: prefs);

    await tester.tap(find.byTooltip('Maximize the remote'));
    await tester.pumpAndSettle();
    expect(prefs.get(kRemoteMaximized), isTrue);

    await tester.tap(find.byTooltip('Exit full screen'));
    await tester.pumpAndSettle();

    // So the *next* reload -- not this session -- lands back on the plain
    // live view instead of snapping straight back to full screen.
    expect(prefs.get(kRemoteMaximized), isFalse);
  });

  // ------------------------------------------------------------------
  // Activity filter
  //
  // The dimensions on offer come from whatever fields the events flowing
  // through actually carry, not a hard-coded list -- these tests lean on an
  // event type the app has never heard of to prove that.
  // ------------------------------------------------------------------

  testWidgets('hiding an event type in the filter drops it from the log', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);

    api.eventController.add(HubEvent(type: 'button', at: DateTime.now(), button: 'mute', phase: 'press'));
    api.eventController.add(HubEvent(type: 'scene', at: DateTime.now(), scene: 'watch_tv', ok: true));
    await tester.pumpAndSettle();
    await tester.pump(kFlashDuration);

    await tester.tap(find.byTooltip('Filter & sort'));
    await tester.pumpAndSettle();

    // "Button" is a value chip under the "Event type" dimension, discovered
    // from the events above rather than declared anywhere.
    await tester.tap(find.widgetWithText(FilterChip, 'Button'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Done'));
    await tester.pumpAndSettle();

    expect(find.text('1 of 2'), findsOneWidget);
  });

  testWidgets('a filter can be reset back to showing everything', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);

    api.eventController.add(HubEvent(type: 'button', at: DateTime.now(), button: 'mute', phase: 'press'));
    api.eventController.add(HubEvent(type: 'scene', at: DateTime.now(), scene: 'watch_tv', ok: true));
    await tester.pumpAndSettle();
    await tester.pump(kFlashDuration);

    await tester.tap(find.byTooltip('Filter & sort'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilterChip, 'Button'));
    await tester.pumpAndSettle();
    expect(find.text('Reset'), findsOneWidget);

    await tester.tap(find.text('Reset'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Done'));
    await tester.pumpAndSettle();

    expect(find.text('1 of 2'), findsNothing);
    expect(find.byTooltip('Filter & sort'), findsOneWidget);
  });

  testWidgets('an event type nobody configured shows up as a filter option on its own',
      (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);

    // A hub-side type this Dart code has never been told about.
    api.eventController.add(HubEvent(type: 'macro', at: DateTime.now(), detail: 'Ran bedtime macro'));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Filter & sort'));
    await tester.pumpAndSettle();

    expect(find.widgetWithText(FilterChip, 'Macro'), findsOneWidget);
  });

  testWidgets('the scenes screen lists scenes and can start one', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);

    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();

    expect(find.text('Global scene'), findsOneWidget);
    // Scoped to the Watch TV card: Standby has its own Start button too.
    await tester.tap(find.descendant(
      of: find.widgetWithText(Card, 'Watch TV'),
      matching: find.text('Start'),
    ));
    await tester.pumpAndSettle();

    expect(api.activeScene, 'watch_tv');
  });

  testWidgets('the scenes screen can be searched by scene, device or setting', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());

    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();

    // A scene name narrows to that scene, and hides the two config cards
    // along with it since neither matches "watch".
    await tester.enterText(find.byType(TextFormField), 'watch');
    await tester.pumpAndSettle();
    expect(find.text('Watch TV'), findsOneWidget);
    expect(find.text('Standby'), findsNothing);
    expect(find.text('Global scene'), findsNothing);
    expect(find.text('Default repeat timing'), findsNothing);

    // A scene is also found by the resolved name of a device it drives,
    // not just its own name/id -- Watch TV drives "tv" (Living Room TV).
    await tester.enterText(find.byType(TextFormField), 'living room');
    await tester.pumpAndSettle();
    expect(find.text('Watch TV'), findsOneWidget);
    expect(find.text('Standby'), findsNothing);

    // A query matching only a config card's own keywords hides every scene
    // and the other config card.
    await tester.enterText(find.byType(TextFormField), 'repeat');
    await tester.pumpAndSettle();
    expect(find.text('Default repeat timing'), findsOneWidget);
    expect(find.text('Global scene'), findsNothing);
    expect(find.text('Watch TV'), findsNothing);
    expect(find.text('Standby'), findsNothing);
    // "Scenes" the section header is gone; "Scenes" the nav destination
    // label stays, so this checks the card grid specifically.
    expect(find.byType(Card), findsOneWidget);

    await tester.enterText(find.byType(TextFormField), 'nonexistent-scene');
    await tester.pumpAndSettle();
    expect(find.textContaining('Nothing matches'), findsOneWidget);

    await tester.tap(find.text('Clear search'));
    await tester.pumpAndSettle();
    expect(find.text('Watch TV'), findsOneWidget);
    expect(find.text('Standby'), findsOneWidget);
    expect(find.text('Global scene'), findsOneWidget);
  });

  /// Opens the four-phase editor for Volume Up in the Watch TV scene.
  Future<void> openBindingEditor(WidgetTester tester, FakeApi api) async {
    tester.view.physicalSize = const Size(1400, 1800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, api);
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Watch TV'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Button bindings'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Volume Up — tv → volume_up'));
    await tester.pumpAndSettle();
  }

  testWidgets('repeat timing is hidden when nothing repeats', (tester) async {
    // A slider that changes nothing is just another thing to wonder about,
    // which is why the hold threshold is conditional too.
    await openBindingEditor(tester, FakeApi());

    expect(find.text('On press'), findsOneWidget);
    expect(find.text('Repeat timing'), findsNothing);
  });

  testWidgets('a repeating button follows the remote-wide default until customised', (tester) async {
    // The remote reports a held button every ~100ms and never says how long
    // it has been down, so an ordinary short press arrives looking exactly
    // like a hold. The wait is what tells them apart -- and one setting for
    // the whole remote beats tuning every button that happens to repeat.
    final api = FakeApi();
    api.saved.scenes.first.bindings['volume_up']!.onRepeat = [
      HubAction.device('tv', 'volume_up')
    ];

    await openBindingEditor(tester, api);

    expect(find.text('Repeat timing'), findsOneWidget);
    expect(find.text('Custom timing for this button'), findsOneWidget);
    // No sliders yet: this button has not asked to diverge from the default.
    expect(find.text('Wait before repeating'), findsNothing);
    expect(
      find.textContaining('Follows the remote-wide default: waits 0.5s before repeating.'),
      findsOneWidget,
    );

    await tester.tap(find.text('Custom timing for this button'));
    await tester.pumpAndSettle();

    // Turning customisation on starts from the default rather than zero, so
    // the switch does not itself change what the button does.
    expect(find.text('Wait before repeating'), findsOneWidget);
    expect(find.text('A press shorter than 0.5s does not repeat at all.'), findsOneWidget);

    final delaySlider = find.byType(Slider).first;
    await tester.ensureVisible(delaySlider);
    await tester.pumpAndSettle();
    // Tapping the track is how a Slider actually moves; dragging it by an
    // offset leaves the thumb where it was.
    final track = tester.getRect(delaySlider);
    await tester.tapAt(Offset(track.left + 4, track.center.dy));
    await tester.pumpAndSettle();

    expect(find.textContaining('Repeats from the very first packet'), findsOneWidget);

    // Three pops to actually persist: the button editor, the bindings
    // picture, then the scene editor's own Save -- which is what finally
    // calls through to the hub.
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.scenes.first.bindings['volume_up']!.repeatDelay, 0.0);
  });

  testWidgets('a custom repeat timing can turn on acceleration for one button', (tester) async {
    final api = FakeApi();
    api.saved.scenes.first.bindings['volume_up']!.onRepeat = [
      HubAction.device('tv', 'volume_up')
    ];

    await openBindingEditor(tester, api);

    await tester.tap(find.text('Custom timing for this button'));
    await tester.pumpAndSettle();

    // Acceleration starts off, so its second slider has nothing to show.
    expect(find.text('Time to reach full speed'), findsNothing);

    final accelSlider = find.byType(Slider).at(2);
    await tester.ensureVisible(accelSlider);
    await tester.pumpAndSettle();
    final track = tester.getRect(accelSlider);
    await tester.tapAt(Offset(track.right - 4, track.center.dy));
    await tester.pumpAndSettle();

    expect(find.text('Time to reach full speed'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.scenes.first.bindings['volume_up']!.repeatAccel, greaterThan(1));
  });

  testWidgets('turning custom timing back off clears the override', (tester) async {
    final api = FakeApi();
    api.saved.scenes.first.bindings['volume_up']!.onRepeat = [
      HubAction.device('tv', 'volume_up')
    ];
    api.saved.scenes.first.bindings['volume_up']!.repeatDelay = 1.5;
    api.saved.scenes.first.bindings['volume_up']!.repeatInterval = 0.3;

    await openBindingEditor(tester, api);

    // A button that already has its own values opens with the switch on.
    expect(find.text('Wait before repeating'), findsOneWidget);

    await tester.tap(find.text('Custom timing for this button'));
    await tester.pumpAndSettle();
    expect(find.text('Wait before repeating'), findsNothing);

    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    final saved = api.saved.scenes.first.bindings['volume_up']!;
    expect(saved.repeatDelay, isNull);
    expect(saved.repeatInterval, isNull);
    expect(saved.repeatAccel, isNull);
    expect(saved.repeatAccelSeconds, isNull);
  });

  testWidgets('an action can be set to follow whatever is focused', (tester) async {
    final api = FakeApi();
    await openBindingEditor(tester, api);

    await tester.tap(find.text('Add').first); // "On press" is the first phase card
    await tester.pumpAndSettle();

    await tester.tap(find.text('Adjust'));
    await tester.pumpAndSettle();
    // Defaults to "Up"; picking "Down" is one tap, same as any other field.
    await tester.tap(find.text('Down'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    // The new action shows up in the "On press" list immediately.
    expect(find.textContaining('Turn down (follows the last device touched)'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    final action = api.saved.scenes.first.bindings['volume_up']!.onPress.last;
    expect(action.type, 'adjust');
    expect(action.direction, 'down');
  });

  testWidgets('a command with parameters offers a form for them', (tester) async {
    final api = FakeApi();
    await openBindingEditor(tester, api);

    await tester.tap(find.text('Add').first); // "On press" is the first phase card
    await tester.pumpAndSettle();

    // Switch to a device whose commands carry parameters.
    await tester.tap(find.byType(DropdownButtonFormField<String>).first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Shield  (androidtv)').last);
    await tester.pumpAndSettle();

    await tester.tap(find.byType(DropdownButtonFormField<String>).last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Select').last);
    await tester.pumpAndSettle();

    // The direction parameter defaults to SHORT, and can be changed --
    // this is what lets a button's press/release pair produce a real
    // Android long press instead of only ever tapping.
    expect(find.text('SHORT'), findsOneWidget);
    await tester.tap(find.byType(DropdownButtonFormField<String>).last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('START_LONG').last);
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    final action = api.saved.scenes.first.bindings['volume_up']!.onPress.last;
    expect(action.device, 'shield');
    expect(action.command, 'select');
    expect(action.params, {'direction': 'START_LONG'});
  });

  testWidgets('a command with a required parameter blocks Save until it is filled', (tester) async {
    final api = FakeApi();
    await openBindingEditor(tester, api);

    await tester.tap(find.text('Add').first);
    await tester.pumpAndSettle();

    await tester.tap(find.byType(DropdownButtonFormField<String>).first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Shield  (androidtv)').last);
    await tester.pumpAndSettle();

    await tester.tap(find.byType(DropdownButtonFormField<String>).last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Launch app').last);
    await tester.pumpAndSettle();

    FilledButton saveButton() => tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Save'));
    expect(saveButton().onPressed, isNull);

    await tester.enterText(
      find.widgetWithText(TextField, 'App link or package name'),
      'com.plexapp.android',
    );
    await tester.pumpAndSettle();
    expect(saveButton().onPressed, isNotNull);

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    final action = api.saved.scenes.first.bindings['volume_up']!.onPress.last;
    expect(action.command, 'launch_app');
    expect(action.params, {'app': 'com.plexapp.android'});
  });

  testWidgets('a scene binds buttons on the picture of the remote', (tester) async {
    tester.view.physicalSize = const Size(1400, 1800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Watch TV'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Button bindings'));
    await tester.pumpAndSettle();

    // The same illustration the live screen uses, not a list of names.
    expect(find.byType(RemoteDiagram), findsOneWidget);
    expect(find.byTooltip('Volume Up \u2014 tv \u2192 volume_up'), findsOneWidget);
    expect(find.byTooltip('Left Arrow \u2014 not bound'), findsOneWidget);
    // A button this scene leaves alone shows what it would do instead.
    expect(find.byTooltip('AC Back \u2014 falls back to tv \u2192 power_on'), findsOneWidget);

    // Tapping one opens the full four-phase editor, which the picture only
    // replaced the *navigation* to.
    await tester.tap(find.byTooltip('Volume Up \u2014 tv \u2192 volume_up'));
    await tester.pumpAndSettle();
    expect(find.text('On press'), findsOneWidget);
    expect(find.text('While held'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, 'Unbind'));
    await tester.pumpAndSettle();

    expect(find.byTooltip('Volume Up \u2014 not bound'), findsOneWidget);
    expect(find.text('0 bound here'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, 'Done'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.scenes.firstWhere((s) => s.id == 'watch_tv').bindings, isEmpty);
  });

  testWidgets('the global scene is a reference, picked from existing scenes', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();

    // The fixture already names "Standby" as the fallback -- no separate
    // "Global bindings" editor exists to have configured that.
    expect(find.textContaining('Standby'), findsWidgets);

    await tester.tap(find.text('Global scene'));
    await tester.pumpAndSettle();

    // The choices are exactly the user's own scenes, plus "None".
    expect(find.text('None'), findsOneWidget);
    expect(find.widgetWithText(SimpleDialogOption, 'Watch TV'), findsOneWidget);
    expect(find.widgetWithText(SimpleDialogOption, 'Standby'), findsOneWidget);

    await tester.tap(find.widgetWithText(SimpleDialogOption, 'Watch TV'));
    await tester.pumpAndSettle();

    expect(api.saved.globalScene, 'watch_tv');
  });

  testWidgets('the global scene can be cleared back to none', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Global scene'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(SimpleDialogOption, 'None'));
    await tester.pumpAndSettle();

    expect(api.saved.globalScene, isNull);
  });

  testWidgets('the default repeat timing applies remote-wide and is editable', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();

    expect(
      find.textContaining('Right now: waits 0.5s first.'),
      findsOneWidget,
    );

    await tester.tap(find.text('Default repeat timing'));
    await tester.pumpAndSettle();

    final delaySlider = find.byType(Slider).first;
    final track = tester.getRect(delaySlider);
    await tester.tapAt(Offset(track.left + 4, track.center.dy));
    await tester.pumpAndSettle();
    expect(find.text('Repeats from the very first packet.'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.defaultRepeatDelay, 0.0);
    expect(find.textContaining('Right now: repeats immediately.'), findsOneWidget);
  });

  testWidgets('the default repeat timing dialog can turn on acceleration', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Scenes'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Default repeat timing'));
    await tester.pumpAndSettle();

    // Off by default: the ramp-time slider has nothing to show yet.
    expect(find.text('Time to reach full speed'), findsNothing);

    final accelSlider = find.byType(Slider).at(2);
    final track = tester.getRect(accelSlider);
    await tester.tapAt(Offset(track.right - 4, track.center.dy));
    await tester.pumpAndSettle();

    // Turning it on reveals the second slider it governs.
    expect(find.text('Time to reach full speed'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.defaultRepeatAccel, greaterThan(1));
    expect(find.textContaining('speeding up to'), findsOneWidget);
  });

  testWidgets('the devices screen lists configured devices', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());

    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();

    expect(find.text('Living Room TV'), findsOneWidget);
    expect(find.textContaining('virtual'), findsWidgets);
  });

  testWidgets('the devices screen can be searched by name, backend or address', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());

    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();

    // A backend name narrows to the one device.
    await tester.enterText(find.byType(TextFormField), 'denon');
    await tester.pumpAndSettle();
    expect(find.text('Receiver'), findsOneWidget);
    expect(find.text('Living Room TV'), findsNothing);
    expect(find.text('Shield'), findsNothing);

    // An allowlisted config field (the device's address) is searchable too.
    await tester.enterText(find.byType(TextFormField), '10.0.0.7');
    await tester.pumpAndSettle();
    expect(find.text('Receiver'), findsOneWidget);
    expect(find.text('Lounge TV'), findsNothing);

    // A query nobody matches offers a way back rather than a blank screen.
    await tester.enterText(find.byType(TextFormField), 'nonexistent-device');
    await tester.pumpAndSettle();
    expect(find.textContaining('Nothing matches'), findsOneWidget);
    expect(find.text('Receiver'), findsNothing);

    await tester.tap(find.text('Clear search'));
    await tester.pumpAndSettle();
    expect(find.text('Living Room TV'), findsOneWidget);
    expect(find.text('Receiver'), findsOneWidget);
  });

  testWidgets('a device that needs pairing offers it, and sends back the code', (tester) async {
    tester.view.physicalSize = const Size(1400, 1600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();

    // The list says what is wrong before the device is even opened. (More
    // than one device can say it -- the LG TV fixture is unpaired too.)
    expect(find.textContaining('not paired'), findsWidgets);

    await tester.tap(find.text('Shield'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Pair this device'));
    await tester.pumpAndSettle();

    expect(api.paired, ['start']);
    expect(find.textContaining('code shown on the device'), findsOneWidget);

    await tester.enterText(find.byType(TextField).last, '123456');
    await tester.tap(find.widgetWithText(FilledButton, 'Connect'));
    await tester.pumpAndSettle();

    expect(api.paired, ['start', '123456']);
  });

  testWidgets('a pairing handshake with nothing to type back asks for a confirm instead',
      (tester) async {
    // LG webOS TVs pair by a prompt accepted on the TV's own remote, not a
    // code typed back here -- pairInputLabel is empty for exactly this
    // reason. DevicesScreen._pair must not fall back to a text field asking
    // for a "Code" nobody has, the way it used to.
    tester.view.physicalSize = const Size(1400, 1600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Lounge TV'));
    await tester.pumpAndSettle();

    // 'Pair this TV' appears twice -- the section header and the button
    // both show the backend's own pairLabel -- so the tap is scoped to the
    // button specifically, the same way the Home Assistant pairing test
    // disambiguates its own identically-worded header and button.
    await tester.tap(find.widgetWithText(FilledButton, 'Pair this TV'));
    await tester.pumpAndSettle();

    expect(api.paired, ['start']);
    expect(find.textContaining('just appeared on the TV'), findsOneWidget);
    // No code box in the dialog itself -- there is nothing here to type
    // into. (The page behind it still has its own Name/Address fields.)
    expect(
      find.descendant(of: find.byType(AlertDialog), matching: find.byType(TextField)),
      findsNothing,
    );

    await tester.tap(find.widgetWithText(FilledButton, 'Done'));
    await tester.pumpAndSettle();

    expect(api.paired, ['start', '']);
  });

  testWidgets('a setting with a fixed set of answers is picked, not typed', (tester) async {
    // The dropdown comes out of the schema alone, so any backend that offers a
    // choice gets one. Typed by hand, "telnet" is a keystroke away from
    // silently staying on HTTP -- the kind of mistake this project makes a
    // point of catching while configuring rather than at press time.
    tester.view.physicalSize = const Size(1400, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Receiver'));
    await tester.pumpAndSettle();

    expect(find.widgetWithText(TextField, 'How to reach it'), findsNothing);

    await tester.tap(find.text('How to reach it'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('telnet').last);
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.device('avr')!.config['transport'], 'telnet');
  });

  testWidgets('a discoverable device offers to find itself on the network, and fills in what it finds',
      (tester) async {
    // Whether the button appears at all comes from the hub's BackendInfo
    // (discoverable/discoverField), not from a name the app hard-codes --
    // this is the Denon backend, which has no pairing step of its own.
    tester.view.physicalSize = const Size(1400, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    api.discoverResults['denon'] = [
      DiscoveredDevice(name: 'Living Room Denon', host: '192.168.1.50', version: 'AVR-X2700H'),
    ];
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Receiver'));
    await tester.pumpAndSettle();

    expect(find.text('Find on the network'), findsOneWidget);

    await tester.tap(find.text('Find on the network'));
    await tester.pumpAndSettle();

    expect(find.text('Living Room Denon'), findsOneWidget);
    expect(find.textContaining('192.168.1.50'), findsOneWidget);

    await tester.tap(find.text('Living Room Denon'));
    await tester.pumpAndSettle();

    expect(find.widgetWithText(TextField, 'Address'), findsOneWidget);
    expect(
      tester.widget<TextField>(find.widgetWithText(TextField, 'Address')).controller!.text,
      '192.168.1.50',
    );
  });

  testWidgets('a backend with no discover route offers no discovery button', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Living Room TV'));
    await tester.pumpAndSettle();

    expect(find.text('Find on the network'), findsNothing);
  });

  /// Opens Devices -> House, which is the Home Assistant device.
  Future<FakeApi> openHomeAssistant(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1400, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('House'));
    await tester.pumpAndSettle();
    return api;
  }

  testWidgets('the pairing screen uses the backend’s own words, not the TV’s', (tester) async {
    // The handshake generalises; "a code on its screen" does not, and telling
    // a Home Assistant user to read one off a television would be nonsense.
    final api = await openHomeAssistant(tester);

    expect(find.text('Connect to Home Assistant'), findsWidgets);
    expect(find.textContaining('issues a token instead'), findsOneWidget);
    expect(find.text('Pair this device'), findsNothing);

    await tester.tap(find.widgetWithText(FilledButton, 'Connect to Home Assistant').last);
    await tester.pumpAndSettle();

    expect(find.text('Long-lived access token'), findsOneWidget);
    // A token is a couple of hundred characters; a one-line box cannot be read back.
    final field = tester.widget<TextField>(find.byType(TextField).last);
    expect(field.maxLines, greaterThan(1));

    await tester.enterText(find.byType(TextField).last, 'a-long-lived-token');
    await tester.tap(find.widgetWithText(FilledButton, 'Connect'));
    await tester.pumpAndSettle();

    expect(api.paired, ['start', 'a-long-lived-token']);
  });

  testWidgets('entities are picked from a list rather than typed as JSON', (tester) async {
    final api = await openHomeAssistant(tester);

    await tester.tap(find.text('Choose entities'));
    await tester.pumpAndSettle();
    expect(api.entityCalls, ['house']);

    // Grouped by domain, and what was already exposed comes up ticked.
    expect(find.text('Lights  (2)'), findsOneWidget);
    expect(find.text('Scenes  (1)'), findsOneWidget);
    expect(find.text('1 picked'), findsOneWidget);

    await tester.tap(find.text('Sofa Lamp'));
    await tester.pumpAndSettle();
    expect(find.text('2 picked'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Save 2'));
    await tester.pumpAndSettle();

    // Written into the form, not saved: nothing on this page commits until Save.
    expect(find.textContaining('light.sofa'), findsOneWidget);
    expect(api.saved.devices.firstWhere((d) => d.id == 'house').config['entities'],
        ['light.kitchen']);

    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.saved.devices.firstWhere((d) => d.id == 'house').config['entities'],
        ['light.kitchen', 'light.sofa']);
  });

  testWidgets('the entity picker searches, because a real instance has hundreds', (tester) async {
    await openHomeAssistant(tester);
    await tester.tap(find.text('Choose entities'));
    await tester.pumpAndSettle();

    await tester.enterText(find.widgetWithText(TextField, 'Search 3 entities'), 'movie');
    await tester.pumpAndSettle();

    expect(find.text('Movie Night'), findsOneWidget);
    expect(find.text('Kitchen'), findsNothing);
  });

  testWidgets('picking entities needs the hub running, and says so', (tester) async {
    tester.view.physicalSize = const Size(1400, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(hubState: 'stopped'));
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('House'));
    await tester.pumpAndSettle();

    // Everything above it still edits and saves; only the parts that ask the
    // equipment a question are off.
    expect(
      tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Choose entities')).onPressed,
      isNull,
    );
    expect(find.textContaining('needs the hub running'), findsWidgets);
  });

  /// Opens Devices -> Shield -> Map the remote, leaving the target dialog up.
  Future<FakeApi> openMapper(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1400, 1800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Shield'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Map the remote to this device'));
    await tester.pumpAndSettle();
    return api;
  }

  testWidgets('mapping into a new scene starts from the suggestions', (tester) async {
    final api = await openMapper(tester);

    await tester.tap(find.text('A new scene'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Continue'));
    await tester.pumpAndSettle();

    // All three suggestions arrive picked, so accepting the lot is one tap.
    await tester.tap(find.widgetWithText(FilledButton, 'Assign 3'));
    await tester.pumpAndSettle();

    final scene = api.saved.scenes.firstWhere((s) => s.id == 'watch_shield');
    expect(scene.devices, ['shield']);
    expect(scene.bindings.keys, containsAll(['volume_up', 'ac_back', 'left_arrow']));
    expect(scene.bindings['ac_back']!.onPress.single.command, 'back');
    // Holding volume should ramp; holding Back must not fire repeatedly.
    expect(scene.bindings['volume_up']!.onRepeat, hasLength(1));
    expect(scene.bindings['ac_back']!.onRepeat, isEmpty);
  });

  testWidgets('mapping into an existing scene touches only the buttons picked', (tester) async {
    final api = await openMapper(tester);

    await tester.tap(find.text('Watch TV'));
    await tester.pumpAndSettle();

    // Nothing is picked yet, and what the scene already binds is shown as
    // kept rather than silently staged for replacement.
    expect(find.byTooltip('Volume Up \u2014 keeps tv \u2192 volume_up'), findsOneWidget);
    expect(find.byTooltip('Left Arrow \u2014 not assigned'), findsOneWidget);
    expect(
      tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Assign 0')).onPressed,
      isNull,
    );

    await tester.tap(find.byTooltip('Left Arrow \u2014 not assigned'));
    await tester.pumpAndSettle();

    // The device's own suggestion for this button is preselected...
    expect(find.text('suggested for this button'), findsOneWidget);
    // ...but any other command can be chosen instead.
    await tester.tap(find.widgetWithText(ListTile, 'Right'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Assign'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Assign 1'));
    await tester.pumpAndSettle();

    final scene = api.saved.scenes.firstWhere((s) => s.id == 'watch_tv');
    expect(scene.bindings['left_arrow']!.onPress.single.command, 'dpad_right');
    expect(scene.bindings['left_arrow']!.onPress.single.device, 'shield');
    // The binding that was already there is untouched.
    expect(scene.bindings['volume_up']!.onPress.single.device, 'tv');
    expect(scene.devices, containsAll(['tv', 'shield']));
  });

  testWidgets('a command that must not repeat cannot have repeat turned on', (tester) async {
    final api = await openMapper(tester);

    await tester.tap(find.text('Watch TV'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Left Arrow — not assigned'));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ListTile, 'Select'));
    await tester.pumpAndSettle();

    // The switch is shown but disallowed, not just defaulted off -- picking
    // a command like Select and holding the button must not silently repeat
    // it: on Android TV that turns a long press into a burst of taps.
    final repeatSwitch = tester.widget<SwitchListTile>(find.byType(SwitchListTile));
    expect(repeatSwitch.value, isFalse);
    expect(repeatSwitch.onChanged, isNull);
    expect(find.textContaining("isn't safe to repeat"), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Assign'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Assign 1'));
    await tester.pumpAndSettle();

    final scene = api.saved.scenes.firstWhere((s) => s.id == 'watch_tv');
    expect(scene.bindings['left_arrow']!.onPress.single.command, 'select');
    expect(scene.bindings['left_arrow']!.onRepeat, isEmpty);
  });

  testWidgets('a button about to be overwritten says what it replaces', (tester) async {
    final api = await openMapper(tester);

    await tester.tap(find.text('Watch TV'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Volume Up \u2014 keeps tv \u2192 volume_up'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Already bound in this scene'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Assign'));
    await tester.pumpAndSettle();

    expect(
      find.byTooltip('Volume Up \u2014 Volume up  (replaces tv \u2192 volume_up)'),
      findsOneWidget,
    );
    expect(find.text('1 will be replaced'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Assign 1'));
    await tester.pumpAndSettle();

    final scene = api.saved.scenes.firstWhere((s) => s.id == 'watch_tv');
    expect(scene.bindings['volume_up']!.onPress.single.device, 'shield');
  });

  Future<FakeApi> openHouseMapper(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1400, 1800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    // The SmartHome +/- keys, which the plain Shield/TV fixture above never
    // needed -- only Home Assistant-style devices suggest anything for them.
    api.known = [
      ...api.known,
      ButtonInfo(key: 'consumer_0x0ff0', label: 'SmartHome +', signatures: const ['C3F00F00']),
      ButtonInfo(key: 'consumer_0x0ff1', label: 'SmartHome -', signatures: const ['C3F10F00']),
    ];
    await pumpApp(tester, api);
    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('House'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Map the remote to this device'));
    await tester.pumpAndSettle();
    return api;
  }

  testWidgets('a device that suggests the SmartHome keys offers the adjust option', (tester) async {
    final api = await openHouseMapper(tester);

    await tester.tap(find.text('A new scene'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Continue'));
    await tester.pumpAndSettle();

    // Both +/- keys arrive pre-picked with the adjust suggestion, the same
    // way a device's ordinary command suggestions arrive pre-picked.
    expect(find.byTooltip('SmartHome + — Turn up (follows the last device touched)'), findsOneWidget);
    expect(find.byTooltip('SmartHome - — Turn down (follows the last device touched)'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Assign 2'));
    await tester.pumpAndSettle();

    final scene = api.saved.scenes.firstWhere((s) => s.id == 'watch_house');
    final plus = scene.bindings['consumer_0x0ff0']!.onPress.single;
    final minus = scene.bindings['consumer_0x0ff1']!.onPress.single;
    expect(plus.type, 'adjust');
    expect(plus.direction, 'up');
    expect(minus.direction, 'down');
    // Ramping while held is the entire point, so it repeats by default.
    expect(scene.bindings['consumer_0x0ff0']!.onRepeat, hasLength(1));
  });

  testWidgets('picking the adjust option by hand offers it alongside commands', (tester) async {
    final api = await openHouseMapper(tester);

    await tester.tap(find.text('A new scene'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Continue'));
    await tester.pumpAndSettle();

    // Reopen the + key and switch it to a plain command instead, proving the
    // adjust row is a choice alongside the device's commands, not the only
    // option once suggested.
    await tester.tap(find.byTooltip('SmartHome + — Turn up (follows the last device touched)'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(ListTile, 'Power On'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Assign'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Assign 2'));
    await tester.pumpAndSettle();

    final scene = api.saved.scenes.firstWhere((s) => s.id == 'watch_house');
    expect(scene.bindings['consumer_0x0ff0']!.onPress.single.type, 'device');
    expect(scene.bindings['consumer_0x0ff0']!.onPress.single.command, 'power_on');
  });

  testWidgets('the mapper fits a phone screen', (tester) async {
    // The diagram is 340x1150 in design space and the command picker wants a
    // fixed size, so a narrow viewport is where this screen would overflow.
    // A widget test fails on overflow, which is the assertion here.
    tester.view.physicalSize = const Size(420, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(MaterialApp(
      home: RemoteMapperPage(
        buttons: [
          ButtonInfo(key: 'volume_up', label: 'Volume Up', signatures: const ['C3E90000']),
          ButtonInfo(key: 'left_arrow', label: 'Left Arrow', signatures: const ['C1500000']),
        ],
        deviceId: 'shield',
        deviceName: 'Shield',
        commands: [
          CommandInfo(name: 'volume_up', label: 'Volume up', description: '', repeatable: true),
          CommandInfo(name: 'dpad_left', label: 'Left', description: ''),
        ],
        suggested: const {'left_arrow': 'dpad_left'},
        existing: {'volume_up': Binding(onPress: [HubAction.device('amp', 'volume_up')])},
        targetName: 'Watch TV',
      ),
    ));
    await tester.pumpAndSettle();

    expect(find.byType(RemoteDiagram), findsOneWidget);
    expect(find.byTooltip('Left Arrow \u2014 not assigned'), findsOneWidget);

    // And the picker, the other fixed-size thing on this screen.
    await tester.tap(find.byTooltip('Left Arrow \u2014 not assigned'));
    await tester.pumpAndSettle();
    expect(find.text('suggested for this button'), findsOneWidget);
  });

  testWidgets('a phone-width layout uses the bottom bar instead of the rail', (tester) async {
    tester.view.physicalSize = const Size(420, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi());

    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.byType(NavigationRail), findsNothing);
  });

  testWidgets('a hub error is surfaced rather than swallowed', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final store = HubStore(api: _BrokenApi());
    await tester.pumpWidget(HarmonyHubApp(store: store));
    await tester.pumpAndSettle();

    expect(find.textContaining('hub is down'), findsOneWidget);
  });

  // ------------------------------------------------------------------
  // Settings, and staying usable while the hub is not
  // ------------------------------------------------------------------

  /// Scrolls a long page until the target exists and is on screen.
  ///
  /// `ensureVisible` is not enough: these pages are `ListView`s that build
  /// lazily, so a widget far enough down has no element to make visible yet.
  Future<void> reveal(WidgetTester tester, Finder finder) async {
    await tester.scrollUntilVisible(finder, 200, scrollable: find.byType(Scrollable).first);
    await tester.pumpAndSettle();
  }

  Future<HubStore> openSettings(
    WidgetTester tester,
    FakeApi api, {
    UiPrefs? prefs,
    Size size = const Size(1400, 1200),
  }) async {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final store = await pumpApp(tester, api, prefs: prefs);
    // `.first` because a NavigationRail renders both a resting and a
    // selected label for its destinations while crossfading between them.
    await tester.tap(find.text('Settings').first);
    await tester.pumpAndSettle();
    return store;
  }

  /// Opens one of Settings' rows -- Runtime, Event source, Files, and so on
  /// -- each of which is its own page rather than a card on the list.
  Future<void> openSection(WidgetTester tester, String title) async {
    final tile = find.widgetWithText(ListTile, title);
    await reveal(tester, tile);
    await tester.tap(tile);
    await tester.pumpAndSettle();
  }

  testWidgets('the settings screen shows what the hub is doing', (tester) async {
    await openSettings(tester, FakeApi());

    expect(find.text('Event source'), findsOneWidget);
    expect(find.textContaining('0.0.0.0:8765'), findsOneWidget);

    await openSection(tester, 'Runtime');
    expect(find.text('Hub is running'), findsOneWidget);
    expect(find.textContaining('0.0.0.0:8765'), findsOneWidget);
  });

  testWidgets('the hub can be stopped and started from settings', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Runtime');

    await tester.tap(find.widgetWithText(OutlinedButton, 'Stop'));
    await tester.pumpAndSettle();

    expect(api.lifecycle, ['stop']);
    expect(find.text('Hub is stopped'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Start'));
    await tester.pumpAndSettle();

    expect(api.lifecycle, ['stop', 'start']);
    expect(find.text('Hub is running'), findsOneWidget);
  });

  testWidgets('an ordinary hub with no release system shows no software card', (tester) async {
    await openSettings(tester, FakeApi());
    expect(find.text('Software'), findsNothing);
  });

  testWidgets('a deployed hub shows what it is running and offers a rollback', (tester) async {
    final api = FakeApi()
      ..fakeVersion = VersionInfo(
        deployed: true,
        updatesEnabled: true,
        buildId: 'build-2',
        previous: 'build-1',
        gitSha: 'abc1234',
        trial: TrialInfo(release: 'build-2', attempts: 1),
        tokenFingerprint: 'deadbeef',
      );
    await openSettings(tester, api);
    await openSection(tester, 'Software');

    final rollbackButton = find.widgetWithText(OutlinedButton, 'Roll back to build-1');
    await reveal(tester, rollbackButton);

    // findsWidgets, not findsOneWidget: the Settings list stays mounted
    // underneath the pushed "Software" page, and its own row is titled the
    // same thing.
    expect(find.text('Software'), findsWidgets);
    expect(find.text('build-2'), findsOneWidget);
    expect(find.textContaining('abc1234'), findsOneWidget);
    expect(find.textContaining('On trial'), findsOneWidget);
    expect(find.textContaining('deadbeef'), findsOneWidget);

    await tester.tap(rollbackButton);
    await tester.pumpAndSettle();

    // A confirmation dialog stands between the button and the actual call --
    // a restart is disruptive enough that a stray tap should not trigger it.
    expect(api.rolledBack, isFalse);
    await tester.tap(find.widgetWithText(FilledButton, 'Roll back'));
    await tester.pumpAndSettle();

    expect(api.rolledBack, isTrue);
  });

  testWidgets('a hub with nothing to roll back to disables the rollback button', (tester) async {
    final api = FakeApi()..fakeVersion = VersionInfo(deployed: true, updatesEnabled: true, buildId: 'build-1');
    await openSettings(tester, api);
    await openSection(tester, 'Software');

    final finder = find.widgetWithText(OutlinedButton, 'No previous release');
    await reveal(tester, finder);
    expect(tester.widget<OutlinedButton>(finder).onPressed, isNull);
  });

  // ------------------------------------------------------------------
  // Updating from a GitHub release
  // ------------------------------------------------------------------

  testWidgets('a deployed hub with github updates off offers no way to check', (tester) async {
    final api = FakeApi()
      ..fakeVersion = VersionInfo(deployed: true, updatesEnabled: true, buildId: 'build-1', updatesFromGithub: false);
    await openSettings(tester, api);
    await openSection(tester, 'Software');

    expect(find.text('Check for updates'), findsNothing);
  });

  testWidgets('a deployed hub with nothing available offers to check for updates', (tester) async {
    final api = FakeApi()
      ..fakeVersion = VersionInfo(deployed: true, updatesEnabled: true, buildId: 'build-1', updatesFromGithub: true);
    await openSettings(tester, api);
    await openSection(tester, 'Software');

    final checkButton = find.widgetWithText(TextButton, 'Check for updates');
    await reveal(tester, checkButton);
    expect(find.text('Not checked yet.'), findsOneWidget);

    await tester.tap(checkButton);
    await tester.pumpAndSettle();

    expect(api.checkedForUpdate, isTrue);
  });

  testWidgets('a release available offers to install it, behind a confirmation', (tester) async {
    final api = FakeApi()
      ..fakeVersion = VersionInfo(deployed: true, updatesEnabled: true, buildId: 'build-1', updatesFromGithub: true)
      ..fakeAvailableUpdate = UpdateCheckInfo(
        available: AvailableUpdateInfo(tag: 'v1.2.3', buildId: 'build-2', notes: 'Fixes things'),
      );
    await openSettings(tester, api);
    await openSection(tester, 'Software');

    final updateButton = find.widgetWithText(FilledButton, 'Update software');
    await reveal(tester, updateButton);
    expect(find.text('v1.2.3 is available'), findsOneWidget);
    expect(find.textContaining('build-2'), findsOneWidget);

    await tester.tap(updateButton);
    await tester.pumpAndSettle();

    // A confirmation dialog stands between the button and the actual
    // install, the same way rollback's does -- this kicks off a
    // multi-minute download and restart on a Pi, not something a stray tap
    // should start.
    expect(api.installRequests, isEmpty);
    await tester.tap(find.widgetWithText(FilledButton, 'Update'));
    await tester.pumpAndSettle();

    expect(api.installRequests, [false]);

    // The hub stays fully reachable while it downloads and installs, unlike
    // a rollback -- so the card has to say so itself, driven by the same
    // `update` events the Live log renders.
    api.eventController.add(
      HubEvent(type: 'update', at: DateTime.now(), ok: true, detail: 'Installing dependencies'),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('Installing dependencies'), findsOneWidget);
    // The release details and its "Update software" button give way to the
    // progress note entirely while installing -- there is nothing left to
    // press a second time.
    expect(find.widgetWithText(FilledButton, 'Update software'), findsNothing);
  });

  testWidgets('an install refused for an active scene offers to force it', (tester) async {
    final api = FakeApi()
      ..fakeVersion = VersionInfo(deployed: true, updatesEnabled: true, buildId: 'build-1', updatesFromGithub: true)
      ..fakeAvailableUpdate = UpdateCheckInfo(available: AvailableUpdateInfo(tag: 'v1.2.3', buildId: 'build-2'))
      ..refuseInstallWithoutForce = true;
    await openSettings(tester, api);
    await openSection(tester, 'Software');

    final updateButton = find.widgetWithText(FilledButton, 'Update software');
    await reveal(tester, updateButton);
    await tester.tap(updateButton);
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Update'));
    await tester.pumpAndSettle();

    expect(api.installRequests, [false]);
    expect(find.text('A scene is active'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Install anyway'));
    await tester.pumpAndSettle();

    expect(api.installRequests, [false, true]);
  });

  testWidgets('the update banner on the home shell can be dismissed per release', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi()
      ..fakeVersion = VersionInfo(deployed: true, updatesEnabled: true, buildId: 'build-1', updatesFromGithub: true)
      ..fakeAvailableUpdate = UpdateCheckInfo(available: AvailableUpdateInfo(tag: 'v1.2.3', buildId: 'build-2'));
    final prefs = UiPrefs.memory();
    await pumpApp(tester, api, prefs: prefs);

    expect(find.textContaining('v1.2.3 is available'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, 'Dismiss'));
    await tester.pumpAndSettle();

    expect(find.textContaining('v1.2.3 is available'), findsNothing);
    expect(prefs.get(kDismissedUpdateBuildId), 'build-2');
  });

  testWidgets('a setting can be edited and saved', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');

    // The settings page is taller than any window, so anything tapped has to
    // be scrolled to first -- a tap at an off-screen offset warns but does
    // not fail, which would leave this passing for the wrong reason.
    final dropdown = find.byType(DropdownButtonFormField<String>);
    await tester.ensureVisible(dropdown);
    await tester.pumpAndSettle();
    await tester.tap(dropdown);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Radio — the real remote').last);
    await tester.pumpAndSettle();

    await tester.enterText(find.widgetWithText(TextFormField, 'Remote address'), '17129BFCB6');
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(OutlinedButton, 'Save'));
    await tester.pumpAndSettle();

    expect(api.savedSettings.source, 'radio');
    expect(api.savedSettings.address, '17129BFCB6');
    expect(api.restartedOnSave, isFalse);
  });

  Future<void> openRadioSource(WidgetTester tester) async {
    final dropdown = find.byType(DropdownButtonFormField<String>);
    await tester.ensureVisible(dropdown);
    await tester.pumpAndSettle();
    await tester.tap(dropdown);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Radio — the real remote').last);
    await tester.pumpAndSettle();
  }

  testWidgets('a "None" event source offers no pause switch', (tester) async {
    await openSettings(tester, FakeApi());
    await openSection(tester, 'Event source');

    expect(find.text('Pause command execution'), findsNothing);
  });

  testWidgets('switching source to radio offers the pause switch', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    expect(find.text('Pause command execution'), findsOneWidget);
  });

  testWidgets('pausing calls the API and shows a note, without touching the draft', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    expect(find.text('Paused — button presses are being logged but not acted on.'), findsNothing);

    final toggle = find.widgetWithText(SwitchListTile, 'Pause command execution');
    await tester.ensureVisible(toggle);
    await tester.tap(toggle);
    await tester.pumpAndSettle();

    expect(api.paused, isTrue);
    expect(find.text('Paused — button presses are being logged but not acted on.'), findsOneWidget);
    // Nothing was saved -- this takes effect live, not through the draft/Save flow.
    expect(api.savedSettings.source, isNot('radio'));
  });

  testWidgets('the Event source row itself shows Paused once collapsed', (tester) async {
    final api = FakeApi()
      ..paused = true
      ..savedSettings = HubSettings(source: 'radio', address: '17129BFCB6');
    await openSettings(tester, api);
    await openSection(tester, 'Event source');

    // Collapse back to the section list to check the row's own subtitle,
    // not the (already-open) card inside it.
    await tester.pageBack();
    await tester.pumpAndSettle();

    expect(find.textContaining('Paused'), findsOneWidget);
  });

  testWidgets('resuming clears the paused note', (tester) async {
    final api = FakeApi()..paused = true;
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    expect(find.text('Paused — button presses are being logged but not acted on.'), findsOneWidget);

    final toggle = find.widgetWithText(SwitchListTile, 'Pause command execution');
    await tester.ensureVisible(toggle);
    await tester.tap(toggle);
    await tester.pumpAndSettle();

    expect(api.paused, isFalse);
    expect(find.text('Paused — button presses are being logged but not acted on.'), findsNothing);
  });

  testWidgets('find my remote asks which method before searching', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    final findButton = find.widgetWithText(OutlinedButton, 'Find my remote');
    await tester.ensureVisible(findButton);
    await tester.pumpAndSettle();
    await tester.tap(findButton);
    await tester.pumpAndSettle();

    expect(find.text('How should I look for your remote?'), findsOneWidget);
    expect(find.text('I still have my Harmony Hub'), findsOneWidget);
    expect(find.text("I don't have a Hub"), findsOneWidget);
    // Nothing started yet -- the dialog is a choice, not a confirmation.
    expect(api.discoveryMethods, isEmpty);
  });

  testWidgets('choosing "I still have my Harmony Hub" starts the hub method', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('I still have my Harmony Hub'));
    // Not pumpAndSettle: starting the search kicks off a real 1-second
    // polling Timer (`_pollDiscovery`) that only stops once the fake status
    // moves off "running", which these tests never ask it to -- they only
    // care what `_findRemote` did *before* any polling happens. A couple of
    // bounded pumps is enough to let the dialog close and the initial
    // `_notify` land, without waiting the timer out.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(api.discoveryMethods, ['hub']);
    expect(find.textContaining('pairing mode'), findsOneWidget);
  });

  testWidgets('choosing "I don\'t have a Hub" starts the hub-less method', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.text("I don't have a Hub"));
    // See the comment in the "hub method" test above -- bounded pumps,
    // not pumpAndSettle, because starting the search leaves a repeating
    // polling Timer behind that this test has no reason to wait out.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(api.discoveryMethods, ['sniff']);
    expect(find.textContaining('press and release buttons'), findsOneWidget);
  });

  testWidgets('backing out of the discovery method dialog starts nothing', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();

    // Tap outside the dialog, the standard way to dismiss a SimpleDialog.
    await tester.tapAt(const Offset(10, 10));
    await tester.pumpAndSettle();

    expect(api.discoveryMethods, isEmpty);
  });

  testWidgets('the discovery dialog warns that a running radio hub will pause', (tester) async {
    final api = FakeApi()..savedSettings = HubSettings(source: 'radio', address: '17129BFCB6');
    await openSettings(tester, api);
    await openSection(tester, 'Event source');

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();

    expect(find.textContaining("it'll pause while I search"), findsOneWidget);
  });

  testWidgets('the discovery dialog has no pause warning for an unsaved draft change', (tester) async {
    // Switching the dropdown only touches the draft -- the hub still
    // running underneath is on whatever was last saved, so a search
    // started here would not actually touch it.
    final api = FakeApi(); // savedSettings.source defaults to 'none'
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();

    expect(find.textContaining("it'll pause while I search"), findsNothing);
  });

  testWidgets('starting or restarting the hub is disabled while a search is running', (tester) async {
    final api = FakeApi(hubState: 'stopped');
    await openSettings(tester, api);
    await openSection(tester, 'Event source');
    await openRadioSource(tester);

    await tester.ensureVisible(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(OutlinedButton, 'Find my remote'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('I still have my Harmony Hub'));
    // See the comment on the discovery-method tests above -- bounded
    // pumps, not pumpAndSettle, because of the repeating polling Timer.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    // Back to the section list, then into Runtime, to check its buttons
    // with the search left running in the background.
    await tester.pageBack();
    await tester.pumpAndSettle();
    await openSection(tester, 'Runtime');

    final startButton = tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Start'));
    final restartButton = tester.widget<OutlinedButton>(find.widgetWithText(OutlinedButton, 'Restart'));
    expect(startButton.onPressed, isNull);
    expect(restartButton.onPressed, isNull);
  });

  testWidgets('saving can restart the hub onto the new settings', (tester) async {
    final api = FakeApi(hubState: 'stopped');
    await openSettings(tester, api);

    await tester.tap(find.widgetWithText(FilledButton, 'Save & restart hub'));
    await tester.pumpAndSettle();

    expect(api.restartedOnSave, isTrue);
  });

  testWidgets('a bind change says it needs the process restarted', (tester) async {
    await openSettings(tester, FakeApi());
    await openSection(tester, 'Web server');

    await tester.enterText(find.widgetWithText(TextFormField, 'Port'), '9999');
    await tester.pumpAndSettle();

    expect(find.textContaining('take effect when the hub process is restarted'), findsOneWidget);
  });

  testWidgets('checks and the dry-run render into the same list', (tester) async {
    final api = FakeApi();
    await openSettings(tester, api);
    await openSection(tester, 'Checks');

    final runChecks = find.widgetWithText(OutlinedButton, 'Run checks');
    await reveal(tester, runChecks);
    await tester.tap(runChecks);
    await tester.pumpAndSettle();
    expect(find.text('no built web UI found'), findsOneWidget);

    final tryThese = find.widgetWithText(OutlinedButton, 'Try these settings');
    await reveal(tester, tryThese);
    await tester.tap(tryThese);
    await tester.pumpAndSettle();
    expect(find.text('These settings, not yet saved'), findsOneWidget);
    expect(find.text('checked none'), findsOneWidget);
  });

  testWidgets('a hub that could not start says why, on a page that loaded fine', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(hubState: 'failed'));

    expect(find.textContaining('The hub could not start'), findsOneWidget);
    // And the rest of the app came up regardless.
    expect(find.byType(NavigationRail), findsOneWidget);
  });

  // ------------------------------------------------------------------
  // Learning the remote
  //
  // The signatures come off the live event stream: the hub publishes an
  // unlearned press under its own hex rather than dropping it, which is what
  // makes it nameable at all.
  // ------------------------------------------------------------------

  /// Fakes the remote sending a button, and lets the UI catch up.
  ///
  /// The pump past [kFlashDuration] matters: the store lights a pressed
  /// button up on a timer, and a test that ends while one is outstanding
  /// fails on the pending timer rather than on anything it was checking.
  Future<void> press(
    WidgetTester tester,
    FakeApi api,
    String button, {
    String? label,
    bool known = false,
  }) async {
    api.eventController.add(HubEvent(
      type: 'button',
      at: DateTime.now(),
      button: button,
      label: label,
      phase: 'press',
      detail: known ? null : 'unbound',
    ));
    await tester.pumpAndSettle();
    await tester.pump(kFlashDuration);
  }

  Future<HubStore> openLearn(WidgetTester tester, FakeApi api, {String source = 'radio'}) async {
    tester.view.physicalSize = const Size(1400, 1400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    api.savedSettings = HubSettings(
      source: source,
      address: source == 'radio' ? '17129BFCB6' : null,
    );
    final store = await pumpApp(tester, api);
    await tester.tap(find.text('Settings').first);
    await tester.pumpAndSettle();
    await openSection(tester, 'Remote buttons');

    final open = find.widgetWithText(FilledButton, 'Learn or edit buttons');
    await reveal(tester, open);
    await tester.tap(open);
    await tester.pumpAndSettle();
    return store;
  }

  testWidgets('an unknown press appears, ready to be named', (tester) async {
    final api = FakeApi();
    await openLearn(tester, api);

    expect(find.text('Listening'), findsOneWidget);

    await press(tester, api, 'C3400000', label: 'Menu');

    expect(find.text('C3400000'), findsOneWidget);
    // The hub already decodes the HID name, so most buttons arrive prefilled.
    expect(find.text('Menu'), findsOneWidget);
    expect(find.text('Saves as menu'), findsOneWidget);
  });

  testWidgets('naming a press and saving sends it to the hub', (tester) async {
    final api = FakeApi();
    await openLearn(tester, api);
    await press(tester, api, 'C3400000', label: 'Menu');

    await tester.tap(find.widgetWithText(FilledButton, 'Save 1'));
    await tester.pumpAndSettle();

    final learned = api.known.firstWhere((b) => b.key == 'menu');
    expect(learned.label, 'Menu');
    expect(learned.signatures, ['C3400000']);
    // Saved rows leave the pending list rather than lingering.
    expect(find.text('C3400000'), findsNothing);
  });

  testWidgets('an unnamed press is skipped rather than saved as nonsense', (tester) async {
    // A button in Logitech's vendor range decodes to no HID name at all.
    final api = FakeApi();
    await openLearn(tester, api);
    await press(tester, api, 'C3F70100');

    expect(find.text('Unnamed — will be skipped when you save.'), findsOneWidget);
    expect(
      tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Save')).onPressed,
      isNull,
    );

    await tester.enterText(find.byType(TextField).first, 'Red');
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Save 1'));
    await tester.pumpAndSettle();

    expect(api.known.firstWhere((b) => b.key == 'red').label, 'Red');
  });

  testWidgets('naming a press after an existing button attaches to it', (tester) async {
    // The same physical key can report differently per activity, and both
    // signatures are still that key.
    final api = FakeApi();
    await openLearn(tester, api);
    await press(tester, api, 'C3E90001', label: 'Volume Up');

    expect(find.text('Adds this signature to the existing "Volume Up".'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Save 1'));
    await tester.pumpAndSettle();

    expect(api.known.firstWhere((b) => b.key == 'volume_up').signatures,
        containsAll(['C3E90000', 'C3E90001']));
    expect(api.known.where((b) => b.key == 'volume_up'), hasLength(1));
  });

  testWidgets('a press can be discarded and brought back', (tester) async {
    final api = FakeApi();
    await openLearn(tester, api);
    await press(tester, api, 'C3400000', label: 'Menu');

    await tester.tap(find.byTooltip('Not a button I want'));
    await tester.pumpAndSettle();
    expect(find.text('C3400000'), findsNothing);

    // A discarded signature must stay discarded even as more events arrive,
    // or it would reappear on the next press of anything.
    await press(tester, api, 'C3400000', label: 'Menu');
    expect(find.text('C3400000'), findsNothing);

    await tester.tap(find.text('Un-ignore 1'));
    await tester.pumpAndSettle();
    expect(find.text('C3400000'), findsOneWidget);
  });

  testWidgets('a known press is reported as already learned', (tester) async {
    final api = FakeApi();
    await openLearn(tester, api);

    await press(tester, api, 'volume_up', label: 'Volume Up', known: true);

    expect(find.textContaining('already learned, nothing to do'), findsOneWidget);
    expect(find.text('C3E90000'), findsNothing); // not offered for naming
  });

  testWidgets('a learned button can be forgotten', (tester) async {
    final api = FakeApi();
    await openLearn(tester, api);

    final row = find.widgetWithText(ListTile, 'Mute');
    await reveal(tester, row);
    await tester.tap(find.descendant(of: row, matching: find.byTooltip('Forget this button')));
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(FilledButton, 'Forget'));
    await tester.pumpAndSettle();

    expect(api.known.where((b) => b.key == 'mute'), isEmpty);
  });

  testWidgets('it says when the hub cannot hear the remote at all', (tester) async {
    // Otherwise this screen is indistinguishable from a broken one: you
    // press buttons and nothing happens.
    final api = FakeApi(hubState: 'stopped');
    await openLearn(tester, api);

    expect(find.text('The hub is stopped'), findsOneWidget);
  });

  testWidgets('it says when the hub is running but not on a radio', (tester) async {
    await openLearn(tester, FakeApi(), source: 'none');

    expect(find.text('Not listening to a remote'), findsOneWidget);
    expect(find.textContaining('Set it to Radio'), findsOneWidget);
  });

  testWidgets('the live screen offers learning when nothing is known', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    api.known = [];
    await pumpApp(tester, api);

    expect(find.text('No buttons known yet'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Learn the remote'));
    await tester.pumpAndSettle();

    expect(find.text('Learn the remote'), findsWidgets); // the page's own title
    expect(find.text('Already learned (0)'), findsNothing);
  });

  testWidgets('with the hub stopped, presses are off but editing is not', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi(hubState: 'stopped');
    await pumpApp(tester, api);

    expect(find.textContaining('The hub is stopped'), findsOneWidget);
    expect(find.textContaining('the hub is stopped, so nothing to send to'), findsOneWidget);

    // Tapping a button does nothing rather than pretending to send.
    await tester.tap(find.byTooltip('Volume Up — unbound'));
    await tester.pumpAndSettle();
    expect(api.simulated, isEmpty);

    // But the scene editor still opens and still saves, which is the point:
    // a hub that will not start is when its configuration needs changing.
    await tester.tap(find.text('Scenes').first);
    await tester.pumpAndSettle();
    expect(find.widgetWithText(TextButton, 'Start'), findsWidgets);
    expect(
      tester.widget<TextButton>(find.widgetWithText(TextButton, 'Start').first).onPressed,
      isNull,
    );

    await tester.tap(find.text('Watch TV').first);
    await tester.pumpAndSettle();
    expect(find.text('Button bindings'), findsOneWidget);
  });

  // ------------------------------------------------------------------
  // Device-local UI preferences: last tab, filters and searches surviving
  // a reload. See `ui_prefs_test.dart` for the persistence layer itself --
  // these check the app actually reads and writes it at the right moments.
  // ------------------------------------------------------------------

  testWidgets('the app opens on the remembered tab', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(tester, FakeApi(), prefs: UiPrefs.memory({'shell.tab': 'devices'}));

    // Devices, not the Live screen's default content, without tapping
    // anything -- proves the tab was restored on the very first build.
    expect(find.text('Living Room TV'), findsOneWidget);
  });

  testWidgets('an unrecognised remembered tab falls back to Live', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    // Could be an id from an older or newer build; either way, the app
    // must not crash trying to look it up.
    await pumpApp(tester, FakeApi(), prefs: UiPrefs.memory({'shell.tab': 'remote_first'}));

    expect(find.text('SmartHome +/- keys: nothing touched yet'), findsOneWidget);
  });

  testWidgets('selecting a tab persists it', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final backend = MemoryPrefsBackend();
    final prefs = await UiPrefs.open(backend: backend);
    await pumpApp(tester, FakeApi(), prefs: prefs);

    await tester.tap(find.text('Devices'));
    await tester.pumpAndSettle();
    await prefs.flush();

    final stored = jsonDecode((await backend.read())!) as Map;
    expect((stored['values'] as Map)['shell.tab'], 'devices');
  });

  testWidgets('a remembered device search filters the list on first frame', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await pumpApp(
      tester,
      FakeApi(),
      prefs: UiPrefs.memory({'shell.tab': 'devices', 'devices.query': 'shield'}),
    );

    expect(find.text('Shield'), findsOneWidget);
    expect(find.text('Living Room TV'), findsNothing);
  });

  testWidgets('a remembered activity filter is applied to the live log', (tester) async {
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final api = FakeApi();
    await pumpApp(
      tester,
      api,
      prefs: UiPrefs.memory({
        'activity.filter': {
          'hidden': {
            'type': ['button']
          },
          'query': '',
          'sort_by': 'at',
          'ascending': false,
        },
      }),
    );

    api.eventController.add(HubEvent(type: 'button', at: DateTime.now(), button: 'mute', phase: 'press'));
    api.eventController.add(HubEvent(type: 'scene', at: DateTime.now(), scene: 'watch_tv', ok: true));
    await tester.pumpAndSettle();
    await tester.pump(kFlashDuration);

    // Restored without ever opening the filter popup.
    expect(find.text('1 of 2'), findsOneWidget);
  });

  testWidgets('turning off "Remember where I was" stops it persisting', (tester) async {
    final prefs = UiPrefs.memory();
    await openSettings(tester, FakeApi(), prefs: prefs);
    await openSection(tester, 'This browser');

    await reveal(tester, find.text('Remember where I was'));
    await tester.tap(find.text('Remember where I was'));
    await tester.pumpAndSettle();

    expect(prefs.get(kRememberEnabled), isFalse);
  });

  testWidgets('resetting device preferences clears the remembered tab and searches', (tester) async {
    final prefs = UiPrefs.memory({'shell.tab': 'devices', 'devices.query': 'shield'});
    await openSettings(tester, FakeApi(), prefs: prefs);
    await openSection(tester, 'This browser');

    await reveal(tester, find.text("Reset this device's preferences"));
    await tester.tap(find.text("Reset this device's preferences"));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Reset'));
    await tester.pumpAndSettle();

    expect(prefs.get(kShellTab), 'live');
    expect(prefs.get(kDevicesQuery), '');
  });
}

class _BrokenApi extends FakeApi {
  @override
  Future<HubStatus> status() async => throw Exception('the hub is down');
}
