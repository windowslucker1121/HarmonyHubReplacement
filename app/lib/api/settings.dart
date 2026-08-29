/// Deployment settings and the hub's own state.
///
/// Mirrors `harmony_hub.settings`, `harmony_hub.runtime` and
/// `harmony_hub.diagnostics`. Kept apart from `config.dart` for the same
/// reason the server keeps two files: that one is devices and scenes, this
/// one is where the hub listens and what it listens to.
///
/// The server rejects unknown keys, so `toJson` has to emit every field and
/// nothing else.
library;

/// Where the hub listens, and what it listens to.
class HubSettings {
  HubSettings({
    this.host = '0.0.0.0',
    this.port = 8765,
    this.uiDir,
    this.configPath = 'hub_config.json',
    this.buttonsPath = 'buttons.json',
    this.autostart = true,
    this.source = 'none',
    this.replayPath,
    this.replaySpeed = 1.0,
    this.replayLoop = true,
    this.address,
    this.channel,
    this.probeInterval = 0.0,
    this.allowAck = false,
    this.csnPin = 'C0',
    this.cePin = 'D4',
    this.irRxPin,
    this.irTxPin,
    this.irPigpioHost = 'localhost',
    this.irPigpioPort = 8888,
    this.verbose = false,
  });

  /// Bind address and port. Editable and saved, but only read at process
  /// start: rebinding the live listener would move this page's own URL.
  String host;
  int port;
  String? uiDir;

  String configPath;
  String buttonsPath;

  /// Whether to bring the hub up as soon as the process starts.
  bool autostart;

  /// none | radio | replay
  String source;

  String? replayPath;
  double replaySpeed;
  bool replayLoop;

  String? address;
  int? channel;
  double probeInterval;
  bool allowAck;
  String csnPin;
  String cePin;

  /// One receiver and one transmitter for the whole install, wired once and
  /// shared by every IR device configured against them — see the identical
  /// note on `harmony_hub.settings.HubSettings.ir_rx_pin`. BCM GPIO numbers,
  /// null meaning "not wired". Applied the moment these are saved, with no
  /// restart needed — unlike `csnPin`/`cePin` above.
  int? irRxPin;
  int? irTxPin;
  String irPigpioHost;
  int irPigpioPort;

  bool verbose;

  factory HubSettings.fromJson(Map<String, dynamic> json) => HubSettings(
        host: (json['host'] ?? '0.0.0.0') as String,
        port: (json['port'] ?? 8765) as int,
        uiDir: json['ui_dir'] as String?,
        configPath: (json['config_path'] ?? 'hub_config.json') as String,
        buttonsPath: (json['buttons_path'] ?? 'buttons.json') as String,
        autostart: (json['autostart'] ?? true) as bool,
        source: (json['source'] ?? 'none') as String,
        replayPath: json['replay_path'] as String?,
        replaySpeed: ((json['replay_speed'] ?? 1.0) as num).toDouble(),
        replayLoop: (json['replay_loop'] ?? true) as bool,
        address: json['address'] as String?,
        channel: json['channel'] as int?,
        probeInterval: ((json['probe_interval'] ?? 0.0) as num).toDouble(),
        allowAck: (json['allow_ack'] ?? false) as bool,
        csnPin: (json['csn_pin'] ?? 'C0') as String,
        cePin: (json['ce_pin'] ?? 'D4') as String,
        irRxPin: json['ir_rx_pin'] as int?,
        irTxPin: json['ir_tx_pin'] as int?,
        irPigpioHost: (json['ir_pigpio_host'] ?? 'localhost') as String,
        irPigpioPort: (json['ir_pigpio_port'] ?? 8888) as int,
        verbose: (json['verbose'] ?? false) as bool,
      );

  Map<String, dynamic> toJson() => {
        'host': host,
        'port': port,
        'ui_dir': uiDir,
        'config_path': configPath,
        'buttons_path': buttonsPath,
        'autostart': autostart,
        'source': source,
        'replay_path': replayPath,
        'replay_speed': replaySpeed,
        'replay_loop': replayLoop,
        'address': address,
        'channel': channel,
        'probe_interval': probeInterval,
        'allow_ack': allowAck,
        'csn_pin': csnPin,
        'ce_pin': cePin,
        'ir_rx_pin': irRxPin,
        'ir_tx_pin': irTxPin,
        'ir_pigpio_host': irPigpioHost,
        'ir_pigpio_port': irPigpioPort,
        'verbose': verbose,
      };

  HubSettings copy() => HubSettings.fromJson(toJson());

  /// Whether editing these would need the whole process restarted, which the
  /// settings page says out loud rather than letting a change look applied.
  bool needsProcessRestart(HubSettings live) =>
      host != live.host || port != live.port || uiDir != live.uiDir;
}

/// What the hub is doing right now, as opposed to how it is configured.
class RuntimeStatus {
  RuntimeStatus({
    required this.state,
    this.detail = '',
    this.source = '',
    this.startedAt,
    this.host = '',
    this.port = 0,
    this.pendingRestart = false,
    this.settingsPath = '',
    this.configPath = '',
    this.configError,
    this.settingsError,
    this.problems = const [],
  });

  /// stopped | starting | running | failed
  final String state;
  final String detail;
  final String source;
  final DateTime? startedAt;

  /// Where this process is *actually* listening, which is not necessarily
  /// what the saved settings now say. See [pendingRestart].
  final String host;
  final int port;
  final bool pendingRestart;

