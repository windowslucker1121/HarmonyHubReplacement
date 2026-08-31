/// Dart mirrors of the hub's API types.
///
/// Hand-written rather than generated: the surface is small and stable, and
/// a code generator would add a build step to a project that deliberately
/// has none.
library;

import 'settings.dart';

/// One physical button on the remote, from `buttons.json`.
class ButtonInfo {
  ButtonInfo({required this.key, required this.label, required this.signatures});

  final String key;
  final String label;
  final List<String> signatures;

  factory ButtonInfo.fromJson(Map<String, dynamic> json) => ButtonInfo(
        key: json['key'] as String,
        label: json['label'] as String,
        signatures: (json['signatures'] as List).cast<String>(),
      );

  /// Sent back when naming a button that the remote has been seen to send.
  Map<String, dynamic> toJson() => {
        'key': key,
        'label': label,
        'signatures': signatures,
      };
}

class DeviceStatus {
  DeviceStatus({
    required this.id,
    required this.name,
    required this.backend,
    required this.running,
    required this.ok,
    required this.detail,
  });

  final String id;
  final String name;
  final String backend;
  final bool running;
  final bool ok;
  final String detail;

  factory DeviceStatus.fromJson(Map<String, dynamic> json) => DeviceStatus(
        id: json['id'] as String,
        name: json['name'] as String,
        backend: json['backend'] as String,
        running: json['running'] as bool,
        ok: json['ok'] as bool,
        detail: (json['detail'] ?? '') as String,
      );
}

class SceneSummary {
  SceneSummary({
    required this.id,
    required this.name,
    this.icon,
    required this.devices,
    required this.boundButtons,
  });

  final String id;
  final String name;
  final String? icon;
  final List<String> devices;
  final int boundButtons;

  factory SceneSummary.fromJson(Map<String, dynamic> json) => SceneSummary(
        id: json['id'] as String,
        name: json['name'] as String,
        icon: json['icon'] as String?,
        devices: (json['devices'] as List).cast<String>(),
        boundButtons: json['bound_buttons'] as int,
      );
}

/// What the remote's SmartHome +/- keys currently follow.
class FocusInfo {
  FocusInfo({
    required this.device,
    required this.target,
    required this.label,
    required this.canAdjust,
  });

  final String device;
  final String target;
  final String label;

  /// Whether the focused target can actually be stepped either way. A
  /// toggled switch takes the focus like anything else does, but +/- would
  /// only ever report "nothing to turn up" for it -- this is what lets the
  /// app show that up front instead of waiting for a press to find out.
  final bool canAdjust;

  factory FocusInfo.fromJson(Map<String, dynamic> json) => FocusInfo(
        device: json['device'] as String,
        target: json['target'] as String,
        label: json['label'] as String,
        canAdjust: (json['can_adjust'] ?? false) as bool,
      );
}

class HubStatus {
  HubStatus({
    this.activeScene,
    required this.scenes,
    required this.devices,
    required this.buttonCount,
    RuntimeStatus? hub,
    this.focus,
    this.paused = false,
  }) : hub = hub ?? RuntimeStatus(state: 'running');

  final String? activeScene;
  final List<SceneSummary> scenes;
  final List<DeviceStatus> devices;
  final int buttonCount;

  /// Whether the hub itself is up. This answers even when it is not, which
  /// is what lets every screen degrade instead of breaking.
  final RuntimeStatus hub;

  /// `null` before anything has claimed the SmartHome keys, or while the
  /// hub is stopped -- the focus lives on the engine, and there is none.
  final FocusInfo? focus;

  /// Whether button presses are being logged but not acted on. Lives on the
  /// engine, like [activeScene] and [focus] -- always false while the hub
  /// is stopped.
  final bool paused;

