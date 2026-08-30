/// Where the hub listens, what it listens to, and whether it is running.
///
/// This screen exists because the hub used to be configured entirely by
/// command-line flags, which meant correcting a typo'd radio address needed a
/// terminal and a restart -- and a hub that would not start took its own
/// settings page down with it. Nothing here can do that: the web server is
/// already up before any of this is reachable, and stays up through every
/// start, stop and restart driven from it.
///
/// The screen itself is a short list of sections -- Runtime, Event source,
/// Remote buttons, and so on -- each opening as its own page. Every section
/// used to sit on screen at once as a grid of cards; splitting them apart
/// keeps any one page short, at the cost of a tap to reach it.
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../api/settings.dart';
import '../main.dart';
import '../state/hub_store.dart';
import '../state/ui_prefs.dart';
import '../widgets/section_card.dart';
import 'learn_screen.dart';

/// The twelve channels a Harmony Hub uses; anything else the hub rejects.
const List<int> kHarmonyChannels = [5, 8, 14, 17, 32, 35, 41, 44, 62, 65, 71, 74];

const List<String> _kMonthAbbreviations = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', //
];

/// Renders a hub-supplied UTC timestamp (`created_at` / `installed_at`, ISO
/// 8601 with a `+00:00` offset) as the device's local time, e.g.
/// "29 Aug 2026, 14:05". The hub always timestamps in UTC so releases sort
/// correctly regardless of where the hub lives; converting only happens here,
/// at the last moment before it reaches a person.
String _formatTimestamp(String iso) {
  final parsed = DateTime.tryParse(iso);
  if (parsed == null) return iso;
  final local = parsed.toLocal();
  final day = local.day.toString().padLeft(2, '0');
  final month = _kMonthAbbreviations[local.month - 1];
  final hour = local.hour.toString().padLeft(2, '0');
  final minute = local.minute.toString().padLeft(2, '0');
  return '$day $month ${local.year}, $hour:$minute';
}

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  /// The form's own copy. Edits live here until saved, so a rejected value
  /// stays on screen to be corrected rather than snapping back.
  HubSettings? _draft;

  /// What `_draft` started from, to tell "edited" from "unchanged".
  String? _pristine;

  List<HubCheck>? _checks;
  String _checksTitle = '';
  bool _busy = false;

  DiscoveryStatus? _discovery;
  Timer? _discoveryPoll;

  List<UpdateHistoryEntry>? _updateHistory;

  /// Bumped alongside every `setState` here so a section pushed onto its own
  /// page -- a separate branch of the widget tree, not a child of this
  /// screen's `build` -- notices the change too. Plain state fields like
  /// `_draft` aren't `Listenable` the way the store and prefs are, so this
  /// stands in for that.
  final ValueNotifier<int> _tick = ValueNotifier(0);

  @override
  void dispose() {
    _discoveryPoll?.cancel();
    _tick.dispose();
    super.dispose();
  }

  void _notify(VoidCallback change) {
    setState(change);
    _tick.value++;
  }

  HubSettings _ensureDraft(HubSettings live) {
    // Adopt the live settings until the user actually changes something;
    // after that their edits win, so a status refresh cannot wipe the form
    // out from under them mid-sentence.
    if (_draft == null) {
      _draft = live.copy();
      _pristine = live.toJson().toString();
    }
    return _draft!;
  }

  bool get _dirty => _draft != null && _draft!.toJson().toString() != _pristine;

  void _edit(void Function(HubSettings) change) {
    _notify(() => change(_draft!));
  }

  // ------------------------------------------------------------------

  Future<void> _run(Future<void> Function() action) async {
    _notify(() => _busy = true);
    try {
      await action();
    } finally {
      if (mounted) _notify(() => _busy = false);
    }
  }

  Future<void> _save(HubStore store, {required bool restart}) async {
    await _run(() async {
      final saved = await store.saveSettings(_draft!, restart: restart);
      if (!mounted) return;
      if (saved) {
        _notify(() => _pristine = _draft!.toJson().toString());
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(restart ? 'Saved and restarted' : 'Settings saved')),
        );
      } else {
        // The draft is deliberately left alone: whatever was rejected is
        // still in the form, next to the message saying why.
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(store.error ?? 'Could not save settings')),
        );
      }
    });
  }

  Future<void> _runChecks(HubStore store) => _run(() async {
        final checks = await store.api.checks();
        if (mounted) {
          _notify(() {
            _checks = checks;
            _checksTitle = 'Checks';
          });
        }
      });

  Future<void> _tryDraft(HubStore store) => _run(() async {
        final checks = await store.api.trySettings(_draft!);
        if (mounted) {
          _notify(() {
            _checks = checks;
            _checksTitle = 'These settings, not yet saved';
          });
        }
      });

  // ------------------------------------------------------------------

  Future<void> _findRemote(HubStore store) async {
    final method = await _chooseDiscoveryMethod(store);
    if (method == null || !mounted) return;

    try {
      final started = await store.api.startDiscovery(method: method);
      if (!mounted) return;
      _notify(() => _discovery = started);
      _pollDiscovery(store);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$err')));
    }
  }

  /// Asks up front which search to run, rather than defaulting to the Hub
  /// handshake and letting someone without one discover that the hard way
  /// after a minute of waiting. Returns null if the user backs out.
  Future<String?> _chooseDiscoveryMethod(HubStore store) {
    // The search needs the radio to itself; a hub already listening on it
    // gets stopped for the duration and started again once the search
    // ends. Said here, before it happens, rather than left for someone to
    // notice the remote briefly stopped working.
    final willPauseHub = store.hubRunning && store.settings?.source == 'radio';
    return showDialog<String>(
      context: context,
      builder: (context) => SimpleDialog(
        title: const Text('How should I look for your remote?'),
        children: [
          if (willPauseHub)
            const Padding(
              padding: EdgeInsets.fromLTRB(24, 0, 24, 12),
              child: _Note(
                icon: Icons.info_outline,
                text: "Your hub is using the radio — it'll pause while I search, "
                    'and start itself again once I\'m done.',
              ),
            ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 'hub'),
            child: const ListTile(
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.settings_remote),
              title: Text('I still have my Harmony Hub'),
              subtitle: Text("Quickest — you'll press the pair button on the Hub."),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 'sniff'),
            child: const ListTile(
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.hearing),
              title: Text("I don't have a Hub"),
              subtitle: Text('Takes a minute or two — press buttons on the remote while it listens.'),
            ),
          ),
        ],
      ),
    );
  }

  void _pollDiscovery(HubStore store) {
    _discoveryPoll?.cancel();
    _discoveryPoll = Timer.periodic(const Duration(seconds: 1), (timer) async {
      late final DiscoveryStatus status;
      try {
        status = await store.api.discoveryStatus();
      } catch (_) {
        timer.cancel();
        return;
      }
      if (!mounted) {
        timer.cancel();
        return;
      }
      _notify(() => _discovery = status);
      if (!status.isRunning) timer.cancel();
    });
  }

  Future<void> _cancelDiscovery(HubStore store) async {
    try {
      final status = await store.api.cancelDiscovery();
      if (mounted) _notify(() => _discovery = status);
    } catch (_) {
      // The job finished on its own between the tap and the request.
    }
  }

  void _useFoundAddress() {
    final found = _discovery;
    if (found?.address == null) return;
    _edit((s) {
      s.address = found!.address;
      if (found.channel != null) s.channel = found.channel;
      s.source = 'radio';
    });
  }

  // ------------------------------------------------------------------

  /// Live, not draft: takes effect immediately, the same as Start/Stop/
  /// Restart below -- there is nothing to Save here, on purpose, since the
  /// whole point is flipping it on and off during a testing session.
  Future<void> _setPaused(HubStore store, bool value) =>
      _run(() => value ? store.pauseHub() : store.resumeHub());

  // ------------------------------------------------------------------

  /// One row per section, in the order they open. Kept as data rather than
  /// widgets so the same list drives the row label/subtitle and decides
  /// what a tap opens.
  List<_Entry> _entries(HubStore store, UiPrefs prefs) {
    final runtime = store.runtime;
    HubSettings draft() => _ensureDraft(store.settings!);

    return [
      _Entry(
        icon: switch (runtime.state) {
          'running' => Icons.play_circle_outline,
          'failed' => Icons.error_outline,
          'starting' => Icons.hourglass_top,
          _ => Icons.pause_circle_outline,
        },
        title: 'Runtime',
        subtitle: switch (runtime.state) {
          'running' => 'Running · ${runtime.host}:${runtime.port}',
          'failed' => 'Could not start — ${runtime.detail}',
          'starting' => 'Starting…',
          _ => 'Stopped',
        },
        error: runtime.isFailed,
        editsDraft: true,
        content: (context) => _runtimeCard(store, draft()),
      ),
      _Entry(
        icon: Icons.settings_input_antenna,
        title: 'Event source',
        subtitle: () {
          final base = switch (draft().source) {
            'radio' => 'Radio · ${draft().address ?? "no address set"}',
            'replay' => 'Replay · ${draft().replayPath ?? "no capture chosen"}',
            _ => 'None — simulated presses only',
          };
          return draft().source != 'none' && store.paused ? '$base · Paused' : base;
        }(),
        editsDraft: true,
        content: (context) => _sourceCard(store, draft()),
      ),
      _Entry(
        icon: Icons.settings_remote,
        title: 'Remote buttons',
        subtitle: store.buttons.isEmpty
            ? 'No buttons learned yet'
            : '${store.buttons.length} button(s) learned',
        error: store.buttons.isEmpty,
        editsDraft: false,
        content: (context) => _buttonsCard(store),
      ),
      _Entry(
        icon: Icons.sensors_outlined,
        title: 'Infrared',
        subtitle: (draft().irRxPin == null && draft().irTxPin == null)
            ? 'Pins not set'
            : 'Receive ${draft().irRxPin ?? "—"} · transmit ${draft().irTxPin ?? "—"}',
        editsDraft: true,
        content: (context) => _irCard(store, draft()),
      ),
      _Entry(
        icon: Icons.folder_outlined,
        title: 'Files',
        subtitle: draft().configPath,
        editsDraft: true,
        content: (context) => _filesCard(draft()),
      ),
      _Entry(
        icon: Icons.dns_outlined,
        title: 'Web server',
        subtitle: '${draft().host} : ${draft().port}',
        editsDraft: true,
        content: (context) => _serverCard(store, draft()),
      ),
      _Entry(
        icon: Icons.checklist,
        title: 'Checks',
        subtitle: 'Verify the setup, or try these settings before saving',
        editsDraft: false,
        content: (context) => _checksCard(store),
      ),
      if (store.version?.deployed == true)
        _Entry(
          icon: Icons.system_update,
          title: 'Software',
          subtitle: store.hasUndismissedUpdate || store.availableUpdate?.available != null
              ? '${store.version!.buildId ?? "No release installed yet"} -- update available'
              : store.version!.buildId ?? 'No release installed yet',
          editsDraft: false,
          content: (context) => _softwareCard(store),
        ),
      _Entry(
        icon: Icons.smartphone_outlined,
        title: 'This browser',
        subtitle: prefs.get(kRememberEnabled)
            ? 'Remembering where you were'
            : 'Not remembering where you were',
        editsDraft: false,
        content: (context) => _thisDeviceCard(prefs),
      ),
    ];
  }

  void _openEntry(BuildContext context, HubStore store, _Entry entry) {
    Navigator.push(
      context,
      MaterialPageRoute(
        // A pushed route sits in its own Overlay entry, outside the
        // SelectionArea wrapped around `home` in main.dart -- so this page
        // needs its own to keep things like version strings and error text
        // selectable.
        builder: (context) => SelectionArea(
          child: Scaffold(
            appBar: AppBar(title: Text(entry.title)),
            body: ListenableBuilder(
              // Merged with `store`, not just `_tick`: most of this page is
              // this screen's own local state (a draft edit, `_busy`), but
              // the Software section also reads live store state -- whether
              // a GitHub install is in progress, and its latest progress
              // detail -- that changes from `update`/`hub` events arriving
              // over the websocket, not from anything this screen did itself.
              listenable: Listenable.merge([_tick, store]),
              builder: (context, _) => ListView(
                padding: const EdgeInsets.all(16),
                children: [entry.content(context)],
              ),
            ),
            bottomNavigationBar: entry.editsDraft
                ? ListenableBuilder(
                    listenable: _tick,
                    builder: (context, _) => _SaveBar(
                      dirty: _dirty,
                      busy: _busy,
                      onSave: () => _save(store, restart: false),
                      onSaveAndRestart: () => _save(store, restart: true),
                      onRevert: () => _notify(() => _draft = null),
                    ),
                  )
                : null,
          ),
        ),
      ),
    );
  }

  // ------------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final store = HubScope.of(context);
    final prefs = PrefsScope.of(context);
    final live = store.settings;
    if (live == null) {
      return const Center(child: CircularProgressIndicator());
    }
    _ensureDraft(live);
    final entries = _entries(store, prefs);

    return Scaffold(
      body: ListView.separated(
        padding: const EdgeInsets.symmetric(vertical: 8),
        itemCount: entries.length,
        separatorBuilder: (context, index) => const Divider(height: 1),
        itemBuilder: (context, index) {
          final entry = entries[index];
          final scheme = Theme.of(context).colorScheme;
          return ListTile(
            leading: Icon(entry.icon, color: entry.error ? scheme.error : null),
            title: Text(entry.title),
            subtitle: Text(
              entry.subtitle,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: entry.error ? TextStyle(color: scheme.error) : null,
            ),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => _openEntry(context, store, entry),
          );
        },
      ),
      bottomNavigationBar: _SaveBar(
        dirty: _dirty,
        busy: _busy,
        onSave: () => _save(store, restart: false),
        onSaveAndRestart: () => _save(store, restart: true),
        onRevert: () => setState(() => _draft = null),
      ),
    );
  }

  // ------------------------------------------------------------------

  Widget _runtimeCard(HubStore store, HubSettings draft) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _StatusCard(
          store: store,
          busy: _busy,
          onAction: _run,
          discoveryRunning: _discovery?.isRunning ?? false,
        ),
        const SizedBox(height: 12),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: SwitchListTile(
              contentPadding: EdgeInsets.zero,
              value: draft.autostart,
              onChanged: (value) => _edit((s) => s.autostart = value),
              title: const Text('Start the hub automatically'),
              subtitle: const Text('Off serves this page with the hub waiting to be started.'),
            ),
          ),
        ),
      ],
    );
  }

  Widget _sourceCard(HubStore store, HubSettings draft) {
    return SectionCard(
      title: 'Event source',
      subtitle: 'Where button presses come from. Changing this restarts the hub, not this page.',
      children: [
        DropdownButtonFormField<String>(
          initialValue: draft.source,
          decoration: const InputDecoration(labelText: 'Source', border: OutlineInputBorder()),
          items: const [
            DropdownMenuItem(value: 'none', child: Text('None — simulated presses only')),
            DropdownMenuItem(value: 'radio', child: Text('Radio — the real remote')),
            DropdownMenuItem(value: 'replay', child: Text('Replay — a recorded capture')),
          ],
          onChanged: (value) => _edit((s) => s.source = value ?? 'none'),
        ),
        if (draft.source != 'none') ...[
          const SizedBox(height: 4),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: store.paused,
            onChanged: _busy ? null : (value) => _setPaused(store, value),
            title: const Text('Pause command execution'),
            subtitle: const Text(
              'For testing on real hardware without touching your equipment — presses '
              'still show up in the live log, but nothing is sent to a device.',
            ),
          ),
          if (store.paused)
            const _Note(
              icon: Icons.pause_circle_outline,
              text: 'Paused — button presses are being logged but not acted on.',
            ),
        ],
        if (draft.source == 'replay') ...[
          const SizedBox(height: 12),
          _TextField(
            label: 'Capture file',
            value: draft.replayPath ?? '',
            hint: 'captures/reference/full_remote_sweep.jsonl',
            onChanged: (value) => _edit((s) => s.replayPath = value.isEmpty ? null : value),
          ),
          const SizedBox(height: 12),
          _TextField(
            label: 'Speed',
            value: '${draft.replaySpeed}',
            keyboardType: TextInputType.number,
            onChanged: (value) =>
                _edit((s) => s.replaySpeed = double.tryParse(value) ?? s.replaySpeed),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: draft.replayLoop,
            onChanged: (value) => _edit((s) => s.replayLoop = value),
            title: const Text('Loop the capture'),
            subtitle: const Text('Off plays it through once and then goes quiet.'),
          ),
        ],
        if (draft.source == 'radio') ...[
          const SizedBox(height: 12),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: _TextField(
                  label: 'Remote address',
                  value: draft.address ?? '',
                  hint: '17129BFCB6',
                  onChanged: (value) => _edit((s) => s.address = value.isEmpty ? null : value),
                ),
              ),
              const SizedBox(width: 12),
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: OutlinedButton.icon(
                  onPressed: (_discovery?.isRunning ?? false) ? null : () => _findRemote(store),
                  icon: const Icon(Icons.wifi_find),
                  label: const Text('Find my remote'),
                ),
              ),
            ],
          ),
          if (_discovery != null && _discovery!.state != 'idle')
            _DiscoveryPanel(
              status: _discovery!,
              onCancel: () => _cancelDiscovery(store),
              onUse: _useFoundAddress,
            ),
          const SizedBox(height: 12),
          DropdownButtonFormField<int?>(
            initialValue: kHarmonyChannels.contains(draft.channel) ? draft.channel : null,
            decoration: const InputDecoration(
              labelText: 'Start channel',
              border: OutlineInputBorder(),
              helperText: 'The Hub moves on its own; this is only where to look first.',
            ),
            items: [
              const DropdownMenuItem(value: null, child: Text('Search for it')),
              for (final channel in kHarmonyChannels)
                DropdownMenuItem(value: channel, child: Text('$channel')),
            ],
            onChanged: (value) => _edit((s) => s.channel = value),
          ),
          const SizedBox(height: 12),
          _TextField(
            label: 'Probe interval (seconds)',
            value: '${draft.probeInterval}',
            keyboardType: TextInputType.number,
            helper: 'Quiet seconds before transmitting to re-find the Hub. 0 never transmits.',
            onChanged: (value) =>
                _edit((s) => s.probeInterval = double.tryParse(value) ?? s.probeInterval),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: draft.allowAck,
            onChanged: (value) => _edit((s) => s.allowAck = value),
            title: const Text('Answer the remote'),
            subtitle: const Text(
              'Correct when replacing the Hub. Wrong while a real Hub is powered on — '
              'both would answer at once and the remote would see neither.',
            ),
          ),
          const SizedBox(height: 4),
          Row(
            children: [
              Expanded(
                child: _TextField(
                  label: 'CSN pin',
                  value: draft.csnPin,
                  onChanged: (value) => _edit((s) => s.csnPin = value),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _TextField(
                  label: 'CE pin',
                  value: draft.cePin,
                  onChanged: (value) => _edit((s) => s.cePin = value),
                ),
              ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _irCard(HubStore store, HubSettings draft) {
    return SectionCard(
      title: 'Infrared',
      subtitle: 'One receiver and one transmitter for the whole install, wired once and shared '
          'by every infrared device — set the pins here, not per device. Applied the moment '
          'you save; unlike the radio pins above, this never needs the hub restarted.',
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: _TextField(
                label: 'Receive pin (GPIO)',
                value: draft.irRxPin?.toString() ?? '',
                hint: '17',
                helper: 'Leave blank on a transmit-only install.',
                keyboardType: TextInputType.number,
                onChanged: (value) => _edit((s) => s.irRxPin = int.tryParse(value.trim())),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: _TextField(
                label: 'Transmit pin (GPIO)',
                value: draft.irTxPin?.toString() ?? '',
                hint: '18',
                helper: 'Leave blank on a receive-only install.',
                keyboardType: TextInputType.number,
                onChanged: (value) => _edit((s) => s.irTxPin = int.tryParse(value.trim())),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          'pigpiod connection',
          style: Theme.of(context).textTheme.labelMedium,
        ),
        const SizedBox(height: 4),
        Text(
          'Only worth changing if the receiver/transmitter are on a different Pi than this hub.',
          style: Theme.of(context).textTheme.bodySmall,
        ),
        const SizedBox(height: 8),
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              flex: 2,
              child: _TextField(
                label: 'pigpiod host',
                value: draft.irPigpioHost,
                onChanged: (value) => _edit((s) => s.irPigpioHost = value),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: _TextField(
                label: 'pigpiod port',
                value: '${draft.irPigpioPort}',
                keyboardType: TextInputType.number,
                onChanged: (value) =>
                    _edit((s) => s.irPigpioPort = int.tryParse(value.trim()) ?? s.irPigpioPort),
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buttonsCard(HubStore store) {
    final count = store.buttons.length;
    return SectionCard(
      title: 'Remote buttons',
      subtitle: 'What the hub knows your remote can send. Nothing else on this '
          'page matters until this has something in it.',
      children: [
        if (count == 0)
          const _Note(
            icon: Icons.priority_high,
            text: 'No buttons learned yet, so the hub cannot tell one press from another.',
            error: true,
          )
        else
          _Note(icon: Icons.settings_remote, text: '$count button(s) learned.'),
        Align(
          alignment: Alignment.centerLeft,
          child: FilledButton.tonalIcon(
            onPressed: () => openLearnPage(context, store),
            icon: const Icon(Icons.school_outlined),
            label: Text(count == 0 ? 'Learn the remote' : 'Learn or edit buttons'),
          ),
        ),
      ],
    );
  }

  Widget _filesCard(HubSettings draft) {
    return SectionCard(
      title: 'Files',
      subtitle: 'Read when the hub starts, and rewritten as you edit scenes and devices.',
      children: [
        _TextField(
          label: 'Configuration file',
          value: draft.configPath,
          onChanged: (value) => _edit((s) => s.configPath = value),
        ),
        const SizedBox(height: 12),
        _TextField(
          label: 'Button map',
          value: draft.buttonsPath,
          helper: 'Built by `harmony-receiver learn` from a capture.',
          onChanged: (value) => _edit((s) => s.buttonsPath = value),
        ),
      ],
    );
  }

  Widget _serverCard(HubStore store, HubSettings draft) {
    final live = store.runtime;
    final pending = draft.needsProcessRestart(store.settings ?? draft) || live.pendingRestart;

    return SectionCard(
      title: 'Web server',
      subtitle: 'Saved now, applied the next time the hub process starts. '
          'Moving the live listener would take this page’s address with it.',
      children: [
        if (pending)
          _Note(
            icon: Icons.schedule,
            text: 'Currently serving on ${live.host}:${live.port}. '
                'The saved values take effect when the hub process is restarted.',
          ),
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              flex: 2,
              child: _TextField(
                label: 'Bind address',
                value: draft.host,
                helper: '0.0.0.0 listens on every interface; 127.0.0.1 only on this machine.',
                onChanged: (value) => _edit((s) => s.host = value),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: _TextField(
                label: 'Port',
                value: '${draft.port}',
                keyboardType: TextInputType.number,
                onChanged: (value) => _edit((s) => s.port = int.tryParse(value) ?? s.port),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _TextField(
          label: 'Web UI directory',
          value: draft.uiDir ?? '',
          hint: 'found automatically',
          onChanged: (value) => _edit((s) => s.uiDir = value.isEmpty ? null : value),
        ),
      ],
    );
  }

  Widget _checksCard(HubStore store) {
    return SectionCard(
      title: 'Checks',
      subtitle: 'Check what is configured, or try the values above before committing them.',
      children: [
        Wrap(
          spacing: 12,
          runSpacing: 8,
          children: [
            OutlinedButton.icon(
              onPressed: _busy ? null : () => _runChecks(store),
              icon: const Icon(Icons.checklist),
              label: const Text('Run checks'),
            ),
            OutlinedButton.icon(
              onPressed: _busy ? null : () => _tryDraft(store),
              icon: const Icon(Icons.science_outlined),
              label: const Text('Try these settings'),
            ),
          ],
        ),
        if (_checks != null) ...[
          const SizedBox(height: 16),
          Text(_checksTitle, style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 4),
          for (final check in _checks!)
            ListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              leading: Icon(
                check.ok ? Icons.check_circle : Icons.error_outline,
                color: check.ok
                    ? Colors.greenAccent
                    : Theme.of(context).colorScheme.error,
              ),
              title: Text(check.name),
              subtitle: Text(check.detail),
            ),
        ],
      ],
    );
  }

  /// What this hub is running, and a way back if the last push was bad.
  ///
  /// Only shown at all when the hub was started under the release/launcher
  /// layout (`store.version.deployed`) -- an ordinary `harmony-hub` run has
  /// nothing here to show, and every route behind this card would 404.
  Widget _softwareCard(HubStore store) {
    final version = store.version!;
    final trial = version.trial;
    return SectionCard(
      title: 'Software',
      subtitle: 'What this hub is running, pushed here with harmony-deploy.',
      children: [
        ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.tag),
          title: Text(version.buildId ?? 'No release installed yet'),
          subtitle: Text([
            if (version.gitSha.isNotEmpty) '${version.gitSha}${version.gitDirty ? ' (dirty)' : ''}',
            if (version.builtAt != null) 'built ${_formatTimestamp(version.builtAt!)}',
          ].join(' -- ')),
        ),
        if (trial != null)
          _Note(
            icon: Icons.hourglass_top,
            text: 'On trial (attempt ${trial.attempts}) -- confirmed automatically once the hub has '
                'stayed up for a minute. Falls back to the previous release on its own if it keeps failing to start.',
          ),
        if (!version.updatesEnabled)
          const _Note(
            icon: Icons.block,
            text: 'Remote updates are disabled for this hub -- turn "updates_enabled" back on in '
                'hub_settings.json to accept a push again.',
            error: true,
          ),
        if (version.tokenFingerprint != null)
          Text(
            'Update token: ${version.tokenFingerprint} -- confirm this matches deploy_targets.json '
            'before trusting a push to have come from you.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        if (version.updatesFromGithub) ...[
          const Divider(height: 24),
          _githubReleaseSection(store, version),
        ],
        const SizedBox(height: 12),
        Wrap(
          spacing: 12,
          runSpacing: 8,
          children: [
            OutlinedButton.icon(
              onPressed: (version.previous == null || _busy || store.installingUpdate)
                  ? null
                  : () => _confirmRollback(store, version),
              icon: const Icon(Icons.settings_backup_restore),
              label: Text(version.previous == null ? 'No previous release' : 'Roll back to ${version.previous}'),
            ),
            OutlinedButton.icon(
              onPressed: _busy ? null : () => _run(() => _loadUpdateHistory(store)),
              icon: const Icon(Icons.history),
              label: const Text('Show history'),
            ),
          ],
        ),
        if (_updateHistory != null) ...[
          const SizedBox(height: 8),
          if (_updateHistory!.isEmpty)
            const Text('No confirmed installs yet.')
          else
            for (final entry in _updateHistory!.reversed)
              ListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                leading: Icon(
                  switch (entry.outcome) {
                    'good' => Icons.check_circle,
                    'rolled_back' => Icons.undo,
                    _ => Icons.error_outline,
                  },
                  color: entry.outcome == 'good' ? Colors.greenAccent : Theme.of(context).colorScheme.error,
                ),
                title: Text(entry.buildId),
                subtitle: Text('${entry.outcome} -- ${_formatTimestamp(entry.installedAt)}'),
              ),
        ],
      ],
    );
  }

  Future<void> _loadUpdateHistory(HubStore store) async {
    _updateHistory = await store.loadUpdateHistory();
  }

  Future<void> _confirmRollback(HubStore store, VersionInfo version) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Roll back?'),
        content: Text(
          'This restarts the hub onto ${version.previous}, undoing ${version.buildId}. '
          'The hub is briefly unreachable while it restarts.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Roll back')),
        ],
      ),
    );
    if (confirmed == true) {
      await _run(() async {
        await store.rollbackUpdate();
      });
    }
  }

  /// The GitHub side of the Software card: what was last found, and the way
  /// to install it. Three states -- installing, a release waiting, or
  /// nothing new -- each replacing the others rather than stacking up.
  Widget _githubReleaseSection(HubStore store, VersionInfo version) {
    final check = store.availableUpdate;
    final release = check?.available;
    final scheme = Theme.of(context).colorScheme;

    if (store.installingUpdate) {
      return _Note(
        icon: Icons.downloading,
        text: 'Installing ${release?.tag ?? 'the new release'} -- '
            '${store.updateProgressDetail ?? 'starting.'} The hub will restart once this finishes.',
      );
    }

    if (release != null) {
      return Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: scheme.tertiaryContainer.withValues(alpha: 0.35),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${release.tag} is available', style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 4),
            Text(
              [release.buildId, if (release.gitSha.isNotEmpty) release.gitSha].join(' -- '),
              style: Theme.of(context).textTheme.bodySmall,
            ),
            if (release.notes.isNotEmpty) ...[
              const SizedBox(height: 8),
              Text(release.notes, maxLines: 4, overflow: TextOverflow.ellipsis),
            ],
            const SizedBox(height: 12),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: [
                FilledButton.icon(
                  onPressed: _busy ? null : () => _installUpdate(store),
                  icon: const Icon(Icons.system_update),
                  label: const Text('Update software'),
                ),
                OutlinedButton.icon(
                  onPressed: _busy ? null : () => _run(() => store.checkForUpdate()),
                  icon: const Icon(Icons.refresh),
                  label: const Text('Check again'),
                ),
              ],
            ),
          ],
        ),
      );
    }

    return Row(
      children: [
        Expanded(
          child: Text(
            check?.lastError != null
                ? 'Last check failed: ${check!.lastError}'
                : check?.lastCheckedAt != null
                    ? 'Up to date -- last checked ${_formatTimestamp(check!.lastCheckedAt!)}'
                    : 'Not checked yet.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ),
        TextButton.icon(
          onPressed: _busy ? null : () => _run(() => store.checkForUpdate()),
          icon: const Icon(Icons.refresh),
          label: const Text('Check for updates'),
        ),
      ],
    );
  }

  Future<void> _installUpdate(HubStore store) async {
    final release = store.availableUpdate?.available;
    if (release == null) return;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Update software?'),
        content: Text(
          'This downloads and installs ${release.tag} (${release.buildId}), then restarts the hub onto '
          'it. The hub is briefly unreachable while it restarts, and the whole install can take several '
          'minutes on a Pi.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Update')),
        ],
      ),
    );
    if (confirmed != true) return;

    await _run(() async {
      var started = await store.installUpdate();

      if (!started && mounted && (store.error ?? '').contains('scene is active')) {
        final forceConfirmed = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('A scene is active'),
            content: const Text('Installing now stops the active scene early. Install anyway?'),
            actions: [
              TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
              FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Install anyway')),
            ],
          ),
        );
        if (forceConfirmed == true) {
          started = await store.installUpdate(force: true);
        }
      }

      if (!started && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(store.error ?? 'Could not start the install')),
        );
      }
    });
  }

  /// Preferences kept in this browser's own storage -- last tab, searches,
  /// the activity filter -- as opposed to every other section on this
  /// screen, which edits settings the hub applies for every browser pointed
  /// at it.
  Widget _thisDeviceCard(UiPrefs prefs) {
    return SectionCard(
      title: 'This browser',
      subtitle: 'Kept in this browser only -- other devices and other browsers have their own.',
      children: [
        if (prefs.degraded)
          const _Note(
            icon: Icons.priority_high,
            text: 'Preferences are not being saved on this device -- storage is unavailable, '
                'so the last tab and any filters will not be remembered.',
            error: true,
          ),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          value: prefs.get(kRememberEnabled),
          onChanged: (value) => prefs.set(kRememberEnabled, value),
          title: const Text('Remember where I was'),
          subtitle: const Text(
            'The last tab, and any searches or filters left set, from one visit to the next.',
          ),
        ),
        const SizedBox(height: 8),
        OutlinedButton.icon(
          onPressed: () => _confirmResetPrefs(prefs),
          icon: const Icon(Icons.restart_alt),
          label: const Text("Reset this device's preferences"),
        ),
      ],
    );
  }

  Future<void> _confirmResetPrefs(UiPrefs prefs) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Reset preferences for this device?'),
        content: const Text(
          'The remembered tab, searches and activity filter go back to their defaults. '
          'Nothing on the hub itself is affected.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Reset')),
        ],
      ),
    );
    if (confirmed == true) prefs.resetAll();
  }
}

