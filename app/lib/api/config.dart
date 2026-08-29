/// The editable configuration: devices, scenes, bindings, actions.
///
/// Mirrors `harmony_hub.models` on the Python side. Kept separate from
/// `models.dart` because these round-trip *back* to the server, so every
/// class needs a faithful `toJson` as well -- a field silently dropped here
/// would be a field silently deleted from someone's configuration.
library;

/// One step in a macro: send a command, switch scene, wait, or step
/// whatever the remote is currently focused on.
class HubAction {
  HubAction({
    required this.type,
    this.device,
    this.command,
    this.params,
    this.scene,
    this.seconds,
    this.direction,
    this.target,
  });

  final String type; // device | scene | delay | adjust
  String? device;
  String? command;
  Map<String, dynamic>? params;
  String? scene;
  double? seconds;

  /// "up" or "down", for an `adjust` action.
  String? direction;

  /// Backend-private fallback target for an `adjust` action with nothing
  /// touched yet -- a Home Assistant entity id, say. Only meaningful
  /// alongside [device]; one without the other cannot be resolved.
  String? target;

  factory HubAction.device(String device, String command) =>
      HubAction(type: 'device', device: device, command: command, params: {});

  factory HubAction.scene(String? scene) => HubAction(type: 'scene', scene: scene);

  factory HubAction.delay(double seconds) => HubAction(type: 'delay', seconds: seconds);

  factory HubAction.adjust(String direction, {String? device, String? target}) =>
      HubAction(type: 'adjust', direction: direction, device: device, target: target);

  factory HubAction.fromJson(Map<String, dynamic> json) => HubAction(
        type: json['type'] as String,
        device: json['device'] as String?,
        command: json['command'] as String?,
        params: (json['params'] as Map?)?.cast<String, dynamic>(),
        scene: json['scene'] as String?,
        seconds: (json['seconds'] as num?)?.toDouble(),
        direction: json['direction'] as String?,
        target: json['target'] as String?,
      );

  /// Only the keys belonging to this action's type: the server rejects
  /// unknown fields, so a `device` key on a delay action would be a 422.
  Map<String, dynamic> toJson() {
    switch (type) {
      case 'device':
        return {'type': 'device', 'device': device, 'command': command, 'params': params ?? {}};
      case 'scene':
        return {'type': 'scene', 'scene': scene};
      case 'adjust':
        return {'type': 'adjust', 'direction': direction, 'device': device, 'target': target};
      default:
        return {'type': 'delay', 'seconds': seconds ?? 1.0};
    }
  }

  String describe() {
    switch (type) {
      case 'device':
        return '$device → $command';
      case 'scene':
        return scene == null ? 'Stop the active scene' : 'Start scene "$scene"';
      case 'adjust':
        final arrow = direction == 'down' ? 'Turn down' : 'Turn up';
        return device != null && target != null
            ? '$arrow (follows the last device touched, or $device if nothing has yet)'
            : '$arrow (follows the last device touched)';
      default:
        return 'Wait ${seconds ?? 1}s';
    }
  }

  HubAction copy() => HubAction.fromJson(toJson());
}

/// What one button does, split by the phase of the press.
class Binding {
  Binding({
    List<HubAction>? onPress,
    List<HubAction>? onRepeat,
    List<HubAction>? onHold,
    List<HubAction>? onRelease,
    this.holdSeconds = 0.6,
    this.repeatDelay,
    this.repeatInterval,
  })  : onPress = onPress ?? [],
        onRepeat = onRepeat ?? [],
        onHold = onHold ?? [],
        onRelease = onRelease ?? [];

  List<HubAction> onPress;
  List<HubAction> onRepeat;
  List<HubAction> onHold;
  List<HubAction> onRelease;
  double holdSeconds;

  /// How long the button must be held before [onRepeat] starts firing.
  ///
  /// The remote reports a held button every ~100ms and never says how long
  /// it has been down, so without this an ordinary short press fires the
  /// repeat actions three or four times over.
  ///
  /// `null` (the common case) follows [HubConfig.defaultRepeatDelay] rather
  /// than every button carrying its own copy of a value that is almost
  /// always the same. Set this to override just one button.
  double? repeatDelay;

  /// Minimum gap between repeats once they have started. `null` follows
  /// [HubConfig.defaultRepeatInterval].
  double? repeatInterval;

  bool get isEmpty =>
      onPress.isEmpty && onRepeat.isEmpty && onHold.isEmpty && onRelease.isEmpty;

  int get actionCount =>
      onPress.length + onRepeat.length + onHold.length + onRelease.length;

  List<HubAction> phase(String name) => switch (name) {
        'press' => onPress,
        'repeat' => onRepeat,
        'hold' => onHold,
        _ => onRelease,
      };

  /// A one-line description for list rows, favouring the press action since
  /// that is what a button "does" in the common case.
  String get summary {
    if (onPress.isNotEmpty) return onPress.first.describe();
    if (onHold.isNotEmpty) return 'hold: ${onHold.first.describe()}';
    if (onRepeat.isNotEmpty) return 'repeat: ${onRepeat.first.describe()}';
    if (onRelease.isNotEmpty) return 'release: ${onRelease.first.describe()}';
    return 'nothing';
  }

  static List<HubAction> _list(dynamic value) =>
      ((value ?? []) as List).map((e) => HubAction.fromJson(e as Map<String, dynamic>)).toList();

  factory Binding.fromJson(Map<String, dynamic> json) => Binding(
        onPress: _list(json['on_press']),
        onRepeat: _list(json['on_repeat']),
        onHold: _list(json['on_hold']),
        onRelease: _list(json['on_release']),
        holdSeconds: (json['hold_seconds'] as num?)?.toDouble() ?? 0.6,
        repeatDelay: (json['repeat_delay'] as num?)?.toDouble(),
        repeatInterval: (json['repeat_interval'] as num?)?.toDouble(),
      );

