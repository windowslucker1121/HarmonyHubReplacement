# Harmony RF24 Receiver — Project Status

## Goal

A drop-in replacement for the discontinued Logitech Harmony Hub. The current
stage decodes the RF traffic from a Harmony remote and maps it to named
physical buttons, exposed as:

1. A command-line tool that prints events as they happen.
2. A reusable Python library (pub/sub events, not print statements).
3. A complete button map for a remote.

**Status: all three work.** All 48 buttons on a Harmony Companion are captured,
decoded, and named. A scene engine on top of them is in place; a web/mobile UI
for it is the current work.

## Hardware

- **Bridge:** Adafruit FT232H USB breakout (Blinka + pyftdi + pyusb +
  libusb-1.0.dll, WinUSB driver via Zadig on Windows).
- **Radio:** nRF24L01+ wired to the FT232H:
  - SCK → D0, MOSI → D1, MISO → D2 (hardware SPI)
  - CSN → C0, CE → D4, VCC → 3.3V, GND → GND
  - 100 µF capacitor across the module's VCC/GND.
- **Remote/Hub:** Logitech Harmony Hub + Harmony Companion remote.
- Network address in use: `17129BFCB6` (driver's LSB-first order).

## Protocol — confirmed

### Packet layout

10-byte packets:

| byte | meaning |
|------|---------|
| 0    | `0x00`, or the network address LSB on a press's first packets |
| 1    | report id |
| 2–4  | report body |
| 5–8  | padding (zero in every capture so far) |
| 9    | checksum |

Checksum rule: all bytes sum to 0 mod 256. True of every payload ever
captured.

### The remote transmits standard USB HID reports

This is the key finding, and it means button identity is *read*, not learned:

| report id | meaning | body |
|-----------|---------|------|
| `0xC1` | HID keyboard | `[modifier, keycode, 0]` |
| `0xC3` | HID consumer | 16-bit usage, little-endian |
| `0x4F` | status/session — **not a button** | varies |
| `0x40` | 5-byte keepalive tick | varies |

An all-zero body is an explicit **release** report. Verified by prediction:
codes for 7 buttons were written down before capturing, and 7/7 decoded to
correctly-named HID usages (6/7 matched the exact guessed code; OK/Select
turned out to be keyboard `0x58` Keypad Enter rather than `0x28` Enter).

Worked examples, each confirmed against the button name reported at capture:

```
C1 00 52 00  -> keyboard 0x52   -> Up Arrow
C1 00 51 00  -> keyboard 0x51   -> Down Arrow
C3 E9 00 00  -> consumer 0x00E9 -> Volume Up
C3 00 00 00  -> consumer 0x0000 -> release
```

### Addressing

Both addresses carry remote → Hub traffic; the Hub is the passive side.

| address | payload[0] | pipe | role |
|---------|-----------|------|------|
| `<network>00` | address LSB | 1 (discovery) | first packets of a press |
| `<network>E`  | `0x00`      | 2 (session)   | every packet after |

The same report body appears on both, so a button decodes from either. On an
nRF24L01+, pipes 2–5 store only their own LSB and inherit pipe 1's upper four
bytes, so the discovery address **must** be opened on pipe 1 first.

Only `0x17` exists as a device id on this network (verified by sweeping all
256 LSBs and seeing exactly one ACK).

### Cadence

5-byte ticks every ~100 ms while a button is held, ~1 s for about 30 s once
nothing is.

## Radio configuration — the two traps

**1. Clearing `EN_AA` makes the radio deaf, silently.** The obvious way to
sniff without ACKing is to disable auto-ACK. But `EN_AA == 0x00` drops the
nRF24L01+ into legacy nRF2401 ShockBurst, whose on-air format has no 9-bit
packet control field. The remote transmits Enhanced ShockBurst *with* one, so
every packet decodes misaligned, fails CRC, and is discarded before reaching
the FIFO — a receiver that is correctly tuned, correctly addressed, and
receives nothing at all. `radio.set_silent()` instead leaves auto-ACK on
**pipe 0 only** (`EN_AA = 0x01`), which keeps Enhanced ShockBurst alive while
pipes 1 and 2 stay mute. Pipe 0 is never opened for RX so it can never match.

**2. `radio.send()` flushes the RX FIFO.** Every probe destroys packets that
arrived but hadn't been read. An earlier version probed 12 channels every
1.5 s and lost whole sessions of button presses to this.

## Channel behaviour — measured

The Hub does **not** sit still, contrary to the Harmoino write-up. Measured
over 60 s with no button activity: exactly one channel answers at any moment,
but which one changes on its own every 10–25 s, and at times it churns
sub-second (three channels answered partially within a few hundred ms).

Consequences, all measured on this rig:

- Full 12-channel probe sweep: **~176 ms** of deafness (dominated by FT232H
  USB latency, not radio retries).
- Single-channel re-check: **~13 ms**.
- `radio.available()`: **~1.5 ms** per call. This caps polling at ~650/s.
- A channel found once goes stale well within a session, so a permanent lock
  is wrong. `sniff()` re-probes only after a quiet interval.

**Passive sniffing of the live link is unreliable.** The remote does not sweep
all 12 channels on a short gap (a fixed-channel listen on ch 5 for 60 s with
15 presses caught nothing), and the Hub hops. Chasing it from outside caught
roughly 2 packets per 8 presses.

**Hub impersonation is reliable.** With the real Hub powered off, sitting on
one channel with ACK enabled captured 282 packets from 10 presses — 10 button
reports and 10 releases, 1:1. This is also the project's end goal, so it is
the recommended capture mode.

Locating the remote when nothing is known: the nRF24's Received Power
Detector (register `0x09`) sees RF energy regardless of address, CRC, or
packet format. That is how the remote's transmit channels (62 and 65) were
found after packet-level capture had failed on every other theory.

## Software

`src/harmony_receiver/`, installed editable as `harmony_receiver`:

- `protocol.py` — constants, addressing, HID-aware frame decoding. No hardware.
- `hid.py` — HID usage tables (keyboard page 0x07, consumer page 0x0C).
- `events.py` — `RemoteEvent` (kind, signature, report, usage, label).
- `tracking.py` — `PressTracker`: frames → press/repeat/release, with a
  timeout backstop so a lost release can't leave a button stuck down.
- `profiles.py` — `ButtonMap`, JSON-backed labels keyed on signature.
- `capture.py` — `CaptureLog`, JSONL recording for offline re-analysis.
- `radio.py` — FT232H/nRF24 bring-up, `set_silent()` / `set_transceiver()`,
  and `release_radio()`. The last one matters more than it looks: a
  `DigitalInOut` claims a pin for the life of the process, so without handing
  CE/CSN back, restarting the hub from the settings page would fail its second
  start with a "pin in use" error indistinguishable from an unplugged radio.
  Also `set_promiscuous()` / `reset_radio()`, the address-agnostic sniffing
  mode `pairing.sniff_network_address` uses.
- `framing.py` — `find_addresses()`: recovers real Harmony addresses out of
  a promiscuous capture by brute-forcing every bit offset and keeping only
  the ones whose trailing bits are a genuine CRC-16 of everything before
  them. No hardware; pinned to synthesised frames the same way `protocol.py`
  is pinned to captured ones.
- `receiver.py` — `HarmonyReceiver`: pipes, channel following, frames/events.
- `dispatcher.py` — pub/sub.
- `pairing.py` — network address discovery, cancellable via `should_stop` so
  the app's "Find my remote" can give up without abandoning a thread that is
  still holding the radio. Two independent ways in: `discover_network_address`
  needs a real Harmony Hub in pairing mode and answers in seconds;
  `sniff_network_address` needs no Hub at all, listening for the remote's own
  transmissions instead via `framing.find_addresses`, at the cost of being
  slower and depending on catching real traffic rather than a Hub answering
  on request. Ported from LeoKlaus/Equilibrium's `discover_remote_address.py`,
  the first published version of this technique for a Harmony remote --
  simplified here because `HarmonyReceiver._resync_from_first_packet` already
  self-corrects a wrong LSB from live traffic, so this needs no equivalent of
  their 255-candidate byte sweep, and its final "does this actually work"
  check reuses `HarmonyReceiver` itself rather than a separate verification
  path.
- `cli.py` — `pair` (`--without-hub` for the hub-less method), `listen`,
  `capture`, `analyze`, `learn`, `scan`, `benchmark`.

50+ unit tests, all passing, pinned to real captured payloads (protocol,
receiver) or synthesised ones proven against the real CRC-16 math (framing,
hub-less pairing).

### Typical session (Hub powered off)

```
python main.py capture --address 17129BFCB6 --channel 62 \
    --probe-interval 0 --allow-ack --seconds 240 \
    --note "full sweep" --out captures/sweep.jsonl
python main.py analyze captures/sweep.jsonl
python main.py learn --from captures/sweep.jsonl --all
python main.py listen --address 17129BFCB6 --channel 62 --probe-interval 0 --allow-ack
```

If the remote's channel is unknown, run the RPD energy scan to find where it
is actually transmitting.

## Button map

`buttons.json` — 48 buttons. Named automatically from HID: digits 0–9, arrows,
Enter/Keypad Enter, Volume Up/Down, Mute, Channel Up/Down, Play, Pause, Stop,
Record, Fast Forward, Rewind, Program Guide, Quit, Media Select Home, AC Back,
Application/Menu, Keypad −.

Confirmed by ordered test: Red `C3F70100`, Green `C3F60100`,
Yellow `C3F50100`, Blue `C3F40100`.

The rest sit in Logitech's vendor-specific ranges, which no HID table can
name, and were labelled by hand:

| range | buttons |
|-------|---------|
| `0x01E8`–`0x01ED` | the four activity keys: Toggle Music/Movie/TV Mode, On/Off |
| `0x0FF0`–`0x0FF5` | the six SmartHome keys: +/−, bulb upper/lower, socket upper/lower |
| `0x01F4`–`0x01F7` | the four colour keys |
| `0x01FF` | Info |

## Hub platform (`src/harmony_hub/`)

The layer that decides what a press *does*. `harmony_receiver` answers "which
button"; this answers "and what should happen". Kept as a separate package so
the RF work stays usable on its own and so all of this is testable with no
radio attached.

### Model

Borrowed from the Harmony Hub's own design, because two of its ideas are not
obvious and are what make a remote hub feel right:

* A **scene** is a *context*, not a macro. While active it decides what every
  button means, so Volume Up can reach the AV receiver in one scene and a
  soundbar in another. Unbound buttons fall through to whichever scene
  `global_scene` names -- a reference, not a second bindable set -- which is
  also the only thing in effect when no scene is running, exactly the state
  the remote's activity buttons must work from.
* Devices carry a **power policy** (`managed` / `leave_on` / `manual`) rather
  than being blindly switched. When one scene switches straight to another,
  `SceneEngine._stop_actions` drops the outgoing scene's power-off for any
  device the incoming scene still needs, or whose policy is `leave_on`;
  `manual` devices get no power command from a scene macro at all, in either
  direction. Only a scene switch is diffed this way -- a button bound
  straight to a power command still sends it, policy or not. Going to idle
  (an explicit stop, or the off button) has no incoming scene to spare a
  device for, so a `managed` device still goes off there, and only there.

A **binding** splits a press into `on_press` / `on_repeat` / `on_hold` /
`on_release`. `on_repeat` matters because the remote emits a packet every
~100ms while held: volume should ramp, power must not. `on_hold` delays
`on_press` until the press resolves, but *only* for buttons that define a
hold -- paying that latency everywhere would make the remote feel sluggish.

`SceneEngine.paused` (Settings, inside Event source, once it is not `none`)
logs every press exactly as normal but never reaches a backend -- for trying
a real remote, or a replay, against real hardware without a press actually
touching a device. The single choke point is `run_actions`, not the button
handler: pressing does not gate the dispatch itself, so hold-vs-tap timing
and repeat throttling stay correct regardless of when pausing happened,
including a hold timer that was already armed before the toggle. `run_actions`
reports what it skipped the same way `_stop_actions` already reports a
power-off it dropped -- an `"action"` event per suppressed step, not a
silent no-op -- so a scene switch triggered while paused still updates
`active_scene` and is visible in the log, just without its macro's device
commands actually running. Resets to unpaused on every hub (re)start, the
same as `active_scene` and focus -- it is not settings and is not persisted.

`repeat_delay` and `repeat_interval` are ordinary auto-repeat, the same two
knobs a keyboard has, and they are not optional comfort. The remote says a
button is down every ~100ms and never says how long it has been down, so
without a delay an ordinary 300ms press -- which is what a short press
actually measures -- fires the repeat actions three or four times and is
indistinguishable from pressing the button four times. Waiting is the only
thing that separates "held" from "pressed".

They live on `HubConfig` as `default_repeat_delay` / `default_repeat_interval`
(0.5s and the remote's own cadence, by default), not on every `Binding` --
because in practice every button that repeats wants the same timing, and a
copy on each one would mean tuning them all by hand for one preference.
`Binding.repeat_delay` / `repeat_interval` exist too, but only as an
`Optional[float]` override for the one button, usually something slow like a
blind or a projector lens, that genuinely needs different timing; `None` (the
default) follows the config-wide setting. The app exposes the config-wide
pair from the Scenes tab, next to the global scene picker, and the
per-button override as a "Custom timing for this button" switch in the
binding editor that starts from the current default rather than zero, so
turning it on does not itself change what the button does.

`default_repeat_accel` / `default_repeat_accel_seconds` (and their
per-`Binding` overrides `repeat_accel` / `repeat_accel_seconds`, same
override convention as above) layer an exponential ramp on top of the flat
rate: the longer a button stays held, the faster it fires, up to
`repeat_accel` times the base rate once `repeat_accel_seconds` of holding
has passed. `repeat_accel` at `1.0` (the default) disables this entirely.
It exists because the flat rate alone tops out at the remote's own ~100ms
reporting cadence -- `default_repeat_interval` at its default of `0`
already fires on every packet the remote sends, with no headroom left to go
faster by shrinking the gap further. Past that ceiling the only way to go
faster is to run the repeat actions more than once for a single packet, so
`SceneEngine._repeats_due` (renamed from `_should_repeat`, since it now
returns a count rather than a bool) returns 0, 1, or more per packet: a
`_credits` accumulator banks fractional progress between packets so the
count climbs smoothly instead of jumping in whole-repeat steps, and
`_ramp_elapsed` tracks how long the ramp itself has been running --
separately from wall-clock "time held" -- so a burst of packets queued up
behind a slow backend cannot all cash in at the top of the ramp the instant
the backend catches up (that gap is additionally capped per-packet by
`MAX_REPEAT_DT`). `MAX_REPEAT_BURST` (8) hard-caps how many times one
packet may fire, whatever `repeat_accel` is configured to. `run_actions`
gained `announce` / `label_suffix` kwargs so a wide burst logs once, as
`"volume_up ×8"`, instead of flooding the live view with one entry per
underlying command -- failures are never silenced, on any iteration of a
burst. The app exposes this the same way as the flat rate: two more
sliders in the "Default repeat timing" dialog and the per-button "Custom
timing for this button" section, the second only shown once the first is
above `1×`.

An **action** is one of `device` (send a command), `scene` (switch or stop),
`delay`, `adjust` (step whatever the SmartHome +/- keys are focused on),
`if` (branch on a condition), `set` (remember a value) or `wait_for` (poll a
condition instead of guessing with a fixed delay) -- see "Conditional
actions, values, and variables" below for the last three. Scene switching
being an ordinary action is what lets the remote's activity buttons work
through normal bindings instead of a special case in the engine.

### Backends

A backend is any way of reaching equipment. The engine only ever calls
`send()`, so new device types need no changes to it. Two methods exist purely
so the editor can be generated rather than hard-coded per backend:
`config_schema()` drives the device form, and `commands()` drives the binding
dropdowns -- which is also why a typo fails at configuration time instead of
silently at press time.

Shipped: `androidtv` (an Nvidia Shield or any Android TV, see below),
`homeassistant` (lights, switches, scenes and scripts, see below), `denon` (a
Denon or Marantz AV receiver, see below), `lgtv` (an LG webOS TV, see below),
`ir` (learns its own commands from a remote via a KY-022/KY-005 pair, see
below), `virtual` (records calls; for tests and UI work), `http` (each
command is a declared request), `shell` (pre-declared local programs only --
the hub has a web UI, so an action that could run arbitrary text would be
remote code execution).
Third-party backends are discovered through the `harmony_hub.backends` entry
point group, so they need no change here.

`denon` and `lgtv` both find themselves over SSDP, which is why the wire
protocol -- the M-SEARCH multicast, and reading `LOCATION` out of the replies
-- lives once in `backends/_ssdp.py` rather than twice. Each backend keeps
only what is actually its own: the search target and the `<manufacturer>`
filter.

Backends that need a one-time handshake implement `Pairable`, which is what
`POST /api/devices/{id}/pair/start` and `.../pair/finish` drive. A backend can
also offer `suggested_bindings()` -- button key to command name -- which seeds
the remote mapper described below. Both are optional: a backend that does
neither still works, it just gets a plainer setup.

Backends that can learn their own commands from a remote implement
`Learnable` instead -- currently only `ir`, see below -- which drives the six
`POST/GET/DELETE /api/devices/{id}/learn/*` routes the same way `Pairable`
drives pairing's two. Distinct from pairing: pairing is a one-time handshake
with the device *being controlled*; learning is a repeated capture from a
*different* remote, aimed at whatever the install's IR receiver is wired to.

`Pairable` also carries four strings -- `pair_label`, `pair_hint`,
`pair_input_label`, `pair_input_multiline` -- because the mechanism
generalises but the wording does not. A television shows a six-digit code; a
Home Assistant issues a two-hundred-character token from a web page. The app
had the Android TV wording hard-coded, which would have told half its users
to read a code off a screen that never shows one.

`pair_input_label = ""` is a fourth case: nothing to type back at all. An LG
webOS TV pairs with a press on its own remote, so the app shows a plain
confirm dialog instead of a text field, and the CLI (`pair.py`) waits for
Enter instead of prompting for input, then both call `pair_finish` with an
empty string.

### Android TV

Talks the Android TV Remote v2 protocol: the same one the Google TV phone app
uses, served by the Remote Service preinstalled on the Shield. Deliberately
not ADB, which would need developer options, network debugging, and an RSA
fingerprint accepted on the TV; here the entire setup is one six-digit code
shown on screen. Pairing is on 6467, control on 6466, both TLS.

The certificate that pairing produces lives in `credentials/` (gitignored),
not in `hub_config.json`: it is secret, machine-specific, and would otherwise
round-trip through the device form on every save.

Three things about it are worth knowing before changing it:

* `connect()` never raises. The engine only registers a backend whose
  `connect()` returned, and an unregistered backend cannot be reached by the
  pairing routes -- so a device that raised on a missing pairing could never
  be paired. Failures become `health()` states instead, and a background task
  retries the ones retrying can fix.
* The client library's own reconnect loop only runs once a connection has
  succeeded at least once, so the first attempt and the retry after it are
  ours; reconnection after a drop is the library's.
* `power_on` and `power_off` read the device's state before acting, because
  POWER is a toggle. Sending it blindly to an already-on TV under a managed
  power policy would switch it off on every scene change.

Pairing normally happens in the app. `python -m harmony_hub.pair <device_id>`
does the same thing from a terminal for a headless install, and works for any
`Pairable` backend.

### Home Assistant

Lights, switches, scenes, scripts, covers, media players -- anything a Home
Assistant already controls. Talks its REST API: `POST /api/services/...` per
command, `GET /api/states` to see what exists, `GET /api/config` to check the
token and read the version. No new dependency; `httpx` was already here.

This is the first backend whose vocabulary is not fixed, and that difference
is what shapes it. Every other backend talks to one box that does a known
number of things, so its command list is written once in Python. A Home
Assistant has hundreds of entities and they differ in every house.

Four decisions follow from that:

* **The entities are chosen, and only the chosen ones become commands.**
  `config["entities"]` names the handful worth putting on a remote. Expanding
  everything would produce thousands of dropdown rows; the obvious
  alternative -- one `turn_on` command with the entity id passed as a
  parameter -- is exactly the "typo fails silently at press time" failure the
  `Command` interface exists to prevent, and the app has no parameter UI
  anyway. Picking from a live list makes a wrong entity id impossible rather
  than merely late, which is the same bargain the `http` backend makes by
  declaring its requests up front.
* **A command is `verb:entity_id`** -- `toggle:light.kitchen`,
  `brighter:light.sofa`, `activate:scene.movie_night`. A verb cannot contain
  a colon, so the split is unambiguous, and the pair reads correctly in a
  binding and in the live log. The verbs come from the entity's domain: a
  light gets on/off/toggle plus `brighter` and `dimmer`, a scene gets
  `activate`, a script gets `run` and `stop`. Anything the tables cannot
  express is a declared action in `config["actions"]` -- a raw service call
  with whatever data it needs, which is the escape hatch that keeps the
  design from having to anticipate everything.
* **The token is not configuration.** A long-lived access token is
  unrestricted, and `GET /api/config` is readable by anything on the LAN. So
  it lives in `credentials/` beside the Android TV certificates, and gets
  there through the `Pairable` conversation. `pair_finish` checks the token
  against the instance *before* writing it, because Home Assistant shows a
  token exactly once -- a rejected one has to be reported while it is still
  on screen.
* **`brighter` and `dimmer` are relative.** They send `brightness_step_pct`,
  which needs no state read, which is what makes them safe to fire on every
  repeat packet. Holding the remote's SmartHome **+** key ramps a light the
  same way holding Volume Up ramps the Shield. `brighter` is the one
  exception that still reads state: an off light has no brightness to step
  from, so it checks first and turns the light on at a fixed level
  (`BRIGHTNESS_TURN_ON_PCT`, 20%) instead of sending a step Home Assistant
  would have to guess a base for.

Two things are worth knowing before changing it:

* **`mute` on a media player reads state first.** Home Assistant has no
  toggle for it -- `volume_mute` is told which way to go -- and a remote's
  mute key is a toggle. Same reasoning as the Shield's `power_on`. Every
  other verb is idempotent and reads nothing, because unlike POWER, Home
  Assistant's `turn_on` and `turn_off` mean what they say.
* **`health()` also checks the configuration still fits the instance.**
  Renaming an entity in Home Assistant breaks every binding pointing at it,
  and nothing else in the system would notice until the button was pressed;
  the device list says which ones went missing. It distinguishes "the state
  list could not be read" from "the state list did not contain it" with an
  explicit flag rather than inferring it from emptiness -- conflating those
  would report every binding as broken the moment one request timed out.

There is no reconnect loop, unlike Android TV: there is no socket to hold, so
a request either works or it does not. `health()` re-probes on a ten-second
timer, so an instance that was down at startup heals as soon as anyone looks
at the device list, and the same cache stops the app's polling from putting
two requests per second on the Home Assistant box.

The remote's six SmartHome keys are what `suggested_bindings()` and
`suggested_adjust()` fill in -- the two bulb keys toggle the first two lights
picked, the two socket keys the first two switches, and the +/- keys are left
to follow whatever gets touched (see **Focus, and the +/- keys** below). Those
are precisely the keys the Android TV backend leaves alone, so those two map a
remote between them without overlapping. The Denon backend is the deliberate
exception, and the reasoning is in its own section below.

**Home Assistant scenes are not hub scenes.** A hub scene is a context that
remaps every button; a Home Assistant scene is a saved set of entity states.
They compose rather than compete -- a hub scene's `on_start` activates a Home
Assistant one -- and the command labels say `Scene: Movie Night` so the two
are distinguishable in a dropdown that lists both.

### Denon

Denon and Marantz AV receivers, over the control protocol they have spoken
since the RS-232 days: short uppercase strings -- `MVUP`, `PWON`, `SISAT/CBL`
-- carried over the LAN unchanged. No cloud, no account and no pairing, which
is why this is the smallest of the three network backends.

**One vocabulary, two transports.** The same strings travel either over telnet
on port 23 or as a bare HTTP query on 8080
(`/goform/formiPhoneAppDirect.xml?MVUP`), and which of the two a given unit
answers on turns out to vary by model and by firmware. So the protocol strings
are written once and the transport is a field in the device form rather than a
fork in the command table; a test asserts both paths put identical bytes on the
wire, because a binding that changed meaning when somebody flipped a dropdown
would be a miserable thing to debug. HTTP is the default.

Things worth knowing before changing it:

* **`Network Control` has to be `Always On` on the receiver.** Denon's default
  powers the network interface down in standby, so the unit leaves the network
  entirely, cannot be woken remotely, and every integration against it looks
  simply dead. It is the first thing to check when one reports as unreachable.
* **The telnet path opens a connection per command and closes it again.** A
  receiver accepts exactly one telnet client, so a socket held for the evening
  is one the Denon phone app and Home Assistant's own integration cannot have.
  A few milliseconds is a fair price for not owning the only slot.
* **Inputs are chosen; everything else is fixed.** The config key is named
  `entities`, which is the name the "Choose entities" picker, its API route and
  its stale-id detection are all keyed on -- so thirteen sources become the
  three a room actually uses, with no app or API change. Power, volume, mute,
  surround, the on-screen menu and Zone 2 exist on every unit and are always
  offered, because there is nothing there to choose. The picker needs no live
  receiver, since somebody setting this up for the first time is very likely to
  be doing it with the thing in standby.
* **Mute reads before it writes.** The protocol has `MUON` and `MUOFF` but no
  toggle, and a remote has one Mute button. Remembering the state here instead
  would desync the first time somebody picked up the receiver's own remote.
* **It never takes the focus.** `focus_for()` is left at the default, so
  touching the receiver does not swing the SmartHome +/- keys onto it. Volume
  has keys of its own.
* **Reading state back is split across two paths, deliberately.** Power,
  source and mute go through `READABLE`/`_read_state`, which both transports
  answer -- HTTP from the Lite status document, telnet from three queries.
  Surround (`readable()`'s fourth target, telnet only) does not: confirmed
  against a real AVR-X2700H that the Lite document does not carry it, the
  full document 403s, and every `AppCommand.xml` variant answers empty, so
  `_read_surround` asks `MS?` directly rather than joining `READABLE` and
  silently going blank on HTTP. What it reports is the receiver's *resolved*
  mode for the current signal (`NEURAL:X`, `DOLBY DIGITAL`, ...), not the
  category last selected (`MSMOVIE`, `MSMUSIC`, ...) -- confirmed that
  sending a category command with nothing playing changes nothing this
  reports, so a condition comparing it against a fixed category only means
  something while audio is actually flowing.
* **It overlaps the Android TV backend on volume and the arrows, on purpose.**
  The receiver is the thing that actually changes the volume in a room that has
  one, and its setup menu needs the same arrows a television's does.
  Suggestions are reviewed before they are applied and the mapper works one
  device at a time, so the overlap costs a glance rather than a mistake.
* **It finds itself over SSDP, not the mDNS the other two network backends
  use.** Denon announces over `239.255.255.250:1900` rather than Bonjour, so
  discovery here is a from-scratch M-SEARCH-and-parse rather than a third copy
  of the shared `zeroconf` helper. It matches on `<manufacturer>` in the UPnP
  description document (`denon`/`marantz`, matched loosely so `DENON
  PROFESSIONAL` counts) rather than trying to also rule out HEOS speakers and
  soundbars that answer the same search: excluding those needs a probe of the
  status endpoint, and newer AVR firmware answers that one with 403 while
  taking commands perfectly well, so a stricter filter risks hiding a working
  receiver to screen out a speaker the picker costs one glance to skip. Same
  failure mode as the other two: nothing answering is an empty list, because
  the address field is still there to type into -- and a receiver in standby
  with `Network Control` off is not on the network at all, so it will not
  appear here either.

Commands that carry a number -- `volume`, `sleep` -- check the range before
building the string, because the digit count *is* the command: `MV1000` is not
a rejected volume, it is a different instruction.

Newer firmware serves commands happily while answering 403 for the status
document, so a refusal there is recorded as *reachable*: losing the "on ·
Blu-ray · muted" health line is a smaller thing than reporting a working
receiver as broken.

### LG webOS TV

Every webOS TV since 2014 runs a websocket control server (port 3000,
falling back to TLS on 3001) speaking SSAP -- the same protocol the LG ThinQ
app and the TV's own Magic Remote use. The library is `aiowebostv`, the same
one behind Home Assistant's own `webostv` integration; it needs Python
&ge;3.11, which is why that is now the floor in `pyproject.toml`.

Three things about it are worth knowing before changing it:

* **Registering and showing the pairing prompt are the same network round
  trip.** Android TV separates proving identity (a certificate, checked
  instantly) from human approval (a code typed back once); webOS folds both
  into a single `connect()` call that blocks on the prompt -- but only for
  the library's own short receive window, well under the time a person
  actually needs to walk to the TV and find its remote. So `pair_start()`
  does not make one attempt and wait: it keeps re-registering in the
  background, each attempt re-showing the prompt, for up to
  `PAIR_RETRY_WINDOW` (90s). This is the one part of the backend not
  exercised against a real set -- worth confirming a repeated registration
  actually refreshes the on-screen prompt rather than stacking several up.
* **`power_on` is Wake-on-LAN, not SSAP.** `system/turnOn` only works once a
  websocket is already open, which by definition it is not on a TV that is
  fully off -- the same gap Home Assistant's integration papers over with an
  automation the user has to wire up themselves. Here it is built in: a
  magic packet to the TV's MAC, which needs "Mobile TV On" (or "Turn on via
  Wi-Fi") enabled on the TV. The MAC does not have to be typed in --
  `connectionmanager/getinfo` reports it, so every connection caches it in
  the same JSON file as the client key, and the config field is only an
  override for when that lookup fails.
* **`power_on` wakes every interface the TV reports, not one guessed at.**
  `connectionmanager/getinfo` returns `wiredInfo` and `wifiInfo`
  unconditionally, with no `state` or `connected` field saying which one is
  actually carrying traffic -- a TV on wifi still reports a MAC for its
  empty Ethernet socket. The first version of this backend preferred
  `wiredInfo`, which meant power-on silently never worked on a wifi-only
  set: confirmed on a real G2, where the cached wired MAC produced no
  response at all and the correct wifi one woke it in ~6s. There is nothing
  to choose between the two, so `power_on` now sends a packet to both --
  one to an interface nothing is listening on costs nothing -- and
  `_refresh_macs` re-reads both on every connection rather than only when
  nothing is cached yet, so an install still holding the old single wrong
  address self-corrects the next time the TV is reachable rather than
  needing the credentials file deleted by hand.
* **Because no key means an unregistered client, `connect()` refuses to try
  without one on file.** Building a client with no key sends registration --
  which is what puts the prompt on the TV -- so doing that on every ordinary
  hub startup and retry would flash a prompt at an empty room. An unpaired
  device reports `unpaired` and stops there; only `pair_start()` deliberately
  builds a keyless client. A key the TV has since revoked hits the same
  state through `WebOsTvPairError`, for the same reason: retrying cannot fix
  a pairing only a human can redo.

Inputs and apps are chosen the same way Denon's inputs and Home Assistant's
entities are -- `config["entities"]`, the same "Choose entities" picker, one
named command generated per input or app actually picked rather than a
free-text `set_input`/`launch_app` left to typos. It finds itself over SSDP
like Denon, on `urn:lge-com:service:webos-second-screen:1`, matched on
`<manufacturer>` containing `lg electronics` -- checked against one real
description document, so the first thing to widen if a TV goes unfound.

### Infrared

The one backend that does not talk to equipment that already speaks a
protocol -- it learns codes off the original remote and plays them back
through a KY-005 LED, read by a KY-022 receiver. Both are one GPIO each.

**One receiver and one transmitter for the whole install, not per device.**
`HubSettings.ir_rx_pin`/`ir_tx_pin` live beside `csn_pin`/`ce_pin`, the same
kind of fact -- how this Pi is wired -- rather than in each IR device's own
config, which would mean N copies of the same two numbers and an
arbitration rule for who wins. Every `IrBackend` resolves the one shared
`ir.gateway.IrGateway` singleton per operation instead of holding a
connection, which is what lets the pins be changed from Settings and applied
immediately: `HubRuntime.apply_settings` calls `ir_gateway.reconfigure()`
directly, with no restart and nothing for any backend to rebuild. The radio's
CSN/CE pins, by contrast, are read once when a `RadioSource` opens and do
still need a restart to move.

**pigpio, not `RPi.GPIO`/`lgpio`.** The KY-005 has no onboard oscillator, so
sending a code means generating the 38kHz carrier itself in software at
microsecond accuracy over tens of milliseconds -- bit-banging that from
Python cannot hold the timing. `pigpiod`'s DMA-timed waveforms can. One
consequence worth knowing: the carrier frequency cannot be learned. By the
time a capture reaches the GPIO the KY-022 has already demodulated it away,
so a Philips/RC5 remote at 36kHz still captures fine but has to be *told*
36kHz on the device form (`carrier_hz`, per-equipment, unlike the pins) or
playback comes out at the wrong rate.

**RX is muted during TX**, because both modules sit on the same board with
no optical separation -- without this, "test this code" would re-enter the
receiver as a phantom capture.

**A `pigpiod` that was not running yet retries on its own.** `configure()`
only runs at hub startup or on an IR settings save; if the daemon was down
at that one moment (the ordinary case on a fresh Pi, before `sudo systemctl
enable --now pigpiod` has been run), the gateway used to stay recorded as
unreachable until the process was restarted -- confirmed the hard way on the
real device, where installing and starting `pigpiod` after the hub was
already up did nothing until then. `capture()` and `transmit()` now call
`IrGateway._ensure_connected` first, which retries the connection if pins are
wired but nothing is connected -- so starting the daemon takes effect on the
next learn attempt, no restart needed. `IrBackend.learn_start` mirrors this:
a genuinely unwired pin (`gateway.rx_configured` false) is refused instantly
and by name, but a wired pin whose daemon connection is currently down is
not pre-judged -- the job is attempted, and `gateway.capture()` reports the
real, specific reason if it still cannot connect, rather than the two cases
sharing one misleading "no pin configured" message. A learn job that ends in
`"failed"` or `"mismatch"` also releases the receiver automatically, for the
same reason: a device that hit exactly this daemon-not-running bug would
otherwise keep parking the shared receiver on a dead job that a hub restart
was the only way to clear.

**Learning takes two agreeing presses, not one.** A partial or noisy single
capture is the most common way IR learning goes wrong; `ir.learn.IrLearnJob`
waits for a second press of the same button and requires `ir.normalise.agree`
before reporting `"captured"`, and a disagreement is `"mismatch"` rather
than an error -- retried by simply starting again, not surfaced as a dead
end. There is exactly one receiver, so only one learn job runs at a time
across every IR device; a second device asking while one is in flight is
refused with the owner named, the same way `DiscoveryJob` refuses a second
concurrent address search.

**Raw timings, not decoded values, are what gets stored.** Same reasoning
`buttons.json` already applies to RF signatures: there is no formula turning
a capture into "volume up," and none is needed for a fixed, small set of
buttons learned once each. `ir.normalise.decode` (best-effort NEC / Sony
SIRC / RC5) exists purely to put a label next to the raw capture in the UI --
it is never what gets replayed, so a decoder being wrong or blank never
costs a working command. Codes live one JSON file per device
(`codes/ir_<device_id>.json`, `credentials/`'s sibling under `data/` on the
Pi), not in `hub_config.json`, for the same reason pairing artefacts don't:
a codeset is dozens of commands' worth of timing arrays, and the config file
is rewritten wholesale on every scene edit.

**Learning is a capability, like pairing.** `backends.Learnable` mirrors
`Pairable`'s shape (including per-backend wording, `learn_label`/`learn_hint`)
and is advertised on `BackendInfo` the same way `pairable` is, so the app
never keeps its own list of which backend names support it. A command
learned under a name that matches a real remote button key (`volume_up`,
`channel_down`, ...) makes `suggested_bindings()` map it onto that button
automatically -- the learn screen offers exactly those names to pick from,
which is what turns learning and binding into one flow instead of two.

Power is the one place this needs a human decision the model already
supports: most IR gear has only a toggle, not discrete on/off codes, so a
toggle-only device wants `power_policy: manual` (or both `power_on_command`
and `power_off_command` pointed at the same learned `power_toggle`) --
sending an unconditional "off" to something already off turns it on.

### Focus, and the SmartHome +/- keys

The remote has six SmartHome keys: two bulbs, two sockets, and a +/- pair
next to them. The bulb and socket keys bind to an ordinary `toggle:entity_id`
command like anything else. The +/- keys are different on purpose -- there is
no single light or speaker they should always reach, because "the thing I
want to adjust" is whatever was last touched, not something fixed at
configuration time. That is what `AdjustAction` and the engine's *focus*
exist for.

* **The engine remembers what was touched, not the binding.** After every
  successful `DeviceAction`, `SceneEngine._update_focus` asks the backend
  `Backend.focus_for(command)` what the command acted on. A backend that
  recognises it (Home Assistant, for any exposed entity) hands back a
  `FocusTarget`; most backends answer `None` and never take the focus --
  which is why pressing Volume Up on the Shield does not steal the +/- keys
  away from a light touched earlier. The focus follows *anything* touched,
  adjustable or not: toggling a switch takes it just as much as dimming a
  light, so pressing + right after a switch honestly reports "nothing to
  turn up" rather than reaching past it to an older light.
* **`AdjustAction` (`type: "adjust"`) has a `direction` and nothing else it
  needs.** At press time the engine resolves it against the current focus,
  asks that device's backend for `adjust_command(target, direction)`, and
  sends whatever comes back. `device`/`target` on the action are only a
  fallback for when nothing has been touched yet -- right after a restart,
  say -- and are otherwise ignored once a real focus exists.
* **Adjusting never moves the focus.** Only a `DeviceAction` can claim it;
  ramping a light with + must not re-stamp what + is pointed at.
* **The focus survives a scene switch**, and is cleared by `stop()` or by
  `reload()` dropping the device that set it. Which light was last touched
  has nothing to do with which scene is running.
* **Repeat timing is not special-cased.** An adjust binding follows the same
  `default_repeat_delay` / `default_repeat_interval` / `default_repeat_accel`
  / `default_repeat_accel_seconds` (or its own override) as any other
  binding -- no new setting was added for this.
* **`GET /api/state` reports the focus** as `{device, target, label,
  can_adjust}`, and `GET /api/devices/{id}/suggested_bindings` returns an
  `adjust` map (button key to `"up"`/`"down"`) alongside the usual
  `bindings` map, which is what lets the remote mapper offer "follow the
  last device touched" as a pick for the +/- keys.

### Conditional actions, values, and variables

A scene's macros were, until now, an unconditional list: every action ran,
every time. That is wrong for equipment whose own state matters -- "turn the
TV on, wait, then pick the input" should not power-cycle a TV that is
already on. Four pieces close that gap, all built on one new backend
capability.

* **`Readable` is a mixin, alongside `Pairable` and `Learnable`.** A backend
  that implements it offers `readable()` (what it can report the state of --
  `StateTarget(target, label, values, description)`, mirroring `Command`) and
  `read_state(target)` (the current value, as a plain string). Implemented by
  `androidtv` (power, app -- read from the client's own cached state, no
  round trip), `lgtv` (power, app, volume, muted -- same, off `tv_state`),
  `denon` (power, source, muted -- a real query, since there is no live
  cache), `homeassistant` (one target per *exposed* entity, its `state`
  string) and `virtual` (entirely config-driven: `state` seeds initial
  values, `state_effects` maps a command to what it changes, `unreadable`
  names targets that always fail, for testing `on_unreadable` without real
  equipment that is actually offline). `ir`, `http` and `shell` are
  one-directional by nature and do not implement it -- a backend either has a
  return channel or it does not, so this is a hard `isinstance` check
  throughout, the same as `Pairable`/`Learnable`. `GET
  /api/devices/{id}/readable` and `GET /api/devices/{id}/state/{target}`
  expose it (a 400 if the backend is not `Readable`, a 409 if it is but the
  read failed right now), and `BackendInfo.readable` on `GET /api/backends`
  is what lets the app's condition editor know which devices to offer
  without hard-coding backend names, same as `pairable`.
* **`Value` is one type with three shapes**, used everywhere a scene needs to
  refer to a piece of data: `StateValue` (a device's live state -- `device` +
  `target`), `VarValue` (something a `set` action stored earlier, by name),
  `LiteralValue` (a value fixed in configuration). A `Condition` compares two
  `Value`s (`left`/`op`/`right`); `op` is `is` / `is_not` / `contains` / `in`
  / `gt` / `lt` (the last two try to parse both sides as numbers) or `known`
  / `unknown` (whether `left` could be read at all, ignoring `right` and
  `on_unreadable` -- that *is* the question for those two). `on_unreadable`
  (`"run"`, the default, or `"skip"`) decides what an unreadable `left` (or
  `right`) is treated as for every other operator: `"run"` matches the
  engine's behaviour before any of this existed -- a network hiccup never
  silently stops a macro -- `"skip"` is for the opposite case, where acting
  on a guess is worse than doing nothing.
* **Three new actions.** `IfAction` (`then`/`otherwise`, either side an
  ordinary action list -- nesting one `if` inside another's branch is just
  how "check several things in order" gets expressed) is the general form a
  single gated action reduces to. `SetAction` (`name` + `value`) remembers a
  value under a name for a `VarValue` to recall later -- in the *same* macro
  or a different scene's, deliberately: "the input the TV was on before this
  scene changed it" is exactly as useful from a different scene's stop macro
  as from this one's own. `WaitForAction` (`condition`, `timeout`, `poll`,
  `on_timeout`) polls instead of guessing with a fixed `DelayAction` --
  `on_timeout: "stop"` logs a timeout as a failure rather than continuing
  quietly, but (like every action here) never actually cancels the rest of
  the macro; nothing in `run_actions` lets one action's failure cancel its
  siblings, on principle.
* **A restore is not a new action.** A `DeviceAction`'s `params` stays
  untyped (`Dict[str, Any]`); a parameter written as a plain
  `{"type": "state", ...}` dict is recognised and resolved at the moment the
  action actually runs (`SceneEngine._resolve_param`), so "set the input back
  to whatever was `set` earlier" is an ordinary `DeviceAction` whose one
  parameter happens to be a `Value` instead of a fixed string.
* **`SceneEngine.variables`** is the store: `Dict[str, str]`, hub-lifetime,
  not persisted -- cleared by `stop()`, kept across `reload()` since saving a
  config edit is not a restart, exactly the same lifetime `focus` already
  has. `GET /api/variables` reports it. Reading a name nothing has `set` yet
  fails the same way an unreadable device does, so a condition's
  `on_unreadable` handling covers both without caring which kind of value it
  was given.
* **Reads are cached for one second** (`SceneEngine._read_device_state`,
  `STATE_READ_TTL`), so a macro that checks the same device's state twice
  costs one backend round trip, not two. `wait_for`'s poll loop bypasses the
  cache entirely on every iteration -- a cached answer there would be exactly
  the stale value it exists to poll past -- but still refills it, so an
  ordinary `if` checked right after a `wait_for` resolves sees the same
  answer the wait just confirmed rather than forcing a third read.
* **Everything that used to assume a flat action list now recurses.**
  `Scene.actions()` walks into an `if`'s `then`/`otherwise` (sharing its
  parent's location label rather than growing one per nesting level);
  `HubConfig._check_references` validates a device named inside a condition,
  a `set`'s value, or a `state`-shaped `params` entry, the same as it always
  validated a `DeviceAction.device`; `SceneEngine._start_actions` /
  `_stop_actions` filter power commands inside `if` branches exactly as they
  filter the top level, and drop an `if` outright once both of its branches
  have been filtered to nothing -- no reason to evaluate a condition whose
  answer changes nothing either way. `MAX_ACTION_DEPTH` is now shared between
  scene-switch nesting and `if` nesting rather than each getting its own
  bound; five levels of either is already pathological.
* **Reading state does not make a scene depend on that device.** Checking
  whether the AVR is on while a scene macro only otherwise touches the TV
  must not make `required_devices()` start protecting the AVR from power-off
  during an unrelated scene switch -- a condition is a read, not a claim of
  need, and `Scene.required_devices()` only ever looks at `DeviceAction`/
  `AdjustAction.device`, never at what a condition or a `set` happened to
  read from.
* **The app's editor** (`ValueEditor`/`ConditionEditor` in
  `app/lib/widgets/value_editor.dart`) is one widget for all three `Value`
  kinds -- a segmented Fixed/Device/Variable choice, with a live "Currently:
  ..." readout next to whichever device state is picked, refreshed on
  demand. `if`'s `then`/`otherwise` are edited on a full page pushed from the
  action dialog (`_IfBranchesPage`, two ordinary `ActionListEditor`s) rather
  than inline -- two nested `ReorderableListView`s do not fit inside a
  fixed-width dialog, the same reason the binding editor's own per-button
  macros already live on their own page.

### Event sources

`RadioSource` (the real receiver, polled on a thread because its loop
blocks), `ReplaySource` (a recorded capture, decoded through the same parser
and press tracker so timing and holds are real), and `ManualSource` (presses
injected over the API). The last two are what make the platform developable:
a scene editor can be built and demonstrated without anyone holding a remote.

### The supervisor, and why the page never goes down

The process is two layers, and the split is the point:

```
uvicorn + FastAPI + static UI      ← starts once, never restarted
HubRuntime                          ← supervises the rest
├── settings   (hub_settings.json)     persisted, editable over the API
├── config     (hub_config.json)       persisted, editable while the hub is stopped
├── buttons    (buttons.json)
├── broker     (EventBroker)           survives restarts, so the live log never resets
└── service                            ← restartable: engine + event sources
```

Everything rests on one rule: **`HubRuntime.start()` never raises.** A missing
FT232H, a typo'd address, a hand-edited `hub_config.json` that will not parse —
each used to escape FastAPI's lifespan and stop uvicorn serving anything at
all, which meant the one screen that could explain the failure was the screen
that failed to load. They are now a `failed` state, an error string, and an
event, all of which the settings page renders.

So the hub can be stopped, reconfigured and restarted underneath a page that
stays up throughout. Routes that genuinely need a live engine answer 409;
everything to do with *configuring* the hub keeps working while it is down,
because being down is when you most need it.

Two consequences worth knowing:

* **Bind host and port are saved but applied on the next process start.**
  Rebinding a live listener would move the page's own URL out from under
  whoever is editing it. `pending_restart` says when that has happened, rather
  than letting a change look applied when it is not.
* **An unreadable config is not silently overwritten.** The runtime stands an
  empty `HubConfig` in for a file that would not parse, so `PUT /api/config`
  refuses without `?force=true` — saving that stand-in would delete every
  scene the real file holds.
* **Address discovery yields the radio rather than refusing to run.**
  `HubRuntime.start_discovery` stops a hub that is listening on the radio
  before searching, and starts it again once the search ends — found,
  failed, or cancelled — via a `on_finish` callback `DiscoveryJob` runs in a
  `finally`, so the restore happens even if the tab is closed mid-search.
  `_start()` refuses in the other direction while a search is running, so a
  manual Start can't fight the search thread for the same hardware.

### Home Assistant bridge (`src/harmony_hub/bridge/`)

The opposite direction from the Home Assistant *backend* above: that one is
the hub controlling a Home Assistant instance; this one is Home Assistant
discovering the hub itself, over MQTT. `MqttBridge` publishes one device --
an activity `select`, a `scene` entity per hub scene, an `event` entity for
every remote button press, `paused`/`running` switches, and a
`binary_sensor` per configured device's health -- and accepts commands back
on `harmony_hub/<node_id>/cmd/#`, routed straight into `SceneEngine` (a raw
`cmd/send` topic goes through `run_actions` the same way a scene's own macro
does, so a Home Assistant automation can fire literally any command any
backend offers).

Owned by `HubRuntime`, the same tier as `EventBroker`, not by `HubService`:
Home Assistant needs to see this hub go `offline` exactly when the hub
itself fails or is stopped, so the bridge runs independently of whether the
hub is up, on its own reconnect loop with exponential backoff. Like
`ir_gateway.reconfigure`, a changed broker setting reconnects live --
`HubRuntime.apply_settings`'s `mqtt_changed` -- with no process restart
needed. Two things follow from being independent of `HubService`:

* **The broker password is not configuration**, for the identical reason
  the Home Assistant backend's access token is not: `credentials/mqtt_
  <node_id>.password`, never round-tripped back out through `/api/mqtt`.
* **Discovery removal is a two-step dance**, because Home Assistant does not
  infer "this component is gone" from it simply being missing from a
  republished device-discovery payload -- a key that disappears (a deleted
  scene) is announced as an empty, platform-only config first, then omitted
  on the publish after. `bridge/state_file.py` remembers what was last
  published across a restart, in a small JSON file next to
  `hub_settings.json`, rather than racing a retained-message read against
  the broker on every reconnect.

Tests (`tests/test_bridge*.py`) run against a fake MQTT client with the same
shape as `aiomqtt.Client`, the same discipline `test_hub_backends_ir.py`
keeps for a fake `pigpio` -- nothing here has been run against a real broker
or a real Home Assistant instance yet.

### Running it

```
harmony-hub                       # boots, serves, and configures itself from the UI
harmony-hub --source replay --replay captures/reference/full_remote_sweep.jsonl
harmony-hub --source radio --address 17129BFCB6 --channel 62 --allow-ack
```

Serves the API on port 8765 with interactive docs at `/docs`. Command-line
flags are *overrides for this run*, layered over `hub_settings.json` and not
written back: the settings file belongs to whoever edits it in the app, not to
whichever shell command last started the process. Nothing on the command line
is required any more — a wrong or missing value is reported in the UI instead
of exiting.

339 tests cover the model, engine, settings, supervisor, backends and API
with no hardware and no network attached.

## Control app (`app/`)

A Flutter app talking to the hub over its REST + WebSocket API. Web is the
only target built today; Android and iOS are configured for later, which is
why the layout already switches between a navigation rail and a bottom bar
and why nothing imports `dart:html`.

Four screens:

* **Live** — every button on the remote, each showing what it is bound to
  *right now*, resolved the way the engine resolves it (active scene first,
  then global). Buttons light up as they are pressed, and tapping one sends
  a real press through the engine. Alongside it, a log of what the hub did,
  with a filter/sort popup (`activity_filter.dart`). Its dimensions are not
  a hard-coded list -- they are read off whatever fields the events flowing
  through actually carry (`HubEvent.facets`), and it hides by exclusion
  rather than inclusion, so a hub event type nobody has configured yet shows
  up already visible instead of already filtered out.
* **Scenes** — start, stop, create, edit. The binding editor covers all four
  press phases together, because the interesting decisions are about how
  they relate.
* **Devices** — add and configure equipment, and fire a command at it
  immediately to check it works while setting it up. A Home Assistant also
  gets **Choose entities**: a searchable list, grouped by domain, of what
  that instance actually has. It writes into the form rather than saving, so
  it behaves like every other edit on the page, and it flags entities that
  were picked once and have since vanished from Home Assistant — every
  binding using one of those is broken, and this is where that becomes
  visible. The order entities are picked in is kept, because the suggested
  bindings point the two bulb keys at the first two lights.
* **Settings** — what the hub is running on, and whether it is running.
  Also where the remote's buttons get learned.

### Learning the remote

A button is identified by a four-byte signature, and no formula turns that
into "Volume Up" -- somebody has to press it and say what it is. That used to
be `harmony-receiver capture` followed by `harmony-receiver learn`, with a
capture file in between. The **Learn the remote** screen does the same job by
pressing buttons and typing names.

It needs no capture step because the engine already publishes an unlearned
press under its own hex instead of dropping it -- a decision made so a new
button stays visible in the live view, which turns out to be exactly what
makes learning from the UI possible. The signatures are already streaming
past; the screen just collects the ones nothing has named.

Consequences worth knowing:

* **Names arrive prefilled.** The HID usage is decoded already, so most
  buttons only need confirming. Logitech's vendor-specific ranges decode to
  nothing, and those rows come up blank and are skipped until named.
* **Typing a name that matches an existing button attaches to it** rather
  than clashing. That is the intended way to record the second signature a
  key emits under a different activity, and the screen says so as you type.
* **It works through a replay too**, because a recorded capture produces the
  same events. That is also how it is tested end to end.
* **It says when the hub cannot hear the remote at all** -- stopped, or
  running with the source set to None. Without that the screen is
  indistinguishable from a broken one: you press buttons and nothing happens.

`POST /api/buttons/learn` and `DELETE /api/buttons/{key}` are the whole API
surface, and both work with the hub stopped: naming what was already captured
needs no radio.

### Settings, and degrading instead of breaking

The settings screen exists because the hub used to be configured entirely by
command-line flags: correcting a typo'd radio address meant a terminal and a
restart. It covers the event source (radio address with a **Find my remote**
search, start channel, probe interval, ACK, pins; or a capture file to replay),
the file paths, the bind address, and Start / Stop / Restart.

It also carries the two testing affordances: **Run checks** verifies the hub as
configured — files writable, buttons loaded, source openable, whether a web UI
was even built — and **Try these settings** does the same against the values in
the form, without saving them. Both render into one list because both answer
the same question.

The rest of the app then has to survive the hub being down, or the screen that
fixes it would be unreachable in the one state that needs it:

* A banner says the hub is stopped or why it could not start.
* Live still draws every binding — those come from configuration, not the
  engine — but tapping is off rather than pretending to send.
* Scene Start/Stop is disabled; **creating and editing scenes and bindings
  keeps working**.
* Devices still save. Pairing, the remote mapper and "try a command" all ask
  the device what it can do, so they are disabled with a note saying why
  instead of silently vanishing.

### Mapping the remote

"Map the remote to this device" opens the same picture of the remote the live
screen uses, in assignment mode: tap a button, pick what it should do on that
device, apply. It targets either a new scene or one that already exists.

Three decisions shape it:

* **Only what is picked gets written.** Mapping into a working scene must be
  safe, so the mapper collects assignments and merges them; every binding not
  chosen there survives untouched. A new scene starts pre-filled from the
  device's suggestions, because there is nothing to lose in one that does not
  exist yet.
* **The suggestion is a default, not a rule.** Tapping Left Arrow preselects
  whatever the device proposed for it, and any other command is one tap away
  -- the picker is a searchable list because fifty commands is normal.
* **Nothing in it knows about any particular backend.** It is handed a command
  list, an optional suggestion map, and the target scene's existing bindings.
  Any backend that can say what it can do gets the same screen for free.

Buttons already bound in the target scene carry a corner dot and say what they
would replace, so an overwrite is visible before it happens rather than after.

The device form is generated from each backend's JSON Schema and command
lists come from the backend itself, so adding a backend needs no change to
the app, and a mistyped command becomes an impossible state instead of a
binding that silently does nothing. A property with an `enum` becomes a
dropdown for the same reason -- the Denon backend's choice of transport is one
keystroke away from silently staying on the wrong one if it is typed. A value
the enum has never heard of is offered alongside the real ones rather than
dropped, so opening the form cannot quietly rewrite a hand-edited setting.

### Building and running

```
cd app && flutter build web        # once, or after changing the app
harmony-hub                        # then configure it on the Settings screen
```

The server finds the built UI automatically and serves it at
`http://localhost:8765`. Without a build it serves the API alone, with
interactive docs at `/docs` — and now says so in the checks rather than only
in a startup log line. During app development, `flutter run -d chrome` talks
to a hub on the default port; override with
`--dart-define=API_BASE=http://host:8765`.

152 widget and serialisation tests. The widget tests build the real tree
against a fake hub, which is the only automated check that the app actually
runs rather than merely being served -- they caught a phone-width layout
overflow that a browser check at desktop size would have missed, and, most
recently, a pushed Settings sub-page that only listened for its own local
edits and never re-rendered on a live store event (see below).

## Release and remote update (`src/harmony_hub/update/`, `.github/workflows/`)

Two ways for new code to reach an already-deployed hub, sharing everything
past "where the bytes come from": the same allowlisted, signed-content-hash
bundle format (`update/manifest.py`, `update/bundle.py`), the same
stage/dependency-install/smoke-test/activate pipeline (`update/installer.py`),
the same versioned release directories and boot-time trial/rollback bookkeeping
(`update/state.py`, `update/launcher.py`). Config, `hub_config.json`,
`buttons.json`, and `credentials/` never leave a dev machine or a CI runner
either way -- the allowlist is the only thing that decides what goes in a
bundle, and it is deliberately a positive list, not a blocklist.

- **Push**, from a dev machine: `harmony-deploy push <target>` builds a
  bundle, signs it with an HMAC over the device's own update token
  (`update/auth.py`; the token itself never crosses the wire), and POSTs it
  to `/api/update`. This is the original mechanism and the one
  `harmony-deploy setup`'s SSH-based provisioning still uses to bootstrap a
  bare device.
- **Pull**, from GitHub, added alongside it: the hub itself checks a
  configured repo's releases (`update/source.py`, read-only against
  GitHub's REST API) on an interval (`update/check.py`, cached in
  `data/update_check.json` so `/api/update/available` costs nothing to
  poll), and the Settings screen's Software card offers an "Update
  software" button once it finds one. Installing downloads the release
  asset in the background and hands it to the same `installer.install`
  a push uses -- see `RASPBERRY_PI_DEPLOYMENT.md`'s "Updating from a
  GitHub release" for the operational side.

  No HMAC on this path -- there is no second party to sign with, only
  GitHub's TLS standing behind "this is really what the configured repo
  published." Accepted deliberately: the same risk class as
  `/api/update/rollback`, which already needs no signature, and turned off
  independently of the push path via `github_updates_enabled`.

  `.github/workflows/release.yml` is what actually publishes a release: a
  push to a `v*` tag runs the full test suite
  (`.github/workflows/tests.yml`, shared with `ci.yml`'s pull-request runs)
  and, once it passes, runs `harmony-deploy build` -- the same bundle-building
  code path `push`/`setup` use -- and attaches the resulting `.tar.gz` and
  `.manifest.json` as release assets.

Caught during this work, both since fixed: the web build's asset allowlist
used a fixed number of `web/*/*/...` glob levels and silently dropped any
file nested deeper (a real Flutter package asset sat exactly at that depth);
and a download failure during a GitHub-sourced install surfaced as an opaque
"unexpected error" instead of the same `ReleaseFeedError` message a failed
release-check already produces, because `update.source.download` did not
wrap `httpx` errors the way `fetch_latest` did. Both were caught by driving
the real app against a real (if fixture-seeded) deployed hub in a browser,
not by the unit tests alone.

## Open items

1. ~~**Power-state diffing** across scene switches~~ -- largely subsumed by
   `Readable`/`Condition` (see "Conditional actions, values, and variables"
   above): a scene can now check a device's actual power state before
   deciding whether to touch it, rather than the engine guessing from policy
   alone. What is still open is *using* it -- none of the shipped scenes
   guard their own power commands with a condition yet, and the suggested
   bindings/macros generated for a new scene do not reach for `if` on their
   own.
2. **Mobile targets.** The app is web-only today. Android needs `flutter
   create --platforms=android .` in `app/`; iOS additionally needs a cloud
   macOS runner to build from Windows.
3. **`0x4F` status reports** are not decoded. They carry a repeating `044C`
   value that also appears in the 5-byte ticks; meaning unknown, and not
   needed for button events.
4. **Modifier byte** of keyboard reports has always been `0x00`; untested
   whether the remote ever sets it.
5. **IR, untested against real hardware.** Everything up through
   `IrBackend`/`ir.gateway`/`ir.learn` is unit-tested against a fake `pigpio`
   (`test_ir_gateway.py`, `test_hub_backends_ir.py`) but has not yet run
   against a real KY-022/KY-005 pair -- worth confirming a real capture
   decodes cleanly, a real send is actually received by the equipment, and
   the carrier timing holds up under `pigpiod`'s real DMA scheduling rather
   than the fake's instant returns. Acting on an incoming IR remote as an
   `EventSource` (so a spare remote could drive scenes, the way the Harmony
   remote itself does) is a related but separate feature, not started.
6. **The Home Assistant bridge, untested against a real broker.** Same
   shape as item 5: `bridge/` is unit-tested against a fake MQTT client
   (`tests/test_bridge*.py`), verified against Home Assistant's documented
   MQTT discovery schema, and confirmed end to end against a real running
   hub for settings, connection attempts and the password flow -- but not
   yet against a real Mosquitto broker or a real Home Assistant instance.
   Worth confirming the discovery payload is accepted as written, entities
   show up with sensible names, and the two-step removal dance actually
   clears a deleted scene's entity rather than leaving it behind. Two
   deliberately deferred, more opinionated pieces: a `button` entity per
   remote button (opt-in, 48 of them), and an `update` entity wired to the
   existing GitHub install path.