// ----------------------------------------------------------------------

/// One row on the Settings list, and what tapping it opens.
class _Entry {
  const _Entry({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.content,
    this.error = false,
    required this.editsDraft,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final bool error;

  /// The section's own content, built fresh every time it is opened or the
  /// draft changes underneath it.
  final WidgetBuilder content;

  /// Whether this section edits the shared [HubSettings] draft -- if so, its
  /// page carries the same save bar as the list itself; if not (Remote
  /// buttons, Checks, Software, This device), saving there means something
  /// else entirely, or nothing, so the bar would just be dead weight.
  final bool editsDraft;
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({
    required this.store,
    required this.busy,
    required this.onAction,
    required this.discoveryRunning,
  });

  final HubStore store;
  final bool busy;
  final Future<void> Function(Future<void> Function()) onAction;

  /// A remote search is using the radio right now. Starting or restarting
  /// the hub while it's running would just be refused by the backend --
  /// disabled here so that refusal isn't the first time anyone hears about
  /// it, and so a radio hub the search auto-stopped doesn't look startable
  /// again until it actually is.
  final bool discoveryRunning;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final runtime = store.runtime;
    final failed = runtime.isFailed;
    final running = runtime.isRunning;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  running
                      ? Icons.play_circle
                      : failed
                          ? Icons.error
                          : Icons.pause_circle,
                  color: running
                      ? Colors.greenAccent
                      : failed
                          ? scheme.error
                          : scheme.onSurfaceVariant,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        switch (runtime.state) {
                          'running' => 'Hub is running',
                          'failed' => 'Hub could not start',
                          'starting' => 'Hub is starting…',
                          _ => 'Hub is stopped',
                        },
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      Text(runtime.detail, style: Theme.of(context).textTheme.bodySmall),
                    ],
                  ),
                ),
              ],
            ),
            if (runtime.problems.isNotEmpty) ...[
              const SizedBox(height: 12),
              for (final problem in runtime.problems)
                _Note(icon: Icons.priority_high, text: problem, error: true),
            ],
            if (runtime.configError != null) ...[
              const SizedBox(height: 12),
              _Note(
                icon: Icons.description_outlined,
                text: '${runtime.configError}. Scenes and devices are showing as empty until '
                    'the file is fixed — saving over it would discard whatever it really holds.',
                error: true,
              ),
            ],
            if (runtime.settingsError != null) ...[
              const SizedBox(height: 12),
              _Note(icon: Icons.settings_outlined, text: runtime.settingsError!, error: true),
            ],
            const SizedBox(height: 12),
            Text(
              'Listening on ${runtime.host}:${runtime.port} · ${runtime.source}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: [
                FilledButton.icon(
                  onPressed: busy || running || discoveryRunning ? null : () => onAction(store.startHub),
                  icon: const Icon(Icons.play_arrow),
                  label: const Text('Start'),
                ),
                OutlinedButton.icon(
                  onPressed: busy || !running ? null : () => onAction(store.stopHub),
                  icon: const Icon(Icons.stop),
                  label: const Text('Stop'),
                ),
                OutlinedButton.icon(
                  onPressed: busy || discoveryRunning ? null : () => onAction(store.restartHub),
                  icon: const Icon(Icons.restart_alt),
                  label: const Text('Restart'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _DiscoveryPanel extends StatelessWidget {
  const _DiscoveryPanel({required this.status, required this.onCancel, required this.onUse});

  final DiscoveryStatus status;
  final VoidCallback onCancel;
  final VoidCallback onUse;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              if (status.isRunning)
                const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              if (status.isRunning) const SizedBox(width: 12),
              Expanded(child: Text(status.detail)),
              if (status.isRunning)
                TextButton(onPressed: onCancel, child: const Text('Cancel')),
            ],
          ),
          if (status.address != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: FilledButton.tonalIcon(
                onPressed: onUse,
                icon: const Icon(Icons.download_done),
                label: Text('Use ${status.address}'),
              ),
            ),
        ],
      ),
    );
  }
}

