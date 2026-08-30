/// Application state: everything the screens read, in one listenable.
///
/// Deliberately a plain `ChangeNotifier` rather than a state-management
/// package. The app has one data source and a handful of screens, so a
/// dependency would buy nothing and cost a concept every future reader has
/// to learn.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api/client.dart';
import '../api/config.dart';
import '../api/models.dart';
import '../api/settings.dart';
import 'activity_filter.dart';
import 'ui_prefs.dart';

/// How long a button stays lit in the live view after being pressed.
const Duration kFlashDuration = Duration(milliseconds: 450);

/// Rolling window of events kept for the activity log.
const int kEventLimit = 300;

class HubStore extends ChangeNotifier {
  HubStore({HubApi? api, UiPrefs? prefs})
      : api = api ?? HubApi(),
        _prefs = prefs {
    // Hydrated once, from whatever was last saved -- absent (a test with no
    // prefs, or nothing saved yet) leaves the filter at its own defaults.
    activityFilter.applyJson(prefs?.get(kActivityFilter));
  }

  final HubApi api;
  final UiPrefs? _prefs;

  HubStatus? status;
  HubConfig? config;
  HubSettings? settings;
  VersionInfo? version;
  UpdateCheckInfo? availableUpdate;
  List<ButtonInfo> buttons = [];
  List<BackendInfo> backends = [];
  final List<HubEvent> events = [];

  /// How the activity log's events are filtered and sorted. Lives here
  /// rather than on the screen so it survives switching tabs and back, and
  /// is persisted through [_prefs] so it survives a reload too.
  final ActivityFilter activityFilter = ActivityFilter();

  /// [events], filtered and sorted for display.
  List<HubEvent> get visibleEvents => activityFilter.apply(events);

  /// Mutates [activityFilter], notifies listeners, and persists the result
  /// -- so the log updates as the filter popup is used rather than only
  /// once it is closed, and the choice survives a reload either way.
  void updateActivityFilter(void Function(ActivityFilter filter) mutate) {
    mutate(activityFilter);
    _prefs?.set(kActivityFilter, activityFilter.toJson());
    notifyListeners();
  }

  bool loading = true;
  bool connected = false;
  String? error;

  /// Whether an install this app kicked off from GitHub (as opposed to a
  /// signed push from a dev machine) is still running. The hub stays fully
  /// reachable the whole time -- downloading and `pip install`ing on a Pi
  /// can take minutes -- so unlike a rollback, which restarts almost
  /// immediately, nothing else would tell a person this is still happening.
  bool installingUpdate = false;

  /// The most recent `update`-type event's detail, for [installingUpdate]
  /// to show as progress. `events` is newest-first, so the first match is
  /// the most recent.
  String? get updateProgressDetail {
    for (final event in events) {
      if (event.type == 'update') return event.detail;
    }
    return null;
  }

  /// Whether the hub itself is up. Every screen reads this to decide between
  /// working normally and explaining why it cannot -- the app stays usable
  /// either way, because configuring a stopped hub is the whole point of it.
  RuntimeStatus get runtime => status?.hub ?? RuntimeStatus(state: 'stopped');

  bool get hubRunning => runtime.isRunning;

  /// When each button was last pressed, so the live view can light it up.
  final Map<String, DateTime> _flashes = {};
  StreamSubscription<HubEvent>? _eventSubscription;
  Timer? _reconnectTimer;
  Timer? _flashTimer;

  bool isFlashing(String key) {
    final at = _flashes[key];
    return at != null && DateTime.now().difference(at) < kFlashDuration;
  }

  String get baseUrl => api.base;

  // ----------------------------------------------------------------------

  Future<void> load() async {
    loading = true;
    notifyListeners();
    try {
      final results = await Future.wait([
        api.status(),
        api.buttons(),
        api.backends(),
        api.config(),
      ]);
      status = results[0] as HubStatus;
      buttons = results[1] as List<ButtonInfo>;
      backends = results[2] as List<BackendInfo>;
      config = results[3] as HubConfig;
      error = null;
    } catch (err) {
      error = '$err';
    }

    // Fetched separately on purpose. These four share a `Future.wait`, where
    // one failure discards all of them -- and the settings screen is the one
    // thing that must still work when something else is broken.
    try {
      settings = await api.settings();
    } catch (_) {
      // An older hub, or a transient failure. The rest of the app is fine.
    }

    try {
      version = await api.version();
    } catch (_) {
      // An older hub with no /api/version yet. The Software card just stays hidden.
    }

    try {
      availableUpdate = await api.availableUpdate();
    } catch (_) {
      // Not deployed, or an older hub with no /api/update/available yet.
    }

    loading = false;
    notifyListeners();
  }

  /// Refreshes just the parts that change on their own.
  Future<void> refreshStatus() async {
    try {
      status = await api.status();
      error = null;
    } catch (err) {
      error = '$err';
    }
    notifyListeners();
  }

  // ----------------------------------------------------------------------