  factory HubStatus.fromJson(Map<String, dynamic> json) => HubStatus(
        activeScene: json['active_scene'] as String?,
        scenes: (json['scenes'] as List)
            .map((e) => SceneSummary.fromJson(e as Map<String, dynamic>))
            .toList(),
        devices: (json['devices'] as List)
            .map((e) => DeviceStatus.fromJson(e as Map<String, dynamic>))
            .toList(),
        buttonCount: json['button_count'] as int,
        hub: json['hub'] == null
            ? null
            : RuntimeStatus.fromJson((json['hub'] as Map).cast<String, dynamic>()),
        focus: json['focus'] == null
            ? null
            : FocusInfo.fromJson((json['focus'] as Map).cast<String, dynamic>()),
        paused: (json['paused'] ?? false) as bool,
      );
}

/// A backend type, with the JSON Schema that drives its device form.
class BackendInfo {
  BackendInfo({
    required this.name,
    required this.label,
    required this.description,
    required this.configSchema,
    this.pairable = false,
    this.pairLabel = '',
    this.pairHint = '',
    this.pairInputLabel = '',
    this.pairInputMultiline = false,
    this.discoverable = false,
    this.discoverField = '',
    this.learnable = false,
    this.learnLabel = '',
    this.learnHint = '',
    this.learnVerifiable = false,
    this.readable = false,
  });

  final String name;
  final String label;
  final String description;
  final Map<String, dynamic> configSchema;

  /// Whether this backend needs a one-time handshake before it works. Comes
  /// from the hub so the app never learns backend names.
  final bool pairable;

  /// The words to put around that handshake. The mechanism generalises but
  /// the wording does not: a television shows a six-digit code, a Home
  /// Assistant issues a long token from a web page. Hard-coding either would
  /// make the screen lie to everyone using the other.
  final String pairLabel;
  final String pairHint;
  final String pairInputLabel;

  /// A value long enough to want a multi-line box — and, because only a code
  /// is all digits, the same flag decides whether the field is numeric.
  final bool pairInputMultiline;

  /// Whether `/api/backends/{name}/discover` exists for this backend. Comes
  /// from the hub for the same reason `pairable` does: the app should not
  /// keep its own list of which backend names happen to support it.
  final bool discoverable;

  /// Which field in [configSchema] a discovery result's address fills in —
  /// `host` for one backend, `url` for another, decided by the hub rather
  /// than guessed here.
  final String discoverField;

  /// Whether this backend can learn its own commands from a remote. Comes
  /// from the hub for the same reason `pairable` does: the app should not
  /// keep its own list of which backend names happen to support it.
  final bool learnable;

  /// The words to put around a learn attempt — the mechanism generalises
  /// (point a remote at the receiver, press a button, confirm), but nothing
  /// here is backend-specific enough to need more than one wording today.
  /// Kept per-backend anyway, the same way pairing's wording is, so a future
  /// backend with something different to say never has to change this model.
  final String learnLabel;
  final String learnHint;

  /// Whether a capture can be replayed through the transmitter to check it
  /// before saving. False for a receive-only install, where there is
  /// nothing to play it back through.
  final bool learnVerifiable;

  /// Whether `/api/devices/{id}/readable` and `/api/devices/{id}/state/...`
  /// exist for this backend -- comes from the hub for the same reason
  /// `pairable` does: the app should not keep its own list of which
  /// backend names can answer a scene condition's "what is this doing".
  final bool readable;

  /// Top-level schema properties, which is what the generated form renders.
  Map<String, dynamic> get properties =>
      (configSchema['properties'] as Map?)?.cast<String, dynamic>() ?? {};

  factory BackendInfo.fromJson(Map<String, dynamic> json) => BackendInfo(
        name: json['name'] as String,
        label: json['label'] as String,
        description: (json['description'] ?? '') as String,
        configSchema: (json['config_schema'] as Map).cast<String, dynamic>(),
        pairable: (json['pairable'] ?? false) as bool,
        pairLabel: (json['pair_label'] ?? '') as String,
        pairHint: (json['pair_hint'] ?? '') as String,
        pairInputLabel: (json['pair_input_label'] ?? '') as String,
        pairInputMultiline: (json['pair_input_multiline'] ?? false) as bool,
        discoverable: (json['discoverable'] ?? false) as bool,
        discoverField: (json['discover_field'] ?? '') as String,
        learnable: (json['learnable'] ?? false) as bool,
        learnLabel: (json['learn_label'] ?? '') as String,
        learnHint: (json['learn_hint'] ?? '') as String,
        learnVerifiable: (json['learn_verifiable'] ?? false) as bool,
        readable: (json['readable'] ?? false) as bool,
      );
}

