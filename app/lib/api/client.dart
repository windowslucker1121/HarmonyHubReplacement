/// HTTP and WebSocket client for the hub API.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

import 'config.dart';
import 'models.dart';
import 'settings.dart';

const int kDefaultHubPort = 8765;

/// Raised when the hub answers with an error, carrying its explanation.
class HubApiException implements Exception {
  HubApiException(this.statusCode, this.message);

  final int statusCode;
  final String message;

  @override
  String toString() => 'Hub returned $statusCode: $message';
}

/// Works out where the hub is.
///
/// Overridable at build time with `--dart-define=API_BASE=http://host:port`,
/// which is what a packaged mobile build will use. Otherwise: when the page
/// was served by the hub itself, that same origin; when it came from
/// Flutter's dev server on some other port, the hub's default port on the
/// same host. `Uri.base` is used rather than `dart:html` so this file stays
/// compilable for the mobile targets.
String defaultApiBase() {
  const override = String.fromEnvironment('API_BASE');
  if (override.isNotEmpty) return override;

  final page = Uri.base;
  if (page.scheme.startsWith('http')) {
    final port = page.port == kDefaultHubPort ? page.port : kDefaultHubPort;
    return '${page.scheme}://${page.host}:$port';
  }
  return 'http://localhost:$kDefaultHubPort';
}

class HubApi {
  HubApi({String? base, http.Client? client})
      : base = base ?? defaultApiBase(),
        _client = client ?? http.Client();

  final String base;
  final http.Client _client;

  Uri _url(String path) => Uri.parse('$base$path');

  Future<dynamic> _decode(http.Response response) async {
    if (response.statusCode >= 400) {
      String message = response.body;
      try {
        final decoded = jsonDecode(response.body);
        // FastAPI validation errors arrive as a list of per-field problems;
        // showing the raw JSON would be useless in a snackbar.
        if (decoded is Map && decoded['detail'] != null) {
          final detail = decoded['detail'];
          message = detail is List
              ? detail.map((e) => e is Map ? (e['msg'] ?? e).toString() : e.toString()).join('\n')
              : detail.toString();
        }
      } catch (_) {
        // Keep the raw body.
      }
      throw HubApiException(response.statusCode, message);
    }
    return response.body.isEmpty ? null : jsonDecode(response.body);
  }

  Future<dynamic> _get(String path) async => _decode(await _client.get(_url(path)));

  Future<dynamic> _send(String method, String path, [Object? body]) async {
    final request = http.Request(method, _url(path))
      ..headers['Content-Type'] = 'application/json';
    if (body != null) request.body = jsonEncode(body);
    return _decode(await http.Response.fromStream(await _client.send(request)));
  }

  // ----------------------------------------------------------------------

  Future<HubStatus> status() async =>
      HubStatus.fromJson((await _get('/api/state')) as Map<String, dynamic>);

  Future<List<ButtonInfo>> buttons() async => ((await _get('/api/buttons')) as List)
      .map((e) => ButtonInfo.fromJson(e as Map<String, dynamic>))
      .toList();

  Future<List<BackendInfo>> backends() async => ((await _get('/api/backends')) as List)
      .map((e) => BackendInfo.fromJson(e as Map<String, dynamic>))
      .toList();

