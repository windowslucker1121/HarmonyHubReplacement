"""Pure-function tests for IR capture normalisation and best-effort decoding.

No hardware and no daemon are involved anywhere here -- see the module
docstring in `harmony_hub.ir.normalise` for why that split exists.
"""

from __future__ import annotations

from harmony_hub.ir.normalise import agree, decode, normalise


def _nec_frame(address: int = 0x04, command: int = 0x08) -> list[int]:
    """A synthetic, jitter-free NEC frame for address/command."""
    address_inv = address ^ 0xFF
    command_inv = command ^ 0xFF
    timings = [9000, 4500]
    for byte in (address, address_inv, command, command_inv):
        for bit_index in range(8):
            bit = (byte >> bit_index) & 1
            timings.append(560)
            timings.append(1690 if bit else 560)
    return timings


def _sony_frame(address: int, command: int, address_bits: int) -> list[int]:
    total = 7 + address_bits
    bits = [(command >> i) & 1 for i in range(7)] + [(address >> i) & 1 for i in range(address_bits)]
    assert len(bits) == total
    timings = [2400, 600]
    for bit in bits:
        timings.append(1200 if bit else 600)
        timings.append(600)
    return timings


def _rc5_frame(address: int, command: int) -> list[int]:
    start2 = 1 if command < 0x40 else 0
    six_bit_command = command & 0x3F
    bits = [1, start2, 0]  # S1, S2, toggle (untoggled)
    bits += [(address >> i) & 1 for i in reversed(range(5))]
    bits += [(six_bit_command >> i) & 1 for i in reversed(range(6))]

    half = 889
    slots: list[int] = []
    for bit in bits:
        slots.extend([1, 0] if bit else [0, 1])

    # Collapse consecutive equal-level slots into mark/space durations.
    timings: list[int] = []
    level = slots[0]
    run = 1
    for value in slots[1:]:
        if value == level:
            run += 1
        else:
            timings.append(run * half)
            level = value
            run = 1
    timings.append(run * half)
    return timings


# ---------------------------------------------------------------------------
# normalise / agree
# ---------------------------------------------------------------------------


def test_normalise_collapses_jitter_within_one_capture():
    # Three occurrences of "the same" ~560us mark, jittered apart, plus two
    # occurrences of "the same" ~1690us space.
    result = normalise([560, 561, 559, 1690, 1688])
    assert result[0] == result[1] == result[2]
    assert result[3] == result[4]
    assert result[0] != result[3]


def test_normalise_keeps_genuinely_different_durations_apart():
    result = normalise([560, 1690, 560, 1690])
    assert set(result) == {560, 1690}


def test_normalise_of_empty_capture_is_empty():
    assert normalise([]) == []


def test_agree_accepts_small_jitter():
    assert agree([9000, 4500, 560, 1690], [9012, 4488, 561, 1688])


def test_agree_rejects_a_different_length_capture():
    assert not agree([9000, 4500], [9000, 4500, 560, 1690])


def test_agree_rejects_a_genuinely_different_value():
    assert not agree([9000, 4500, 560, 1690], [9000, 4500, 560, 560])


# ---------------------------------------------------------------------------
# decode -- NEC
# ---------------------------------------------------------------------------


def test_decodes_a_standard_nec_frame():
    assert decode(_nec_frame(0x04, 0x08)) == "NEC 0x04 0x08"


def test_decodes_an_extended_nec_frame_whose_address_is_not_self_complementing():
    # address_inv deliberately not ~address, which is what marks it "extended".
    timings = [9000, 4500]
    address, address_inv, command, command_inv = 0x01, 0x02, 0x08, 0x08 ^ 0xFF
    for byte in (address, address_inv, command, command_inv):
        for bit_index in range(8):
            bit = (byte >> bit_index) & 1
            timings.append(560)
            timings.append(1690 if bit else 560)
    assert decode(timings) == "NEC ext 0x0102 0x08"


def test_nec_with_a_bad_command_checksum_does_not_decode_as_nec():
    timings = _nec_frame(0x04, 0x08)
    # Corrupt the final space so command_inv's last bit flips.
    timings[-1] = 560 if timings[-1] == 1690 else 1690
    assert not decode(timings).startswith("NEC ")


def test_a_short_capture_is_never_mistaken_for_nec():
    assert decode([9000, 4500, 560]) == ""


# ---------------------------------------------------------------------------
# decode -- Sony SIRC
# ---------------------------------------------------------------------------


def test_decodes_a_12_bit_sony_frame():
    assert decode(_sony_frame(address=0x01, command=0x15, address_bits=5)) == "Sony12 0x01 0x15"


def test_decodes_a_15_bit_sony_frame():
    assert decode(_sony_frame(address=0x1A, command=0x15, address_bits=8)) == "Sony15 0x1A 0x15"


# ---------------------------------------------------------------------------
# decode -- RC5
# ---------------------------------------------------------------------------


def test_decodes_an_rc5_frame():
    assert decode(_rc5_frame(address=0x05, command=0x0D)) == "RC5 0x05 0x0D"


def test_decodes_an_rc5_frame_needing_the_extended_command_bit():
    # command >= 0x40 forces start-bit 2 low, and the decoder must fold that
    # back into the command's 7th bit to recover the original value.
    assert decode(_rc5_frame(address=0x05, command=0x45)) == "RC5 0x05 0x45"


# ---------------------------------------------------------------------------
# decode -- fallback
# ---------------------------------------------------------------------------


def test_an_unrecognised_capture_decodes_to_an_empty_label():
    assert decode([100, 200, 300, 400, 500, 600, 700, 800]) == ""


def test_decode_never_raises_on_a_trivially_short_capture():
    assert decode([]) == ""
    assert decode([100]) == ""