/// Where one IR learn job has got to. Mirrors the hub's `LearnStatusInfo`.
class LearnStatus {
  LearnStatus({required this.state, this.detail = '', this.decoded = '', this.pulses = 0});

  /// `idle` / `waiting` / `confirming` / `captured` / `mismatch` / `failed`.
  final String state;
  final String detail;

  /// A best-effort protocol label ("NEC 0x04 0x08"), or empty if none
  /// matched — purely cosmetic, meaningful only once [state] is `captured`.
  final String decoded;
  final int pulses;

  bool get isBusy => state == 'waiting' || state == 'confirming';
  bool get isCaptured => state == 'captured';

  factory LearnStatus.fromJson(Map<String, dynamic> json) => LearnStatus(
        state: json['state'] as String,
        detail: (json['detail'] ?? '') as String,
        decoded: (json['decoded'] ?? '') as String,
        pulses: (json['pulses'] ?? 0) as int,
      );
}

/// Something found by mDNS, so its address does not have to be typed.
class DiscoveredDevice {
  DiscoveredDevice({required this.name, required this.host, this.version = ''});

  final String name;

  /// An address for an Android TV, a full URL for a Home Assistant — either
  /// way, what belongs in the form's address field.
  final String host;
  final String version;

  factory DiscoveredDevice.fromJson(Map<String, dynamic> json) => DiscoveredDevice(
        name: json['name'] as String,
        host: json['host'] as String,
        version: (json['version'] ?? '') as String,
      );
}

/// One thing a device could offer a command for, before anyone has picked it.
///
/// Only Home Assistant has these: it is the one backend whose vocabulary is
/// not fixed but drawn from hundreds of entities that differ in every house.
class EntityInfo {
  EntityInfo({
    required this.entityId,
    required this.name,
    required this.domain,
    this.state = '',
    this.controllable = true,
  });

  final String entityId;
  final String name;
  final String domain;
  final String state;

  /// False for the domains that report rather than obey — a thermometer has
  /// nothing a remote button could do to it.
  final bool controllable;

  factory EntityInfo.fromJson(Map<String, dynamic> json) => EntityInfo(
        entityId: json['entity_id'] as String,
        name: json['name'] as String,
        domain: json['domain'] as String,
        state: (json['state'] ?? '') as String,
        controllable: (json['controllable'] ?? true) as bool,
      );
}

/// One thing a device can report the state of, for the condition editor's
/// target picker -- what `Backend.readable()` offers, mirrored over the
/// wire.
class StateTargetInfo {
  StateTargetInfo({
    required this.target,
    required this.label,
    this.values = const [],
    this.description = '',
  });

  final String target;
  final String label;

  /// Known values this target can take, e.g. `["on", "standby"]` for a
  /// power state -- lets the condition editor offer a dropdown instead of
  /// free text. Empty means the value space is open (a volume level, an
  /// input name) and free text is the only option anyway.
  final List<String> values;
  final String description;

  factory StateTargetInfo.fromJson(Map<String, dynamic> json) => StateTargetInfo(
        target: json['target'] as String,
        label: json['label'] as String,
        values: ((json['values'] ?? []) as List).cast<String>(),
        description: (json['description'] ?? '') as String,
      );
}

/// One value a `set` action has stored, for the live view and the `var`
/// value picker to show what is actually available to recall right now.
class VariableInfo {
  VariableInfo({required this.name, required this.value});

  final String name;
  final String value;

  factory VariableInfo.fromJson(Map<String, dynamic> json) => VariableInfo(
        name: json['name'] as String,
        value: json['value'] as String,
      );
}

class CommandInfo {
  CommandInfo({
    required this.name,
    required this.label,
    required this.description,
    this.repeatable = false,
    this.params,
  });

  final String name;
  final String label;
  final String description;

  /// Whether firing this repeatedly while a button is held makes sense.
  /// Volume should ramp; power must not toggle forty times a second.
  final bool repeatable;

