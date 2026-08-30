# Deploying on a Raspberry Pi 3 Model A+

Running the hub permanently on a Pi 3A+ with an nRF24L01+ PA+LNA module wired
directly to the headers.  This assumes the Pi already has a working headless Linux install and network access.

## Hardware wiring

| nRF24 pin | Pi pin (header) | Pi function |
|---|---|---|
| VCC | Pin 1 (3.3V) | see power note below |
| GND | Pin 6 (GND) | |
| SCK | Pin 23 (GPIO11) | SPI0 SCLK |
| MOSI | Pin 19 (GPIO10) | SPI0 MOSI |
| MISO | Pin 21 (GPIO9) | SPI0 MISO |
| CSN | Pin 29 (GPIO5) | plain GPIO |
| CE | Pin 31 (GPIO6) | plain GPIO |
| IRQ | not connected | unused |

CSN/CE can be any two free GPIOs (toggled in software, not hardware chip
select) — D5/D6 just keep them off the real SPI bus pins (GPIO7/8/9/10/11).

**Power:**  Put a 100 µF+ capacitor across the module's VCC/GND. If the radio is flaky ("radio not found" / garbled packets), feed it from a separate 3.3V regulator off the Pi's 5V instead of the Pi's own 3.3V pin.

## Infrared wiring (Optional)

A KY-022 receiver and a KY-005 transmitter, each needing exactly one GPIO —
set both in the app's **Settings → Infrared** tab (or leave either blank for
a receive-only or transmit-only install). Unlike the radio's CSN/CE pins,
changing these never needs the hub restarted.

| Module | Pin | Pi pin (header) | Default |
|---|---|---|---|
| KY-022 (receiver) | OUT | any free GPIO | GPIO17 (pin 11) |
| KY-022 (receiver) | VCC | Pin 1 (**3.3V — not 5V**) | |
| KY-005 (transmitter) | S | any free GPIO | GPIO18 (pin 12) |

