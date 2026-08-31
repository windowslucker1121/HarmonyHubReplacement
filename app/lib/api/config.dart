/// The editable configuration: devices, scenes, bindings, actions.
///
/// Mirrors `harmony_hub.models` on the Python side. Kept separate from
/// `models.dart` because these round-trip *back* to the server, so every
/// class needs a faithful `toJson` as well -- a field silently dropped here
/// would be a field silently deleted from someone's configuration.
library;

/// Shared by [HubAction] (`then`/`otherwise`), [Binding] and [SceneConfig] --
/// every place a JSON array of actions round-trips through this file.
List<HubAction> _actionList(dynamic value) =>
    ((value ?? []) as List).map((e) => HubAction.fromJson(e as Map<String, dynamic>)).toList();

/// One side of a [HubCondition], or the source a `set` action stores --
/// a device's live state, a previously-`set` variable, a value fixed right
/// here in configuration, or which scene a switch is moving from/to. All
/// four round-trip through the same shape so a condition's two sides, or a
/// restore parameter, are never a special case depending on which kind of
/// value they happen to be.
class HubValue {
  HubValue({required this.type, this.device, this.target, this.name, this.value, this.edge});

  /// 'state' | 'var' | 'literal' | 'transition'
  String type;

  /// A 'state' value's device and target -- what `Backend.read_state` takes.
  String? device;
  String? target;

  /// A 'var' value's name, as a previous `set` action stored it.
  String? name;

  /// A 'literal' value's fixed text.
  String? value;

  /// A 'transition' value's side: 'from' (the scene being left) or 'to'
  /// (the one being entered). Meaningful only while a scene switch is
  /// under way -- an `on_start`/`on_stop` macro -- and resolves to `""`
  /// (never unreadable) everywhere else, including a plain button binding.
  String? edge;

  factory HubValue.state(String device, String target) =>
      HubValue(type: 'state', device: device, target: target);

  factory HubValue.variable(String name) => HubValue(type: 'var', name: name);

  factory HubValue.literal(String value) => HubValue(type: 'literal', value: value);

  factory HubValue.transition(String edge) => HubValue(type: 'transition', edge: edge);

  factory HubValue.fromJson(Map<String, dynamic> json) => HubValue(
        type: json['type'] as String,
        device: json['device'] as String?,
        target: json['target'] as String?,
        name: json['name'] as String?,
        value: json['value'] as String?,
        edge: json['edge'] as String?,
      );

  /// Only the keys belonging to this value's type -- the server rejects
  /// unknown fields the same way `HubAction.toJson` has to avoid them.
  Map<String, dynamic> toJson() => switch (type) {
        'state' => {'type': 'state', 'device': device, 'target': target},
        'var' => {'type': 'var', 'name': name},
        'transition' => {'type': 'transition', 'edge': edge ?? 'from'},
        _ => {'type': 'literal', 'value': value ?? ''},
      };

  String describe() => switch (type) {
        'state' => '${device ?? '?'}.${target ?? '?'}',
        'var' => '\$${name ?? '?'}',
        'transition' => edge == 'to' ? 'scene switching to' : 'scene switching from',
        _ => "'${value ?? ''}'",
      };

  HubValue copy() => HubValue.fromJson(toJson());
}

/// One thing an `if` or `wait_for` action checks before running.
class HubCondition {
  HubCondition({required this.left, this.op = 'is', this.right, this.onUnreadable = 'run'});

  HubValue left;

  /// 'is' | 'is_not' | 'contains' | 'in' | 'gt' | 'lt' | 'known' | 'unknown'
  String op;

  /// Unused for 'known'/'unknown', which ask whether [left] could be read
  /// at all rather than comparing it to anything.
  HubValue? right;

  /// 'run' | 'skip' -- what to do when [left] (or [right]) cannot be read:
  /// the device is offline, or a `var` was never set. 'run' matches the
  /// engine's behaviour before conditions existed, and is the default.
  String onUnreadable;

  static const _opsWithoutRight = {'known', 'unknown'};

  bool get needsRight => !_opsWithoutRight.contains(op);

  factory HubCondition.fromJson(Map<String, dynamic> json) => HubCondition(
        left: HubValue.fromJson((json['left'] as Map).cast<String, dynamic>()),
        op: (json['op'] ?? 'is') as String,
        right: json['right'] == null
            ? null
            : HubValue.fromJson((json['right'] as Map).cast<String, dynamic>()),
        onUnreadable: (json['on_unreadable'] ?? 'run') as String,
      );