  /// Gives names to signatures the remote has been seen to send.
  ///
  /// Naming a key that already exists adds the signature to it rather than
  /// replacing it: one physical button can report differently depending on
  /// the activity, and both signatures are still that button.
  Future<List<ButtonInfo>> learnButtons(List<ButtonInfo> buttons) async =>
      ((await _send('POST', '/api/buttons/learn', {
        'buttons': [for (final b in buttons) b.toJson()]
      })) as List)
          .map((e) => ButtonInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<List<ButtonInfo>> forgetButton(String key) async =>
      ((await _send('DELETE', '/api/buttons/$key')) as List)
          .map((e) => ButtonInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<HubConfig> config() async =>
      HubConfig.fromJson((await _get('/api/config')) as Map<String, dynamic>);

  /// Saves configuration. `force` replaces a file the hub could not read --
  /// which would discard whatever it really holds, so it is never implicit.
  Future<HubConfig> saveConfig(HubConfig config, {bool force = false}) async => HubConfig.fromJson(
      (await _send('PUT', '/api/config${force ? '?force=true' : ''}', config.toJson()))
          as Map<String, dynamic>);

  // ----------------------------------------------------------------------
  // Settings, and the hub's own lifecycle.
  //
  // These keep working when the hub is down, which is the point of them:
  // a hub that will not start is exactly when its settings need changing.
  // ----------------------------------------------------------------------

  Future<HubSettings> settings() async =>
      HubSettings.fromJson((await _get('/api/settings')) as Map<String, dynamic>);

  Future<RuntimeStatus> saveSettings(HubSettings settings, {bool restart = false}) async =>
      RuntimeStatus.fromJson(
          (await _send('PUT', '/api/settings?restart=$restart', settings.toJson()))
              as Map<String, dynamic>);

  Future<RuntimeStatus> _hubAction(String action) async =>
      RuntimeStatus.fromJson((await _send('POST', '/api/hub/$action')) as Map<String, dynamic>);

  Future<RuntimeStatus> startHub() => _hubAction('start');

  Future<RuntimeStatus> stopHub() => _hubAction('stop');

  Future<RuntimeStatus> restartHub() => _hubAction('restart');

  /// Stops button commands from reaching a device without stopping the hub
  /// from hearing them -- presses still show up live. The reply is a plain
  /// `{paused: bool}`, not a `RuntimeStatus`, since [HubStatus.paused] lives
  /// on the engine like [HubStatus.activeScene] does, not on the hub's own
  /// process lifecycle -- so, like those, it is read back via `status()`
  /// rather than trusted from this call's own response.
  Future<void> pauseHub() async => await _send('POST', '/api/hub/pause');

  Future<void> resumeHub() async => await _send('POST', '/api/hub/resume');

  Future<List<HubCheck>> checks() async => ((await _get('/api/checks')) as List)
      .map((e) => HubCheck.fromJson(e as Map<String, dynamic>))
      .toList();

  /// Whether settings would work, without committing them.
  Future<List<HubCheck>> trySettings(HubSettings settings) async =>
      ((await _send('POST', '/api/settings/try', settings.toJson())) as List)
          .map((e) => HubCheck.fromJson(e as Map<String, dynamic>))
          .toList();

  // ----------------------------------------------------------------------
  // Home Assistant, via MQTT. Everything else about the bridge is an
  // ordinary settings field, saved through `saveSettings` above like any
  // other -- only the broker password gets its own routes, for the same
  // reason the Home Assistant *backend*'s access token does.
  // ----------------------------------------------------------------------

  Future<MqttStatus> mqttStatus() async =>
      MqttStatus.fromJson((await _get('/api/mqtt')) as Map<String, dynamic>);

  Future<MqttStatus> setMqttPassword(String password) async => MqttStatus.fromJson(
      (await _send('PUT', '/api/mqtt/password', {'password': password})) as Map<String, dynamic>);

  Future<MqttStatus> clearMqttPassword() async =>
      MqttStatus.fromJson((await _send('DELETE', '/api/mqtt/password')) as Map<String, dynamic>);

  /// Forces a fresh discovery/state publish, for "I don't see it in Home Assistant".
  Future<MqttStatus> republishMqtt() async =>
      MqttStatus.fromJson((await _send('POST', '/api/mqtt/republish')) as Map<String, dynamic>);

  // ----------------------------------------------------------------------
  // Finding the remote's address. A minute-long handshake, so it is a job
  // that gets polled rather than a request that gets answered.
  // ----------------------------------------------------------------------

  /// `method` is 'hub' (needs a real Harmony Hub in pairing mode, quick) or
  /// 'sniff' (no Hub needed -- listens for the remote's own traffic, slower).
  Future<DiscoveryStatus> startDiscovery({String method = 'hub'}) async => DiscoveryStatus.fromJson(
      (await _send('POST', '/api/radio/discover?method=$method')) as Map<String, dynamic>);

  Future<DiscoveryStatus> discoveryStatus() async =>
      DiscoveryStatus.fromJson((await _get('/api/radio/discover')) as Map<String, dynamic>);

  Future<DiscoveryStatus> cancelDiscovery() async => DiscoveryStatus.fromJson(
      (await _send('POST', '/api/radio/discover/cancel')) as Map<String, dynamic>);

  Future<List<CommandInfo>> deviceCommands(String deviceId) async =>
      ((await _get('/api/devices/$deviceId/commands')) as List)
          .map((e) => CommandInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  /// Routes that answer `{ok, detail}` rather than throwing: the hub uses
  /// this shape wherever the failure is the device's, not the request's.
  Future<({bool ok, String detail})> _outcome(String method, String path, [Object? body]) async {
    final result = (await _send(method, path, body)) as Map<String, dynamic>;
    return (ok: result['ok'] as bool, detail: (result['detail'] ?? '') as String);
  }

  Future<({bool ok, String detail})> testCommand(String deviceId, String command) =>
      _outcome('POST', '/api/devices/$deviceId/test', {'command': command});

  /// The buttons this device suggests binding: `bindings` is button key to
  /// command name, same as ever; `adjust` is button key to `"up"`/`"down"`
  /// for the SmartHome +/- keys, which steps whatever the engine is focused
  /// on rather than a command fixed to this device.
  Future<({Map<String, String> bindings, Map<String, String> adjust})> suggestedBindings(
    String deviceId,
  ) async {
    final result = (await _get('/api/devices/$deviceId/suggested_bindings')) as Map<String, dynamic>;
    return (
      bindings: (result['bindings'] as Map).cast<String, String>(),
      adjust: ((result['adjust'] as Map?) ?? const {}).cast<String, String>(),
    );
  }

  /// Asks the device to put its pairing code on screen.
  Future<({bool ok, String detail})> pairStart(String deviceId) =>
      _outcome('POST', '/api/devices/$deviceId/pair/start');

  /// Completes pairing with the code the user read off the screen.
  Future<({bool ok, String detail})> pairFinish(String deviceId, String code) =>
      _outcome('POST', '/api/devices/$deviceId/pair/finish', {'code': code});

  // ----------------------------------------------------------------------
  // Learning IR commands. Polled rather than pushed over the event socket,
  // the same way radio address discovery is -- a dropped websocket
  // mid-capture must not lose it.
  // ----------------------------------------------------------------------

  /// Begins listening for one command. Poll [learnStatus] for progress.
  Future<LearnStatus> startLearn(String deviceId, {double timeout = 20.0}) async => LearnStatus.fromJson(
      (await _send('POST', '/api/devices/$deviceId/learn/start?timeout=$timeout'))
          as Map<String, dynamic>);

  Future<LearnStatus> learnStatus(String deviceId) async =>
      LearnStatus.fromJson((await _get('/api/devices/$deviceId/learn')) as Map<String, dynamic>);

  Future<LearnStatus> cancelLearn(String deviceId) async => LearnStatus.fromJson(
      (await _send('POST', '/api/devices/$deviceId/learn/cancel')) as Map<String, dynamic>);

  /// Replays the most recent capture, so it can be checked before it is named and saved.
  Future<({bool ok, String detail})> verifyLearn(String deviceId) =>
      _outcome('POST', '/api/devices/$deviceId/learn/verify');

  /// Names the most recent capture and adds it to this device's commands.
  Future<List<CommandInfo>> saveLearned(
    String deviceId, {
    required String name,
    required String label,
    bool repeatable = false,
    int repeats = 1,
  }) async =>
      ((await _send('POST', '/api/devices/$deviceId/learn/save', {
        'name': name,
        'label': label,
        'repeatable': repeatable,
        'repeats': repeats,
      })) as List)
          .map((e) => CommandInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<List<CommandInfo>> forgetLearned(String deviceId, String name) async =>
      ((await _send('DELETE', '/api/devices/$deviceId/learn/$name')) as List)
          .map((e) => CommandInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  /// Devices of one backend announcing themselves over mDNS.
  ///
  /// Backend-scoped rather than generic because discovery is protocol
  /// specific: there is no way to ask "what is out there" without knowing
  /// what to listen for.
  Future<List<DiscoveredDevice>> discover(String backend) async =>
      ((await _get('/api/backends/$backend/discover')) as List)
          .map((e) => DiscoveredDevice.fromJson(e as Map<String, dynamic>))
          .toList();

  /// What a device could offer commands for, for picking from.
  ///
  /// `controllableOnly` hides the domains that report rather than obey. The
  /// hub flags them rather than dropping them, so this stays the app's call.
  Future<List<EntityInfo>> deviceEntities(String deviceId, {bool controllableOnly = true}) async =>
      ((await _get('/api/devices/$deviceId/entities?controllable_only=$controllableOnly')) as List)
          .map((e) => EntityInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  /// What a device can report the state of, for a condition's target picker.
  /// Only backends the hub flagged `readable` in `/api/backends` have
  /// anything to answer here.
  Future<List<StateTargetInfo>> deviceReadable(String deviceId) async =>
      ((await _get('/api/devices/$deviceId/readable')) as List)
          .map((e) => StateTargetInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  /// The current value of one state target, read fresh -- for the condition
  /// editor's "currently: ..." readout next to whatever target is picked.
  Future<String> deviceState(String deviceId, String target) async =>
      ((await _get('/api/devices/$deviceId/state/$target')) as Map<String, dynamic>)['value'] as String;

  /// Every value a `set` action has stored in the running engine, for the
  /// `var` value picker to show what is actually available to recall.
  Future<List<VariableInfo>> variables() async =>
      ((await _get('/api/variables')) as List)
          .map((e) => VariableInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  // ----------------------------------------------------------------------
  // Remote update. A signed push from a dev machine with `harmony-deploy`
  // still needs nothing from the app beyond `version`/`updateHistory`/
  // `rollbackUpdate` below. `availableUpdate`/`checkForUpdate`/
  // `installUpdate` are the pull side: the hub checking GitHub on its own,
  // and this app being the thing that triggers installing what it found.
  // Progress for either kind of install arrives through the same event
  // stream as everything else (`HubEvent.type == 'update'`), not polled
  // here.
  // ----------------------------------------------------------------------

  Future<VersionInfo> version() async =>
      VersionInfo.fromJson((await _get('/api/version')) as Map<String, dynamic>);

  Future<List<UpdateHistoryEntry>> updateHistory() async =>
      ((await _get('/api/update/history')) as List)
          .map((e) => UpdateHistoryEntry.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<UpdateResult> rollbackUpdate() async =>
      UpdateResult.fromJson((await _send('POST', '/api/update/rollback')) as Map<String, dynamic>);

  /// The last GitHub release check's result, from cache -- no network request of its own.
  Future<UpdateCheckInfo> availableUpdate() async =>
      UpdateCheckInfo.fromJson((await _get('/api/update/available')) as Map<String, dynamic>);

  /// Asks the hub to check GitHub for a new release now. Throttled server-side
  /// (see `update.check.MIN_MANUAL_CHECK_SECONDS`), so this is safe to call
  /// from a button a person can press repeatedly.
  Future<UpdateCheckInfo> checkForUpdate() async =>
      UpdateCheckInfo.fromJson((await _send('POST', '/api/update/check')) as Map<String, dynamic>);

  /// Installs the release [availableUpdate] last reported. Returns once the
  /// install has *started*, not once it has finished -- watch for `update`
  /// events (or poll [version]) the same way a signed push is watched.
  Future<GithubInstallResult> installUpdate({bool force = false}) async => GithubInstallResult.fromJson(
      (await _send('POST', '/api/update/install${force ? '?force=true' : ''}')) as Map<String, dynamic>);

  Future<void> activateScene(String sceneId) => _send('POST', '/api/scenes/$sceneId/activate');

  Future<void> stopScene() => _send('POST', '/api/scenes/stop');

  Future<void> simulate(String key, {String kind = 'press'}) =>
      _send('POST', '/api/buttons/$key/simulate', {'kind': kind});

  /// Live hub events. The caller is responsible for reconnecting.
  Stream<HubEvent> events() {
    final url = Uri.parse(base).replace(
      scheme: base.startsWith('https') ? 'wss' : 'ws',
      path: '/api/events',
    );
    final channel = WebSocketChannel.connect(url);
    return channel.stream.map((message) => HubEvent.fromJson(
          jsonDecode(message as String) as Map<String, dynamic>,
        ));
  }

  void close() => _client.close();
}