  /// JSON Schema for this command's parameters, or null for a command that
  /// takes none. Drives the action editor's params form -- without this the
  /// editor could offer the command but never its arguments, which is what
  /// left `key`'s direction and `launch_app`'s target unreachable from the
  /// UI even though the backend already accepted them.
  final Map<String, dynamic>? params;

  factory CommandInfo.fromJson(Map<String, dynamic> json) => CommandInfo(
        name: json['name'] as String,
        label: json['label'] as String,
        description: (json['description'] ?? '') as String,
        repeatable: (json['repeatable'] ?? false) as bool,
        params: (json['params'] as Map?)?.cast<String, dynamic>(),
      );
}

/// Something the hub did, streamed over the websocket.
class HubEvent {
  HubEvent({
    required this.type,
    required this.at,
    this.button,
    this.label,
    this.phase,
    this.scene,
    this.fromScene,
    this.action,
    this.ok,
    this.detail,
    Map<String, dynamic>? raw,
  }) : raw = raw ??
            {
              'type': type,
              'button': ?button,
              'label': ?label,
              'phase': ?phase,
              'scene': ?scene,
              'from_scene': ?fromScene,
              'action': ?action,
              'ok': ?ok,
              'detail': ?detail,
            };

  /// button | scene | action | status | hub. "hub" is the runtime starting,
  /// stopping or failing -- the app refetches its state on one of those,
  /// where "status" is just the running hub reporting something it did.
  final String type;
  final DateTime at;
  final String? button;
  final String? label;
  final String? phase;
  final String? scene;

  /// For a `"scene"` event, the scene being left -- `null` if none was
  /// running (starting from idle) or a plain stop had nothing incoming to
  /// report. The pair `TransitionValue` resolves inside the macros this
  /// same switch runs.
  final String? fromScene;
  final String? action;
  final bool? ok;
  final String? detail;

  /// The event exactly as decoded, including any field this class does not
  /// otherwise know about. What lets the activity filter offer a new
  /// dimension the day the hub starts sending one, without a Dart change.
  final Map<String, dynamic> raw;

  bool get isFailure => ok == false;

  factory HubEvent.fromJson(Map<String, dynamic> json) => HubEvent(
        type: json['type'] as String,
        at: DateTime.tryParse((json['at'] ?? '') as String) ?? DateTime.now(),
        button: json['button'] as String?,
        label: json['label'] as String?,
        phase: json['phase'] as String?,
        scene: json['scene'] as String?,
        fromScene: json['from_scene'] as String?,
        action: json['action'] as String?,
        ok: json['ok'] as bool?,
        detail: json['detail'] as String?,
        raw: json,
      );

  String get summary {
    switch (type) {
      case 'button':
        final tag = detail == 'unbound' ? '  (unbound)' : detail == 'paused' ? '  (paused)' : '';
        return '${label ?? button} ${phase ?? ''}$tag';
      case 'scene':
        final base = detail ?? (scene ?? 'scene');
        // Only a switch (not a plain stop, and not starting from idle) has
        // both ends worth naming -- `detail` alone already says "Started
        // X"/"Stopped X", so this is purely the "from" half it leaves out.
        return (scene != null && fromScene != null) ? '$base (from $fromScene)' : base;
      case 'action':
        return '${action ?? ''}${detail != null ? '  ($detail)' : ''}';
      default:
        return detail ?? type;
    }
  }

  /// Fields this event can be filtered by, stringified. `at`, `detail` and
  /// `label` are left out -- they carry free text or a timestamp, which
  /// would each produce their own chip instead of grouping events into a
  /// handful of values.
  static const _excludedFacets = {'at', 'detail', 'label'};

  Map<String, String> get facets {
    final out = <String, String>{};
    for (final entry in raw.entries) {
      if (_excludedFacets.contains(entry.key)) continue;
      final value = entry.value;
      if (value == null || value is Map || value is List) continue;
      out[entry.key] = value.toString();
    }
    return out;
  }

  /// Free text searched by the activity filter's search box.
  String get searchText => [type, button, label, phase, scene, fromScene, action, detail]
      .where((e) => e != null)
      .join(' ')
      .toLowerCase();
}