  /// Subscribes to the live event stream, reconnecting if it drops.
  ///
  /// A hub that has been restarted, or a phone coming back from sleep, must
  /// recover on its own -- an app that silently stops updating looks
  /// identical to one where nobody is pressing anything.
  void connect() {
    _eventSubscription?.cancel();
    _reconnectTimer?.cancel();
    try {
      _eventSubscription = api.events().listen(
            _onEvent,
            onError: (_) => _scheduleReconnect(),
            onDone: _scheduleReconnect,
          );
      connected = true;
      notifyListeners();
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    if (connected) {
      connected = false;
      notifyListeners();
    }
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 2), connect);
  }

  void _onEvent(HubEvent event) {
    events.insert(0, event);
    if (events.length > kEventLimit) events.removeRange(kEventLimit, events.length);

    if (event.type == 'button' && event.button != null) {
      _flashes[event.button!] = DateTime.now();
      // One timer clears the highlight; without it the button would stay lit
      // until some unrelated event happened to rebuild the widget.
      _flashTimer?.cancel();
      _flashTimer = Timer(kFlashDuration, notifyListeners);
    }

    // The hub starting, stopping or failing changes what every screen may
    // do, and it can happen without this client having asked -- a radio that
    // dropped, or another browser tab hitting Stop. "status" covers the
    // same ground for everything smaller than that, including the engine
    // announcing that the SmartHome +/- keys now follow something new --
    // there is no per-field push, so a full state refetch is what picks it
    // up promptly instead of waiting for whatever next touches `status`.
    if (event.type == 'hub' || event.type == 'status') {
      refreshStatus();
    }

    // A "hub" event right after a reconnect is what a restart-onto-new-code
    // actually looks like from here: this process's own event history is
    // gone (a new process means a new, empty broker), so its first "hub"
    // event is the new one starting up. That is the moment `version` can
    // have actually changed, which is why this is event-driven rather than
    // a fixed delay after asking for a rollback -- there is no way to know
    // from here how long a restart will actually take.
    if (event.type == 'hub') {
      refreshVersion();
      // A restart is exactly the moment "what's installed" may have moved,
      // so what's still worth offering has to be recomputed against it --
      // and, whatever kicked this restart off, an install this app was
      // tracking is no longer "in progress" once it happens.
      refreshAvailableUpdate();
      installingUpdate = false;
    }

    // An install that fails never restarts, so `installingUpdate` has to be
    // cleared here too -- not just on the "hub" event above -- or the
    // Software card would show "Installing…" forever after a failure.
    if (event.type == 'update' && event.ok == false) {
      installingUpdate = false;
    }

    // The engine is the authority on the active scene, so follow its events
    // rather than assuming our own requests succeeded.
    if (event.type == 'scene' && status != null) {
      status = HubStatus(
        activeScene: event.scene,
        scenes: status!.scenes,
        devices: status!.devices,
        buttonCount: status!.buttonCount,
        hub: status!.hub,
        // Unrelated to which scene is running -- both survive a scene
        // switch on the engine side, so the local patch must not drop them
        // (defaulting `paused` back to false) while waiting on the "status"
        // refetch above -- HubStatus() defaults it to false when omitted.
        focus: status!.focus,
        paused: status!.paused,
      );
    }
    notifyListeners();
  }

  // ----------------------------------------------------------------------

  Future<void> activateScene(String sceneId) => _guard(() => api.activateScene(sceneId));

  Future<void> stopScene() => _guard(() => api.stopScene());

  Future<void> simulate(String key, {String kind = 'press'}) =>
      _guard(() => api.simulate(key, kind: kind));

  /// Presses and immediately releases, the way a real tap behaves.
  ///
  /// Sending only a press would leave the engine believing the button is
  /// still held, which matters for any binding that uses hold or release.
  Future<void> tap(String key) async {
    await simulate(key, kind: 'press');
    await simulate(key, kind: 'release');
  }

  /// Every button key the hub can name. Anything the remote sends that is
  /// not in here arrives under its own raw signature, which is what the
  /// learning screen picks up on.
  Set<String> get knownButtonKeys => {for (final b in buttons) b.key};

  /// Names signatures the remote has been seen to send.
  Future<bool> learnButtons(List<ButtonInfo> learned) async {
    try {
      buttons = await api.learnButtons(learned);
      status = await api.status();
      error = null;
      notifyListeners();
      return true;
    } catch (err) {
      error = '$err';
      notifyListeners();
      return false;
    }
  }

  Future<bool> forgetButton(String key) async {
    try {
      buttons = await api.forgetButton(key);
      status = await api.status();
      error = null;
      notifyListeners();
      return true;
    } catch (err) {
      error = '$err';
      notifyListeners();
      return false;
    }
  }

  Future<bool> saveConfig(HubConfig updated) async {
    try {
      config = await api.saveConfig(updated);
      status = await api.status();
      error = null;
      notifyListeners();
      return true;
    } catch (err) {
      error = '$err';
      notifyListeners();
      return false;
    }
  }

  // ----------------------------------------------------------------------
  // The hub's own lifecycle.
  //
  // Each of these refreshes status afterwards rather than trusting the
  // reply, for the same reason scene events are followed rather than
  // assumed: the runtime is the authority on whether it came up.
  // ----------------------------------------------------------------------

  Future<void> startHub() => _lifecycle(api.startHub);

  Future<void> stopHub() => _lifecycle(api.stopHub);

  Future<void> restartHub() => _lifecycle(api.restartHub);

  /// Whether button presses are logged but not acted on right now.
  bool get paused => status?.paused ?? false;

  Future<void> pauseHub() => _lifecycle(api.pauseHub);

  Future<void> resumeHub() => _lifecycle(api.resumeHub);

  /// `action`'s own return value is never used -- `status()` is re-fetched
  /// regardless, the runtime being the authority on what actually happened
  /// -- so this also serves `pauseHub`/`resumeHub`, whose `paused` lives on
  /// [HubStatus] rather than the [RuntimeStatus] the other three return.
  Future<void> _lifecycle(Future<void> Function() action) async {
    try {
      await action();
      status = await api.status();
      error = null;
    } catch (err) {
      error = '$err';
    }
    notifyListeners();
  }

  /// Saves settings, optionally restarting the hub onto them.
  ///
  /// Returns whether it saved. A rejected value leaves the previous settings
  /// live, so the form can keep the text that caused it and let it be fixed.
  Future<bool> saveSettings(HubSettings updated, {bool restart = false}) async {
    try {
      await api.saveSettings(updated, restart: restart);
      settings = await api.settings();
      status = await api.status();
      error = null;
      notifyListeners();
      return true;
    } catch (err) {
      error = '$err';
      notifyListeners();
      return false;
    }
  }

  // ----------------------------------------------------------------------
  // Remote update. A signed push from a dev machine still needs nothing
  // from here beyond showing what is running and offering a rollback --
  // see `refreshVersion`/`loadUpdateHistory`/`rollbackUpdate` below.
  // `refreshAvailableUpdate`/`checkForUpdate`/`installUpdate` are the pull
  // side: a release the hub found on GitHub on its own, and this app being
  // what triggers installing it.
  // ----------------------------------------------------------------------

  /// Refreshes just [version], e.g. after reconnecting once a push restarts the hub.
  Future<void> refreshVersion() async {
    try {
      version = await api.version();
      error = null;
    } catch (err) {
      error = '$err';
    }
    notifyListeners();
  }

  Future<List<UpdateHistoryEntry>> loadUpdateHistory() async {
    try {
      return await api.updateHistory();
    } catch (_) {
      return const [];
    }
  }

  /// Activates the previous release and restarts onto it. The hub goes down
  /// briefly to do this; [_onEvent] refreshes [version] once the websocket
  /// reconnects and sees the new process's own first "hub" event, so there
  /// is nothing more to do here than kick the rollback off.
  Future<bool> rollbackUpdate() async {
    try {
      await api.rollbackUpdate();
      error = null;
      notifyListeners();
      return true;
    } catch (err) {
      error = '$err';
      notifyListeners();
      return false;
    }
  }

  /// Refreshes just [availableUpdate], from cache -- no network request of its own.
  Future<void> refreshAvailableUpdate() async {
    try {
      availableUpdate = await api.availableUpdate();
    } catch (_) {
      // Not deployed, GitHub updates are off, or an older hub. Left as-is.
    }
    notifyListeners();
  }

  /// Asks the hub to check GitHub for a new release right now.
  Future<void> checkForUpdate() async {
    try {
      availableUpdate = await api.checkForUpdate();
      error = null;
    } catch (err) {
      error = '$err';
    }
    notifyListeners();
  }

  /// Starts installing [availableUpdate]. Returns once the install has
  /// *started*, not once it has finished -- [installingUpdate] and
  /// [updateProgressDetail] track the rest, driven by `update` events as
  /// they arrive over the same event stream everything else uses.
  Future<bool> installUpdate({bool force = false}) async {
    try {
      await api.installUpdate(force: force);
      installingUpdate = true;
      error = null;
      notifyListeners();
      return true;
    } catch (err) {
      error = '$err';
      notifyListeners();
      return false;
    }
  }

  /// Marks [availableUpdate]'s release as dismissed for this browser -- see
  /// [kDismissedUpdateBuildId]. A later, *different* release still shows.
  void dismissUpdateBanner() {
    final buildId = availableUpdate?.available?.buildId;
    if (buildId == null) return;
    _prefs?.set(kDismissedUpdateBuildId, buildId);
    notifyListeners();
  }

  /// Whether [availableUpdate] names a release this browser has not already dismissed.
  bool get hasUndismissedUpdate {
    final buildId = availableUpdate?.available?.buildId;
    if (buildId == null) return false;
    return _prefs?.get(kDismissedUpdateBuildId) != buildId;
  }

  Future<void> _guard(Future<void> Function() action) async {
    try {
      await action();
      error = null;
    } catch (err) {
      error = '$err';
    }
    notifyListeners();
  }

  void clearError() {
    if (error == null) return;
    error = null;
    notifyListeners();
  }

  @override
  void dispose() {
    _eventSubscription?.cancel();
    _reconnectTimer?.cancel();
    _flashTimer?.cancel();
    api.close();
    super.dispose();
  }
}
