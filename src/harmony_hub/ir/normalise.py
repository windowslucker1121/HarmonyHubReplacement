"""Pure signal-processing for IR captures: no hardware, no daemon.

A capture from `IrGateway.capture` is a list of microsecond durations,
alternating mark (carrier on) and space (carrier off), starting on a mark --
that is exactly what a falling edge on an active-low receiver produces, and
exactly what `wave_add_generic` wants back on the way out, so nothing here
ever has to convert between shapes.

`decode` is deliberately best-effort and purely cosmetic: it never changes
what gets stored or sent, only what label the learn screen can show next to
a raw capture (`"NEC 0x04 0x08"`). A protocol this cannot recognise is not an
error -- `send()` always replays the raw timings themselves, so nothing here
being wrong, or blank, ever stops a learned command from working.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

#: Below this many mark/space pairs, a capture is noise rather than a button
#: press. A TSOP demodulator free-runs under fluorescent light and direct
#: sun, and without a floor that noise gets "learned" as a real command. The
#: shortest real remote protocol here (Sony SIRC, 12 bits) clears this by a
#: wide margin.
MIN_PULSES = 8

DEFAULT_TOLERANCE = 0.2


def normalise(timings: Sequence[int], tolerance: float = DEFAULT_TOLERANCE) -> List[int]:
    """Buckets a single capture's durations to remove jitter within it.

    Two mentions of the "same" duration within one capture never carry
    identical microsecond values -- clock jitter and callback latency mean a
    32-bit NEC frame's thirty-two roughly-560us bit marks each land a few
    microseconds apart. Bucketing by `tolerance` (values within `tolerance`
    of each other collapse to their mean, rounded) turns that into a clean,
    small set of duration values while leaving genuinely different timings --
    a 560us "0" gap versus a 1690us "1" gap -- untouched.

    This does not reconcile two *separate* captures with each other -- two
    presses of the same button still land on their own independent jitter,
    so comparing them is `agree()`'s job, not this one's.
    """
    if not timings:
        return []
    buckets: List[List[int]] = []
    for value in sorted(timings):
        for bucket in buckets:
            if abs(value - bucket[0]) <= tolerance * bucket[0]:
                bucket.append(value)
                break
        else:
            buckets.append([value])
    means = {v: round(sum(bucket) / len(bucket)) for bucket in buckets for v in bucket}
    return [means[v] for v in timings]


def agree(a: Sequence[int], b: Sequence[int], tolerance: float = DEFAULT_TOLERANCE) -> bool:
    """Whether two captures are close enough to be the same button press.

    Used to require a second press before a learn is accepted -- see
    `learn_start` in `backends/ir.py` -- because a partial or corrupted
    single capture is the most common way IR learning goes wrong, and a
    second press that disagrees is the cheapest way to catch it before it is
    saved as a command that will not play back correctly.
    """
    if len(a) != len(b):
        return False
    return all(abs(x - y) <= tolerance * max(x, y, 1) for x, y in zip(a, b))


def _near(value: int, target: int, tolerance: float) -> bool:
    return abs(value - target) <= tolerance * target


# --------------------------------------------------------------------------
# NEC
# --------------------------------------------------------------------------

_NEC_TOL = 0.25
_NEC_LEADER_MARK = 9000
_NEC_LEADER_SPACE = 4500
_NEC_BIT_MARK = 560
_NEC_ZERO_SPACE = 560
_NEC_ONE_SPACE = 1690


def _decode_nec(timings: Sequence[int]) -> str:
    if len(timings) < 4:
        return ""
    if not (
        _near(timings[0], _NEC_LEADER_MARK, _NEC_TOL)
        and _near(timings[1], _NEC_LEADER_SPACE, _NEC_TOL)
    ):
        return ""

    bits: List[int] = []
    i = 2
    while i + 1 < len(timings) and len(bits) < 32:
        mark, space = timings[i], timings[i + 1]
        if not _near(mark, _NEC_BIT_MARK, _NEC_TOL):
            break
        if _near(space, _NEC_ONE_SPACE, _NEC_TOL):
            bits.append(1)
        elif _near(space, _NEC_ZERO_SPACE, _NEC_TOL):
            bits.append(0)
        else:
            break
        i += 2
    if len(bits) != 32:
        return ""

    def byte_at(offset: int) -> int:
        # NEC sends every byte least-significant-bit first.
        value = 0
        for j, bit in enumerate(bits[offset : offset + 8]):
            value |= bit << j
        return value

    address, address_inv, command, command_inv = (byte_at(k) for k in (0, 8, 16, 24))
    if (command ^ command_inv) != 0xFF:
        return ""

    extended = (address ^ address_inv) != 0xFF
    addr_label = f"0x{address:02X}{address_inv:02X}" if extended else f"0x{address:02X}"
    return f"NEC{' ext' if extended else ''} {addr_label} 0x{command:02X}"


# --------------------------------------------------------------------------
# Sony SIRC
# --------------------------------------------------------------------------

_SONY_TOL = 0.3
_SONY_START_MARK = 2400
_SONY_UNIT_SPACE = 600
_SONY_ONE_MARK = 1200
_SONY_ZERO_MARK = 600
#: Longest first: a genuine 12-bit capture cannot satisfy the longer forms
#: (there is nothing left to read), so trying long-to-short never misreads
#: a short frame as a truncated long one.
_SONY_BIT_COUNTS = (20, 15, 12)


def _decode_sony(timings: Sequence[int]) -> str:
    if len(timings) < 3:
        return ""
    if not (
        _near(timings[0], _SONY_START_MARK, _SONY_TOL)
        and _near(timings[1], _SONY_UNIT_SPACE, _SONY_TOL)
    ):
        return ""

    bits: List[int] = []
    i = 2
    while i < len(timings):
        mark = timings[i]
        if _near(mark, _SONY_ONE_MARK, _SONY_TOL):
            bits.append(1)
        elif _near(mark, _SONY_ZERO_MARK, _SONY_TOL):
            bits.append(0)
        else:
            break
        i += 1
        if i >= len(timings) or not _near(timings[i], _SONY_UNIT_SPACE, _SONY_TOL):
            break
        i += 1

    for total in _SONY_BIT_COUNTS:
        if len(bits) != total:
            continue
        command = 0
        for j, bit in enumerate(bits[:7]):
            command |= bit << j
        address = 0
        for j, bit in enumerate(bits[7:total]):
            address |= bit << j
        return f"Sony{total} 0x{address:02X} 0x{command:02X}"
    return ""


# --------------------------------------------------------------------------
# RC5 (Manchester-coded)
# --------------------------------------------------------------------------

_RC5_TOL = 0.3
_RC5_HALF_BIT = 889
_RC5_BITS = 14


def _rc5_slots(timings: Sequence[int]) -> Optional[List[int]]:
    """Expands mark/space durations into half-bit-time on/off slots.

    A capture always starts on a mark -- the receiver idles high and the
    first edge is the carrier switching on -- so the level of slot 0 is
    fixed; every slot after that alternates. A duration that is not roughly
    one or two half-bit periods cannot be RC5 at all.
    """
    slots: List[int] = []
    level = 1
    for duration in timings:
        units = round(duration / _RC5_HALF_BIT)
        if units not in (1, 2) or not _near(duration, units * _RC5_HALF_BIT, _RC5_TOL):
            return None
        slots.extend([level] * units)
        level ^= 1
    return slots


def _decode_rc5(timings: Sequence[int]) -> str:
    slots = _rc5_slots(timings)
    if slots is None or len(slots) < _RC5_BITS * 2:
        return ""
    slots = slots[: _RC5_BITS * 2]

    bits: List[int] = []
    for i in range(0, len(slots), 2):
        first, second = slots[i], slots[i + 1]
        # Manchester: a bit spans one full bit-time. The first physical
        # transmission is always logic 1, and a capture always starts on a
        # mark, so "mark then space" is the only consistent reading for 1.
        if first == 1 and second == 0:
            bits.append(1)
        elif first == 0 and second == 1:
            bits.append(0)
        else:
            return ""

    if bits[0] != 1:
        return ""  # S1 is always 1 -- anything else is not RC5

    start2 = bits[1]
    address = 0
    for bit in bits[3:8]:
        address = (address << 1) | bit
    command = 0
    for bit in bits[8:14]:
        command = (command << 1) | bit
    # The second start bit doubles as an inverted 7th command bit, kept for
    # backward compatibility with the original 6-bit RC5 command field.
    command |= (start2 ^ 1) << 6

    return f"RC5 0x{address:02X} 0x{command:02X}"


def decode(timings: Sequence[int]) -> str:
    """A short protocol label for a capture, or "" if none matched.

    Tried in order of how unambiguous a match is: NEC's 9ms leader is
    essentially impossible to confuse with anything else, Sony's repeating
    short/long marks are next most distinctive, and RC5's Manchester coding
    is tried last because a short or noisy capture can coincidentally satisfy
    its bit-time checks.
    """
    for decoder in (_decode_nec, _decode_sony, _decode_rc5):
        label = decoder(timings)
        if label:
            return label
    return ""