class _SaveBar extends StatelessWidget {
  const _SaveBar({
    required this.dirty,
    required this.busy,
    required this.onSave,
    required this.onSaveAndRestart,
    required this.onRevert,
  });

  final bool dirty;
  final bool busy;
  final VoidCallback onSave;
  final VoidCallback onSaveAndRestart;
  final VoidCallback onRevert;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
        child: Row(
          children: [
            if (dirty)
              TextButton(onPressed: busy ? null : onRevert, child: const Text('Discard changes')),
            const Spacer(),
            OutlinedButton(
              onPressed: dirty && !busy ? onSave : null,
              child: const Text('Save'),
            ),
            const SizedBox(width: 12),
            FilledButton.icon(
              onPressed: busy ? null : onSaveAndRestart,
              icon: const Icon(Icons.restart_alt),
              label: const Text('Save & restart hub'),
            ),
          ],
        ),
      ),
    );
  }
}

// ----------------------------------------------------------------------

/// A text field that follows the draft rather than owning its own state, so
/// discarding changes actually clears what is on screen.
class _TextField extends StatelessWidget {
  const _TextField({
    required this.label,
    required this.value,
    required this.onChanged,
    this.hint,
    this.helper,
    this.keyboardType,
  });

  final String label;
  final String value;
  final ValueChanged<String> onChanged;
  final String? hint;
  final String? helper;
  final TextInputType? keyboardType;

  @override
  Widget build(BuildContext context) {
    return TextFormField(
      // Keyed on the value's identity so a programmatic change -- "use this
      // address" -- actually shows up, while typing does not rebuild it.
      key: ValueKey('$label:$value'),
      initialValue: value,
      keyboardType: keyboardType,
      decoration: InputDecoration(
        labelText: label,
        hintText: hint,
        helperText: helper,
        helperMaxLines: 3,
        border: const OutlineInputBorder(),
      ),
      onChanged: onChanged,
    );
  }
}

class _Note extends StatelessWidget {
  const _Note({required this.icon, required this.text, this.error = false});

  final IconData icon;
  final String text;
  final bool error;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final colour = error ? scheme.error : scheme.onSurfaceVariant;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 18, color: colour),
          const SizedBox(width: 8),
          Expanded(
            child: Text(text, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: colour)),
          ),
        ],
      ),
    );
  }
}