  Map<String, dynamic> toJson() => {
        'on_press': onPress.map((a) => a.toJson()).toList(),
        'on_repeat': onRepeat.map((a) => a.toJson()).toList(),
        'on_hold': onHold.map((a) => a.toJson()).toList(),
        'on_release': onRelease.map((a) => a.toJson()).toList(),
        'hold_seconds': holdSeconds,
        'repeat_delay': repeatDelay,
        'repeat_interval': repeatInterval,
      };

  Binding copy() => Binding.fromJson(toJson());
}

class DeviceConfig {
  DeviceConfig({
    required this.id,
    required this.name,
    required this.backend,
    Map<String, dynamic>? config,
    this.powerPolicy = 'managed',
    this.powerOnCommand,
    this.powerOffCommand,
  }) : config = config ?? {};

  String id;
  String name;
  String backend;
  Map<String, dynamic> config;
  String powerPolicy;
  String? powerOnCommand;
  String? powerOffCommand;

  factory DeviceConfig.fromJson(Map<String, dynamic> json) => DeviceConfig(
        id: json['id'] as String,
        name: json['name'] as String,
        backend: json['backend'] as String,
        config: (json['config'] as Map?)?.cast<String, dynamic>() ?? {},
        powerPolicy: (json['power_policy'] ?? 'managed') as String,
        powerOnCommand: json['power_on_command'] as String?,
        powerOffCommand: json['power_off_command'] as String?,
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'backend': backend,
        'config': config,
        'power_policy': powerPolicy,
        'power_on_command': powerOnCommand,
        'power_off_command': powerOffCommand,
      };

  DeviceConfig copy() => DeviceConfig.fromJson(toJson());
}

class SceneConfig {
  SceneConfig({
    required this.id,
    required this.name,
    this.icon,
    List<String>? devices,
    List<HubAction>? onStart,
    List<HubAction>? onStop,
    Map<String, Binding>? bindings,
  })  : devices = devices ?? [],
        onStart = onStart ?? [],
        onStop = onStop ?? [],
        bindings = bindings ?? {};

  String id;
  String name;
  String? icon;
  List<String> devices;
  List<HubAction> onStart;
  List<HubAction> onStop;
  Map<String, Binding> bindings;

  factory SceneConfig.fromJson(Map<String, dynamic> json) => SceneConfig(
        id: json['id'] as String,
        name: json['name'] as String,
        icon: json['icon'] as String?,
        devices: ((json['devices'] ?? []) as List).cast<String>(),
        onStart: Binding._list(json['on_start']),
        onStop: Binding._list(json['on_stop']),
        bindings: ((json['bindings'] ?? {}) as Map).map(
          (key, value) => MapEntry(key as String, Binding.fromJson((value as Map).cast<String, dynamic>())),
        ),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'icon': icon,
        'devices': devices,
        'on_start': onStart.map((a) => a.toJson()).toList(),
        'on_stop': onStop.map((a) => a.toJson()).toList(),
        'bindings': bindings.map((key, value) => MapEntry(key, value.toJson())),
      };

  SceneConfig copy() => SceneConfig.fromJson(toJson());
}

class HubConfig {
  HubConfig({
    this.version = 1,
    List<DeviceConfig>? devices,
    List<SceneConfig>? scenes,
    this.globalScene,
    this.defaultScene,
    this.defaultRepeatDelay = 0.5,
    this.defaultRepeatInterval = 0.0,
  })  : devices = devices ?? [],
        scenes = scenes ?? [];

  int version;
  List<DeviceConfig> devices;
  List<SceneConfig> scenes;

  /// The scene whose bindings fall back into effect for a button the active
  /// scene does not bind, and the only bindings that work when idle. A
  /// reference to an ordinary scene, not a separate configurable set.
  String? globalScene;
  String? defaultScene;

  /// The repeat timing every [Binding] follows unless it sets its own. One
  /// setting for the whole remote instead of a copy on every button that
  /// repeats, since in practice they are almost always the same number.
  double defaultRepeatDelay;
  double defaultRepeatInterval;

  DeviceConfig? device(String id) => devices.where((d) => d.id == id).firstOrNull;

  SceneConfig? scene(String id) => scenes.where((s) => s.id == id).firstOrNull;

  factory HubConfig.fromJson(Map<String, dynamic> json) => HubConfig(
        version: (json['version'] ?? 1) as int,
        devices: ((json['devices'] ?? []) as List)
            .map((e) => DeviceConfig.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        scenes: ((json['scenes'] ?? []) as List)
            .map((e) => SceneConfig.fromJson((e as Map).cast<String, dynamic>()))
            .toList(),
        globalScene: json['global_scene'] as String?,
        defaultScene: json['default_scene'] as String?,
        defaultRepeatDelay: (json['default_repeat_delay'] as num?)?.toDouble() ?? 0.5,
        defaultRepeatInterval: (json['default_repeat_interval'] as num?)?.toDouble() ?? 0.0,
      );

  Map<String, dynamic> toJson() => {
        'version': version,
        'devices': devices.map((d) => d.toJson()).toList(),
        'scenes': scenes.map((s) => s.toJson()).toList(),
        'global_scene': globalScene,
        'default_scene': defaultScene,
        'default_repeat_delay': defaultRepeatDelay,
        'default_repeat_interval': defaultRepeatInterval,
      };

  HubConfig copy() => HubConfig.fromJson(toJson());
}