  Map<String, dynamic> toJson() => {
        'left': left.toJson(),
        'op': op,
        'right': needsRight ? right?.toJson() : null,
        'on_unreadable': onUnreadable,
      };

  String describe() =>
      needsRight ? '${left.describe()} $op ${right?.describe() ?? '?'}' : '${left.describe()} is $op';

  HubCondition copy() => HubCondition.fromJson(toJson());
}

/// One step in a macro: send a command, switch scene, wait, step whatever
/// the remote is currently focused on, branch on a condition, remember a
/// value, or wait for a condition to come true.
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
    this.condition,
    List<HubAction>? then,
    List<HubAction>? otherwise,
    this.varName,
    this.value,
    this.timeout,
    this.poll,
    this.onTimeout,
  })  : then = then ?? [],
        otherwise = otherwise ?? [];

  final String type; // device | scene | delay | adjust | if | set | wait_for
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

  /// What an `if` or `wait_for` action checks.
  HubCondition? condition;

  /// An `if` action's two branches -- run [then] when [condition] holds,
  /// [otherwise] when it does not.
  List<HubAction> then;
  List<HubAction> otherwise;

  /// A `set` action's variable name and the value stored under it. Named
  /// `varName` rather than `name` to keep this readable as one field among
  /// many on a multi-purpose action, even though the wire key is `name`.
  String? varName;
  HubValue? value;

  /// A `wait_for` action's timeout and poll interval, in seconds, and what
  /// to do once [timeout] runs out without [condition] holding -- 'continue'
  /// (run the rest of the macro anyway) or 'stop' (log it as a failure;
  /// nothing in the engine lets one action's failure cancel its siblings,
  /// so this changes how it is reported, not whether the macro keeps going).
  double? timeout;
  double? poll;
  String? onTimeout;

  factory HubAction.device(String device, String command) =>
      HubAction(type: 'device', device: device, command: command, params: {});

  factory HubAction.scene(String? scene) => HubAction(type: 'scene', scene: scene);

  factory HubAction.delay(double seconds) => HubAction(type: 'delay', seconds: seconds);

  factory HubAction.adjust(String direction, {String? device, String? target}) =>
      HubAction(type: 'adjust', direction: direction, device: device, target: target);

  factory HubAction.ifAction(HubCondition condition, {List<HubAction>? then, List<HubAction>? otherwise}) =>
      HubAction(type: 'if', condition: condition, then: then, otherwise: otherwise);

  factory HubAction.set(String name, HubValue value) =>
      HubAction(type: 'set', varName: name, value: value);

  factory HubAction.waitFor(
    HubCondition condition, {
    double timeout = 10.0,
    double poll = 0.5,
    String onTimeout = 'continue',
  }) =>
      HubAction(type: 'wait_for', condition: condition, timeout: timeout, poll: poll, onTimeout: onTimeout);

  factory HubAction.fromJson(Map<String, dynamic> json) => HubAction(
        type: json['type'] as String,
        device: json['device'] as String?,
        command: json['command'] as String?,
        params: (json['params'] as Map?)?.cast<String, dynamic>(),
        scene: json['scene'] as String?,
        seconds: (json['seconds'] as num?)?.toDouble(),
        direction: json['direction'] as String?,
        target: json['target'] as String?,
        condition: json['condition'] == null
            ? null
            : HubCondition.fromJson((json['condition'] as Map).cast<String, dynamic>()),
        then: _actionList(json['then']),
        otherwise: _actionList(json['otherwise']),
        varName: json['name'] as String?,
        value: json['value'] == null
            ? null
            : HubValue.fromJson((json['value'] as Map).cast<String, dynamic>()),
        timeout: (json['timeout'] as num?)?.toDouble(),
        poll: (json['poll'] as num?)?.toDouble(),
        onTimeout: json['on_timeout'] as String?,
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
      case 'if':
        return {
          'type': 'if',
          'condition': _conditionOrPlaceholder().toJson(),
          'then': then.map((a) => a.toJson()).toList(),
          'otherwise': otherwise.map((a) => a.toJson()).toList(),
        };
      case 'set':
        return {
          'type': 'set',
          'name': varName ?? '',
          'value': (value ?? HubValue.literal('')).toJson(),
        };
      case 'wait_for':
        return {
          'type': 'wait_for',
          'condition': _conditionOrPlaceholder().toJson(),
          'timeout': timeout ?? 10.0,
          'poll': poll ?? 0.5,
          'on_timeout': onTimeout ?? 'continue',
        };
      default:
        return {'type': 'delay', 'seconds': seconds ?? 1.0};
    }
  }

  /// [condition] should always be set by the time an `if`/`wait_for` action
  /// is saved -- the editor seeds one the moment either type is picked --
  /// but a placeholder here means a stray null can never produce a 422 that
  /// only reproduces from the exact editor state that caused it.
  HubCondition _conditionOrPlaceholder() => condition ?? HubCondition(left: HubValue.literal(''));

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
      case 'if':
        return 'If ${condition?.describe() ?? '…'}';
      case 'set':
        return 'Remember ${value?.describe() ?? '…'} as \$${varName ?? '?'}';
      case 'wait_for':
        return 'Wait for ${condition?.describe() ?? '…'}';
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
    this.repeatAccel,
    this.repeatAccelSeconds,
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

  /// How many times faster [onRepeat] fires once the button has been held
  /// for [repeatAccelSeconds], on top of the flat rate above. `1.0` (or
  /// `null`, following [HubConfig.defaultRepeatAccel]) means no ramp at
  /// all -- the remote only reports a hold every ~100ms, so past a certain
  /// point the only way to go faster is to run the repeat actions more than
  /// once per packet instead of waiting for a packet that will not arrive
  /// any sooner.
  double? repeatAccel;

  /// How long the button must be held for [repeatAccel] to reach its full
  /// effect. `null` follows [HubConfig.defaultRepeatAccelSeconds].
  double? repeatAccelSeconds;

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

  factory Binding.fromJson(Map<String, dynamic> json) => Binding(
        onPress: _actionList(json['on_press']),
        onRepeat: _actionList(json['on_repeat']),
        onHold: _actionList(json['on_hold']),
        onRelease: _actionList(json['on_release']),
        holdSeconds: (json['hold_seconds'] as num?)?.toDouble() ?? 0.6,
        repeatDelay: (json['repeat_delay'] as num?)?.toDouble(),
        repeatInterval: (json['repeat_interval'] as num?)?.toDouble(),
        repeatAccel: (json['repeat_accel'] as num?)?.toDouble(),
        repeatAccelSeconds: (json['repeat_accel_seconds'] as num?)?.toDouble(),
      );

  Map<String, dynamic> toJson() => {
        'on_press': onPress.map((a) => a.toJson()).toList(),
        'on_repeat': onRepeat.map((a) => a.toJson()).toList(),
        'on_hold': onHold.map((a) => a.toJson()).toList(),
        'on_release': onRelease.map((a) => a.toJson()).toList(),
        'hold_seconds': holdSeconds,
        'repeat_delay': repeatDelay,
        'repeat_interval': repeatInterval,
        'repeat_accel': repeatAccel,
        'repeat_accel_seconds': repeatAccelSeconds,
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
        onStart: _actionList(json['on_start']),
        onStop: _actionList(json['on_stop']),
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
    this.defaultRepeatAccel = 1.0,
    this.defaultRepeatAccelSeconds = 3.0,
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

  /// Layers an exponential ramp on top of the flat repeat rate above: the
  /// longer a button is held, the faster it repeats, up to
  /// [defaultRepeatAccel] times the base rate once [defaultRepeatAccelSeconds]
  /// of holding has passed. `1.0` disables the ramp entirely.
  double defaultRepeatAccel;
  double defaultRepeatAccelSeconds;

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
        defaultRepeatAccel: (json['default_repeat_accel'] as num?)?.toDouble() ?? 1.0,
        defaultRepeatAccelSeconds: (json['default_repeat_accel_seconds'] as num?)?.toDouble() ?? 3.0,
      );

  Map<String, dynamic> toJson() => {
        'version': version,
        'devices': devices.map((d) => d.toJson()).toList(),
        'scenes': scenes.map((s) => s.toJson()).toList(),
        'global_scene': globalScene,
        'default_scene': defaultScene,
        'default_repeat_delay': defaultRepeatDelay,
        'default_repeat_interval': defaultRepeatInterval,
        'default_repeat_accel': defaultRepeatAccel,
        'default_repeat_accel_seconds': defaultRepeatAccelSeconds,
      };

  HubConfig copy() => HubConfig.fromJson(toJson());
}