  final String settingsPath;
  final String configPath;

  /// Set when the configuration file could not be read. The hub still runs,
  /// on an empty configuration standing in for the real one.
  final String? configError;
  final String? settingsError;

  /// Why the hub will not start, in words meant for a person.
  final List<String> problems;

  bool get isRunning => state == 'running';
  bool get isFailed => state == 'failed';

  factory RuntimeStatus.fromJson(Map<String, dynamic> json) => RuntimeStatus(
        state: (json['state'] ?? 'stopped') as String,
        detail: (json['detail'] ?? '') as String,
        source: (json['source'] ?? '') as String,
        startedAt: DateTime.tryParse((json['started_at'] ?? '') as String? ?? ''),
        host: (json['host'] ?? '') as String,
        port: (json['port'] ?? 0) as int,
        pendingRestart: (json['pending_restart'] ?? false) as bool,
        settingsPath: (json['settings_path'] ?? '') as String,
        configPath: (json['config_path'] ?? '') as String,
        configError: json['config_error'] as String?,
        settingsError: json['settings_error'] as String?,
        problems: ((json['problems'] ?? []) as List).cast<String>(),
      );
}

/// One thing that was verified, from either the checklist or the dry-run.
class HubCheck {
  HubCheck({required this.name, required this.ok, this.detail = ''});

  final String name;
  final bool ok;
  final String detail;

  factory HubCheck.fromJson(Map<String, dynamic> json) => HubCheck(
        name: json['name'] as String,
        ok: json['ok'] as bool,
        detail: (json['detail'] ?? '') as String,
      );
}

/// How the search for the remote's network address is going.
class DiscoveryStatus {
  DiscoveryStatus({
    required this.state,
    this.method = 'hub',
    this.detail = '',
    this.address,
    this.channel,
  });

  /// idle | running | done | failed | cancelled
  final String state;

  /// hub | sniff -- which search this status belongs to.
  final String method;

  final String detail;
  final String? address;
  final int? channel;

  bool get isRunning => state == 'running';

  factory DiscoveryStatus.fromJson(Map<String, dynamic> json) => DiscoveryStatus(
        state: (json['state'] ?? 'idle') as String,
        method: (json['method'] ?? 'hub') as String,
        detail: (json['detail'] ?? '') as String,
        address: json['address'] as String?,
        channel: json['channel'] as int?,
      );
}

/// A release that has been activated but not yet proven to boot cleanly.
class TrialInfo {
  TrialInfo({required this.release, required this.attempts, this.fromRelease});

  final String release;
  final int attempts;
  final String? fromRelease;

  factory TrialInfo.fromJson(Map<String, dynamic> json) => TrialInfo(
        release: json['release'] as String,
        attempts: (json['attempts'] ?? 0) as int,
        fromRelease: json['from_release'] as String?,
      );
}

/// What this hub is running, and whether `/api/update` is even available.
///
/// Mirrors `harmony_hub.api.VersionInfo`. `deployed` is false for an
/// ordinary `harmony-hub` run started outside the release/launcher layout
/// (a dev checkout, or an install that predates this feature) -- the
/// Settings screen shows nothing update-related at all in that case, rather
/// than a card full of routes that would all 404.
class VersionInfo {
  VersionInfo({
    required this.deployed,
    required this.updatesEnabled,
    this.buildId,
    this.gitSha = '',
    this.gitDirty = false,
    this.builtAt,
    this.webBuildId,
    this.previous,
    this.trial,
    this.tokenFingerprint,
  });

  final bool deployed;
  final bool updatesEnabled;
  final String? buildId;
  final String gitSha;
  final bool gitDirty;
  final String? builtAt;
  final String? webBuildId;
  final String? previous;
  final TrialInfo? trial;
  final String? tokenFingerprint;

  factory VersionInfo.fromJson(Map<String, dynamic> json) => VersionInfo(
        deployed: (json['deployed'] ?? false) as bool,
        updatesEnabled: (json['updates_enabled'] ?? true) as bool,
        buildId: json['build_id'] as String?,
        gitSha: (json['git_sha'] ?? '') as String,
        gitDirty: (json['git_dirty'] ?? false) as bool,
        builtAt: json['built_at'] as String?,
        webBuildId: json['web_build_id'] as String?,
        previous: json['previous'] as String?,
        trial: json['trial'] == null ? null : TrialInfo.fromJson(json['trial'] as Map<String, dynamic>),
        tokenFingerprint: json['token_fingerprint'] as String?,
      );
}

/// The result of pushing an update or rolling one back: what is now current, and that a restart is coming.
class UpdateResult {
  UpdateResult({this.buildId, required this.restarting});

  final String? buildId;
  final bool restarting;

  factory UpdateResult.fromJson(Map<String, dynamic> json) =>
      UpdateResult(buildId: json['build_id'] as String?, restarting: (json['restarting'] ?? false) as bool);
}

/// One completed install or rollback, kept after the release it describes may have been pruned.
class UpdateHistoryEntry {
  UpdateHistoryEntry({required this.buildId, required this.installedAt, required this.outcome});

  final String buildId;
  final String installedAt;

  /// good | rolled_back | failed
  final String outcome;

  factory UpdateHistoryEntry.fromJson(Map<String, dynamic> json) => UpdateHistoryEntry(
        buildId: json['build_id'] as String,
        installedAt: json['installed_at'] as String,
        outcome: json['outcome'] as String,
      );
}