The defaults sit clear of SPI0 (GPIO7/8/9/10/11, used by the radio above)
and of GPIO5/GPIO6 (this deployment's CSN/CE) — pick different GPIOs if
those default radio pins are ever changed, since the app has no way to know
what else is wired to a pin it is not using.

`harmony-deploy setup` installs and enables it automatically — nothing
below needs doing by hand on a device provisioned that way. (`harmony-deploy
push`, the ordinary HTTP update path for a device already set up, does not
touch provisioning at all -- there is nothing for it to do here.) This is
the manual equivalent, for a device set up by the manual steps in
[Installing on the Pi](#installing-on-the-pi) instead, or for fixing one
that predates this being automatic.

**Not `apt install pigpio`.** Raspberry Pi OS stopped packaging it once
Debian moved to Trixie/Bookworm-based releases — confirmed on a real Pi 3A+
on Debian 13 (trixie), where it fails outright:

```
$ sudo apt install -y pigpio python3-pigpio
Package pigpio is not available, but is referred to by another package.
Error: Package 'pigpio' has no installation candidate
```

The upstream project is unmaintained and never got repackaged for the
newer GPIO driver model, even though the C code and the DMA/register
access it actually needs are unchanged on a Pi 3-family board — Raspberry
Pi Ltd simply stopped shipping it. Building it from source instead:

```bash
sudo apt update
sudo apt install -y git build-essential
git clone --depth 1 https://github.com/joan2937/pigpio.git
cd pigpio
make -j4
sudo make install
```

`make install`'s very last step tries to install a bundled Python module
using `distutils`, which Python 3.12+ dropped from the standard library —
this fails on every current Raspberry Pi OS:

```
ModuleNotFoundError: No module named 'distutils'
make: *** [Makefile:107: install] Error 1
```

**This is harmless and expected.** Everything that actually matters —
`pigpiod` itself, its libraries — installs in the steps *before* that one,
confirmed by checking for the binary directly:

```bash
ls -la /usr/local/bin/pigpiod
```

The system-wide Python module that step was trying to install is not what
the hub uses anyway: its own venv gets `pigpio` from PyPI via `pip`, which
handles the same `distutils` removal correctly and never hits this.

`make install` also does not ship a systemd unit — that used to come from
the `.deb` package alone — so the last step is writing one by hand:

```bash
sudo tee /etc/systemd/system/pigpiod.service > /dev/null <<'EOF'
[Unit]
Description=pigpio daemon
After=network.target

[Service]
ExecStart=/usr/local/bin/pigpiod
ExecStop=/bin/systemctl kill pigpiod
Type=forking

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod
sudo systemctl status pigpiod --no-pager   # expect "active (running)"
```

Nothing here needs a reboot or a device-tree overlay, which is what lets the
RX/TX pins be changed from the app with the hub already running — and the
gateway retries the `pigpiod` connection on its own the next time you learn
or send something, so getting the daemon running is enough on its own; the
hub itself does not need restarting either.

## Code change required

[`src/harmony_receiver/radio.py`](src/harmony_receiver/radio.py) used to
unconditionally force `BLINKA_FT232H=1`, which would have made Blinka look
for a USB FT232H bridge even on the Pi and ignore its native SPI/GPIO. It now
only forces that on non-Linux hosts (Windows/macOS); on Linux, Blinka
auto-detects the Pi's own hardware. This fix is already committed — nothing
to do here, just don't reintroduce the unconditional env var.

Because the Pi uses different pin names than the FT232H (`D5`/`D6` instead of
`C0`/`D4`), the radio pins are set per-install in the app's **Settings**
tab (or via `--csn-pin`/`--ce-pin`), not hard-coded.

## Layout on the Pi

The hub lives under one root (`~/harmony` below) laid out so that a later
[remote update](#remote-update) can replace *code* without ever touching
anything this install already has running:

```
~/harmony/
├── bin/harmony-launch     # decides which release to boot; never replaced by an update
├── venv/                  # third-party dependencies only -- no project code
├── releases/<build-id>/   # one versioned copy of harmony_hub + harmony_receiver + the web UI
└── data/                  # the actual working directory: hub_settings.json, hub_config.json,
                            #   buttons.json, credentials/, codes/, captures/, update_state.json,
                            #   update_token
```

Only `data/` holds anything specific to *this* install. `releases/` and
`bin/` are code, and a fresh Pi with an empty `data/` is a blank hub waiting
to be configured, no matter which release is running.

## Installing on the Pi

**If the Pi already has SSH access enabled** (true of a stock Raspberry Pi
OS install), `harmony-deploy setup` does everything below automatically —
create the layout, upload the first release, install the systemd unit, and
fetch the update token — whether the device is completely bare or you are
re-running it against one already set up:

```bash
pip install -e ".[deploy]"    # once, on the dev machine -- adds paramiko
harmony-deploy setup pi --host 10.10.0.124 --user pi
```

It prompts for anything not passed as a flag, tries your SSH keys before
asking for a password, shows the exact plan before touching anything, and
only proceeds after you confirm (`--yes` to skip that, `--dry-run` to only
see the plan). `harmony-deploy inspect --host ... ` reports a device's
state read-only, with nothing to confirm. Run `setup` again later against
the same device to push an ordinary update the same way `harmony-deploy
push` would, or to repair one — reinstall the systemd unit, re-migrate
config — without needing the hub itself to be reachable.

**Still needs SPI enabled by hand first** (see step 1 below) — `setup`
does not reach into raspi-config, and a wrong or missing wiring is not
something any of this can detect for you.

The rest of this section is what `setup` does, spelled out — useful if SSH
is not available, something needs fixing by hand, or you just want to know
what it is about to do to a device before running it.

1. **Enable SPI:**
   ```bash
   sudo raspi-config nonint do_spi 0
   sudo reboot
   ```

2. **Create the layout and the venv, over SSH:**
   ```bash
   mkdir -p ~/harmony/{releases,incoming,data,bin}
   cd ~/harmony
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **If `lgpio` fails to build** (`swig: No such file or directory`) once
   dependencies are installed below:
   ```bash
   sudo apt update
   sudo apt install -y build-essential python3-dev swig
   ```

4. **If it then fails at the link step** (`/usr/bin/ld: cannot find -llgpio`)
   — the SWIG wrapper compiled, but the underlying C library isn't
   installed:
   ```bash
   sudo apt install -y liblgpio-dev
   ```
   If `liblgpio-dev` isn't available in your apt sources, skip building
   `lgpio` from source entirely and use Raspberry Pi OS's prebuilt packages
   instead:
   ```bash
   sudo apt install -y python3-lgpio python3-rpi.gpio python3-spidev
   rm -rf venv
   python3 -m venv --system-site-packages venv
   ```

5. **Install `pigpiod`, for the IR backend** (skip this if you have no
   IR receiver/transmitter wired -- the hub works fine without it, and
   `Settings → Infrared` just stays unconfigured). `harmony-deploy setup`
   does this step automatically; it is spelled out in full, including why
   `apt install pigpio` does not work on current Raspberry Pi OS, in
   [Infrared wiring](#infrared-wiring) above.

6. **Build the web UI on your dev machine, not the Pi** — a Pi 3A+ has only
   512MB RAM, too little for the Flutter web build tooling. On Windows,
   from the repo root:
   ```bash
   cd app && flutter build web --release && cd ..
   ```

7. **Put the first release on by hand.** This is the one time code goes on
   without going through `/api/update` — there is nothing running yet to
   verify a signature against. From the repo root, on the dev machine:

   **Git Bash, macOS, or Linux:**
   ```bash
   BUILD_ID=$(date -u +%Y%m%dT%H%M%S)-bootstrap
   rm -rf /tmp/release && mkdir -p /tmp/release/src
   cp -r src/harmony_hub src/harmony_receiver /tmp/release/src/
   cp -r app/build/web /tmp/release/src/harmony_hub/web
   find /tmp/release -name __pycache__ -type d -exec rm -rf {} +
   venv/Scripts/python -c "from harmony_hub.update.bundle import read_requirements; open('/tmp/release/requirements.txt','w').write(read_requirements('pyproject.toml'))"
   scp -r /tmp/release pi@<pi-host>:~/harmony/releases/$BUILD_ID
   scp src/harmony_hub/update/launcher.py pi@<pi-host>:~/harmony/bin/harmony-launch
   ```

   **PowerShell** (needs the *OpenSSH Client* optional Windows feature for
   `scp` — Settings → Optional Features, if `scp` is not already recognised):
   ```powershell
   $BuildId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmss") + "-bootstrap"
   $ReleaseDir = "$env:TEMP\harmony_release"
   if (Test-Path $ReleaseDir) { Remove-Item -Recurse -Force $ReleaseDir }
   New-Item -ItemType Directory -Force -Path "$ReleaseDir\src" | Out-Null
   Copy-Item -Recurse src\harmony_hub, src\harmony_receiver "$ReleaseDir\src\"
   Copy-Item -Recurse app\build\web "$ReleaseDir\src\harmony_hub\web"
   Get-ChildItem $ReleaseDir -Filter __pycache__ -Recurse -Directory | Remove-Item -Recurse -Force
   $env:RELEASE_DIR = $ReleaseDir
   venv\Scripts\python -c "import os; from harmony_hub.update.bundle import read_requirements; open(os.path.join(os.environ['RELEASE_DIR'], 'requirements.txt'), 'w').write(read_requirements('pyproject.toml'))"
   scp -r $ReleaseDir pi@<pi-host>:~/harmony/releases/$BuildId
   scp src\harmony_hub\update\launcher.py pi@<pi-host>:~/harmony/bin/harmony-launch
   ```

   Then, on the Pi (over SSH, so this part is the same either way). Set
   `BUILD_ID` to the directory name that just arrived — named explicitly
   rather than globbed or `ls`-ed, because an interrupted `scp` leaves a
   half-copied directory behind, and a glob that matches two of them
   silently produces either a bad `pip` invocation or invalid JSON that the
   launcher then falls back from with "no release recorded":
   ```bash
   cd ~/harmony
   ls releases                    # exactly one entry expected; delete any partial one first
   BUILD_ID=<the directory name from above>
   venv/bin/pip install -r "releases/$BUILD_ID/requirements.txt"
   printf '{"current": "%s"}\n' "$BUILD_ID" > data/update_state.json
   ```
   `data/update_state.json` is what the launcher actually reads to decide
   what to boot — copying a release into `releases/` does not activate it,
   by design, since that is the same separation an ordinary update relies
   on to stage a release before committing to it.

8. **Migrating an existing install?** Move its per-device files into `data/`
   rather than recreating them:
   ```bash
   mv ~/harmony-old/hub_settings.json ~/harmony-old/hub_config.json ~/harmony-old/buttons.json ~/harmony/data/
   mv ~/harmony-old/credentials ~/harmony-old/captures ~/harmony-old/codes ~/harmony/data/ 2>/dev/null
   ```
   Also remove the old install's `pip install -e .`, if the venv you are
   reusing ever had one. It leaves a `harmony-hub` command in `venv/bin/`
   that still points at the *old* checkout's source — which still works,
   still starts, and will quietly serve the old code from the old working
   directory (missing your just-migrated `data/`) if anyone runs it by habit
   instead of going through `bin/harmony-launch`:
   ```bash
   venv/bin/pip uninstall -y harmony-receiver
   ```
   After this, `harmony-hub` should no longer exist as a command at all —
   the only supported way to start the hub is `bin/harmony-launch`, below.

9. **First run, to set the radio pins:**
   ```bash
   cd ~/harmony
   venv/bin/python bin/harmony-launch ~/harmony
   ```
   Open `http://<pi-ip>:8765` → **Settings** → set the radio pins to `D5`
   (CSN) / `D6` (CE), or whatever GPIOs were actually wired. Save, then
   **Run checks** to confirm the radio opens. Pair the remote and learn
   buttons as usual from here. Stop it with Ctrl-C once it looks right —
   the next step is making it permanent.

## Running headless on boot

```ini
# /etc/systemd/system/harmony-hub.service
[Unit]
Description=Harmony Hub
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/harmony/data
ExecStart=/home/pi/harmony/venv/bin/python /home/pi/harmony/bin/harmony-launch /home/pi/harmony
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=10

[Install]
WantedBy=multi-user.target
```

`ExecStart` runs the launcher, not the hub directly — it decides which
release to boot and rolls back one that keeps failing to start (see
[Remote update](#remote-update)). `Restart=always` rather than
`on-failure`: a remote update asks the hub to exit cleanly (exit code 0) in
order to restart itself onto new code, and that has to bring it back too.
The higher `StartLimitBurst`/interval give the launcher's own two-attempt
rollback room to run before systemd's rate limiter would otherwise mark the
unit `failed` first.

Adjust the paths above to wherever `~/harmony` actually lives — a mismatch
fails with `status=203/EXEC` ("Unable to locate executable"), which is a
path problem, not a code problem.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now harmony-hub
journalctl -u harmony-hub -f
```

If it crash-loops on a *fresh* release (not one already rolled back), the
launcher will fall back to the previous one within two restarts; if you see
"Start request repeated too quickly" anyway, run
`sudo systemctl reset-failed harmony-hub` and check `journalctl` for what
actually failed.

Restarting the hub itself (radio reconnects, config reloads) still works
from the Settings tab over the API — systemd only needs to know about the
process's own start/stop, not the hub's internal restarts.

## Remote update

Once the Pi is up under the layout above, new code goes on with
`harmony-deploy` from the dev machine — no more manual `scp` and unit
restarts for an ordinary code change. See [`src/harmony_hub/update/`](src/harmony_hub/update)
for how it works; the short version:

1. **One-time pairing.** If the device was set up with `harmony-deploy
   setup` (see above), this already happened — it fetches the token and
   writes `deploy_targets.json` itself, and you can skip to step 2. By
   hand: the device generates its own update token on first use — grab it
   once over SSH:
   ```bash
   scp pi@<pi-host>:~/harmony/data/update_token ~/.harmony/pi.token
   ```
   Then create `deploy_targets.json` at the repo root (copy
   `deploy_targets.example.json`) pointing a name at this Pi:
   ```json
   { "pi": { "url": "http://<pi-host>:8765", "token_file": "~/.harmony/pi.token" } }
   ```
   `deploy_targets.json` is gitignored — it is per-machine, like the token
   file it points at.

2. **Every deploy after that:**
   ```bash
   harmony-deploy push pi
   ```
   This runs the test suite, builds the web UI, packs a code-only bundle
   (config, `credentials/`, and anything under `data/` never leave your
   machine — see `update/manifest.ALLOWED_PATTERNS`), signs it, and waits
   for the Pi to come back on the new build.

3. **If a deploy goes bad**, the Pi rolls itself back automatically after
   two failed boot attempts — no action needed. To go back sooner, or after
   something that started but misbehaves once actually used:
   ```bash
   harmony-deploy rollback pi
   ```

4. **What's running right now:**
   ```bash
   harmony-deploy version pi
   ```
   or open the Settings tab, which shows the same thing.

Config, `hub_config.json`, `buttons.json`, and `credentials/` are never part
of a bundle — only `harmony_hub`/`harmony_receiver` source and the built web
UI move. A push while a scene is active is refused unless you pass
`--force`, since the update briefly takes the hub down to restart it.

## Updating from a GitHub release

The Pi can also update itself, with nobody at a dev machine at all — every
push to a `v*` tag on GitHub runs the test suite and, if it passes, builds
and publishes a release with the same bundle `harmony-deploy push` would
have built (see [`.github/workflows/release.yml`](.github/workflows/release.yml)).
The hub checks for one on its own and offers to install it from the app.

This is the pull side of the same release system `harmony-deploy push`
uses — same bundle format, same `installer.install` doing the actual
staging/dependency-install/smoke-test/activate, same automatic rollback if
a release keeps failing to boot. The only thing that differs is how the
bytes arrive: over a signed HTTP push from a dev machine, or downloaded
directly from a GitHub release. See
[`src/harmony_hub/update/source.py`](src/harmony_hub/update/source.py) and
[`check.py`](src/harmony_hub/update/check.py) for how it works.

- **Settings that control it**, in `hub_settings.json` — all three editable
  from the Settings tab, no restart needed:
  - `github_updates_enabled` (default on) — whether the hub checks GitHub
    at all.
  - `github_repo` (default `windowslucker1121/HarmonyHubReplacement`) —
    which repo's releases to watch.
  - `update_check_interval_hours` (default 6, `0` disables automatic
    checking) — how often the hub checks on its own; "Check for updates"
    in Settings always works on demand regardless.
- **No signature required**, unlike a push. There is no second party to
  sign with — GitHub's own TLS stands in for it, the same way
  `/api/update/rollback` already needs no signature: at worst a LAN
  attacker can make the hub install a release the configured repo actually
  published, not arbitrary code. Turn `github_updates_enabled` off for a
  hub that should only ever take a signed push.
- **Installing** downloads and installs in the background and returns
  immediately — the download plus `pip install` can take several minutes
  on a Pi, and the hub stays fully reachable the whole time. Progress shows
  up as `update` events in the Live log and on the Software card, the same
  way a push's progress does; the hub only goes briefly unreachable at the
  very end, while it restarts onto the new release.
- **Publishing a release yourself** (maintainers): tag a commit that has
  already passed CI and push the tag —
  ```bash
  git tag v1.5.0
  git push origin v1.5.0
  ```
  CI runs the full test suite again on that exact commit, then runs
  `harmony-deploy build --out dist` and attaches `dist/*.tar.gz` and
  `dist/*.manifest.json` to a GitHub release named after the tag. A hub
  checking that repo picks it up on its next check.
