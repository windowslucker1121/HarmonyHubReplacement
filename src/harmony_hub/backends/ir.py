"""Infrared devices: learns commands from a remote and sends them back out.

Everything hardware-specific is delegated: `harmony_hub.ir.gateway` owns the
one shared receiver/transmitter this install is wired to, and
`harmony_hub.ir.learn` owns the one shared learn job. This class is thin
plumbing between those and the `Backend`/`Learnable` interfaces --
`config_schema()` covers per-*equipment* protocol settings (carrier
frequency, duty cycle) rather than wiring, because there is exactly one
receiver and one transmitter for the whole install; see the docstring on
`HubSettings.ir_rx_pin` for why those live there instead.

Reading and writing its own `ir.codes.CodeSet` is the only state that
belongs to an instance of this class -- the codeset genuinely is per-device,
one learned button being unrelated to another device's.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..ir import codes as ir_codes
from ..ir import gateway as ir_gateway
from ..ir.gateway import IrBusy, IrHardwareError
from ..ir.learn import DEFAULT_TIMEOUT as DEFAULT_LEARN_TIMEOUT
from ..ir.learn import LearnStatus
from ..ir.learn import job as learn_job
from . import Backend, BackendError, Command, Health, Learnable, register

logger = logging.getLogger("HUB.ir")

DEFAULT_CARRIER_HZ = 38_000
DEFAULT_DUTY_CYCLE = 0.33
DEFAULT_GAP_MS = 40.0
DEFAULT_REPEATS = 1


@register
class IrBackend(Backend, Learnable):
    """A device controlled entirely by commands learned from its own remote."""

    name = "ir"
    label = "Infrared device"
    description = "Learns commands from the original remote and sends them back out over IR."

    learn_hint = (
        "Point the original remote at the receiver and press the button you want to learn. "
        "You'll be asked to press it a second time, to confirm the capture."
    )

    def __init__(self, device_id: str, config: Dict[str, Any]) -> None:
        super().__init__(device_id, config)
        self._codes = ir_codes.CodeSet()

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "carrier_hz": {
                    "type": "integer",
                    "title": "Carrier frequency (Hz)",
                    "default": DEFAULT_CARRIER_HZ,
                    "description": "38000 suits most NEC and Sony remotes; 36000 suits Philips/RC5 "
                    "ones. Cannot be learned -- the receiver's own demodulator strips the carrier "
                    "before a capture ever sees it -- so it has to be told, not captured.",
                },
                "duty_cycle": {
                    "type": "number",
                    "title": "Duty cycle",
                    "default": DEFAULT_DUTY_CYCLE,
                    "description": "Fraction of each carrier cycle the LED is actually on.",
                },
                "gap_ms": {
                    "type": "number",
                    "title": "Minimum gap between sends (ms)",
                    "default": DEFAULT_GAP_MS,
                },
                "repeats": {
                    "type": "integer",
                    "title": "Frames per press",
                    "default": DEFAULT_REPEATS,
                    "description": "How many times to repeat a command's waveform each time it "
                    "is sent. Overridable per command when it is learned.",
                },
                "codes_dir": {
                    "type": "string",
                    "title": "Codes folder",
                    "default": ir_codes.DEFAULT_DIR,
                    "description": "Where this device's learned commands are stored.",
                },
            },
        }

    # -- configuration read-through --------------------------------------

    @property
    def _codes_path(self):
        return ir_codes.path_for(self.device_id, self.config.get("codes_dir") or ir_codes.DEFAULT_DIR)

    @property
    def _carrier_hz(self) -> int:
        return int(self.config.get("carrier_hz") or DEFAULT_CARRIER_HZ)

    @property
    def _duty_cycle(self) -> float:
        return float(self.config.get("duty_cycle") or DEFAULT_DUTY_CYCLE)

    @property
    def _gap_ms(self) -> float:
        return float(self.config.get("gap_ms") or DEFAULT_GAP_MS)

    @property
    def _default_repeats(self) -> int:
        return int(self.config.get("repeats") or DEFAULT_REPEATS)

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Loads the codeset from disk. Never raises.

        A device with no gateway reachable -- `pigpiod` not running, no pins
        wired yet -- still has to list its commands and stay editable, the
        same way `DenonBackend` stays editable with the receiver powered
        off. Only reading its own small JSON file is needed for that, and a
        file that cannot be read is treated as "nothing learned yet" rather
        than a startup failure.
        """
        try:
            self._codes = ir_codes.CodeSet.load(self._codes_path)
        except Exception as err:
            logger.error("Device '%s' could not read its codes from %s: %s", self.device_id, self._codes_path, err)
            self._codes = ir_codes.CodeSet()

    async def commands(self) -> List[Command]:
        return [
            Command(name=c.name, label=c.label, description=c.decoded, repeatable=c.repeatable)
            for c in self._codes
        ]

    async def send(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        entry = self._codes.get(command)
        if entry is None:
            names = ", ".join(c.name for c in self._codes) or "none learned yet"
            raise BackendError(f"device '{self.device_id}' has no IR command '{command}' (offers: {names})")
        try:
            await ir_gateway.gateway().transmit(
                entry.timings,
                self._carrier_hz,
                duty_cycle=self._duty_cycle,
                repeats=entry.repeats or self._default_repeats,
                gap_ms=self._gap_ms,
            )
        except IrHardwareError as err:
            raise BackendError(f"{self.device_id}.{command}: {err}") from err

    async def health(self) -> Health:
        ok, detail = ir_gateway.gateway().health()
        count = len(self._codes)
        learned = f"{count} code(s) learned" if count else "no codes learned yet"
        return Health(ok=ok, detail=f"{detail} -- {learned}" if detail else learned)

    def focus_for(self, command: str):
        # An IR remote never has anything for the SmartHome +/- keys to
        # step -- see `denon.DenonBackend.focus_for`'s identical choice, and
        # `test_the_receiver_never_steals_the_smarthome_keys` there for why
        # this is deliberate rather than an oversight.
        return None

    # -- learning -------------------------------------------------------------

    async def learn_start(self, timeout: float = DEFAULT_LEARN_TIMEOUT) -> LearnStatus:
        gateway = ir_gateway.gateway()
        if not gateway.rx_configured:
            # A pin that was never set is a permanent, instant "no" -- worth
            # a fast-path that skips the job entirely. A pin that *is* set
            # but whose pigpiod connection is currently down is not checked
            # here: `gateway.capture()` retries that connection itself (see
            # `ir.gateway`'s docstring on why `configure()` alone is not
            # enough), so the job is always attempted and reports the real,
            # specific reason -- "pigpiod not reachable at ..." -- rather
            # than this method guessing at one in advance and getting it
            # wrong the moment the daemon comes back up without a restart.
            return LearnStatus(
                state="failed", detail="no IR receive pin is configured -- set one in Settings"
            )
        try:
            return learn_job().start(self.device_id, gateway, timeout)
        except IrBusy as err:
            raise BackendError(str(err)) from err

    def learn_status(self) -> LearnStatus:
        return learn_job().status(self.device_id)

    def learn_cancel(self) -> LearnStatus:
        return learn_job().cancel(self.device_id)

    async def learn_verify(self) -> None:
        job = learn_job()
        if job.owner != self.device_id or job.result is None:
            raise BackendError("nothing has been captured yet")
        try:
            await ir_gateway.gateway().transmit(
                job.result,
                self._carrier_hz,
                duty_cycle=self._duty_cycle,
                repeats=self._default_repeats,
                gap_ms=self._gap_ms,
            )
        except IrHardwareError as err:
            raise BackendError(str(err)) from err

    async def learn_save(self, name: str, label: str, *, repeatable: bool = False, repeats: int = 1) -> None:
        job = learn_job()
        if job.owner != self.device_id or job.result is None:
            raise BackendError("nothing has been captured yet")
        self._codes.add(
            name,
            label,
            job.result,
            repeats=repeats or self._default_repeats,
            repeatable=repeatable,
            decoded=job.decoded,
        )
        self._codes.save(self._codes_path)
        job.finish(self.device_id)

    async def learn_forget(self, name: str) -> None:
        self._codes.forget(name)
        self._codes.save(self._codes_path)

    # -- suggestions ----------------------------------------------------------

    def suggested_bindings(self) -> Dict[str, str]:
        """Every learned command whose name is itself a remote button key.

        Works with no lookup table because the learn screen offers exactly
        those names to pick from -- see `ir_learn_screen.dart` -- so a
        command learned as `"volume_up"` maps itself onto the remote's own
        Volume Up the moment it is saved, with nothing else to configure.
        Unlike `DenonBackend.SUGGESTED_BINDINGS`, this cannot be a fixed
        table: which names exist is whatever this device has actually
        learned, decided at teach time, not at code-authoring time.
        """
        return {c.name: c.name for c in self._codes}
