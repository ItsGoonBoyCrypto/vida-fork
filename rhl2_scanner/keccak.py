"""Dependency-free Keccak-256 (Ethereum's hash) + EVM helpers.

Ethereum uses *Keccak-256*, which differs from the finalized SHA3-256 in its
padding byte (0x01 vs 0x06). Python's ``hashlib.sha3_256`` is the finalized
variant and is therefore wrong for EVM use, so we implement Keccak-256 here to
avoid pulling in pysha3/pycryptodome.

Used for:
  * event topic hashes (log filtering for the new-pool listener), and
  * Solidity storage-slot computation (honeypot stateOverride simulation).
"""

from __future__ import annotations

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f(state: list[int]) -> None:
    for rnd in range(24):
        # theta
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]
        # rho + pi
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])
        # iota
        state[0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte Keccak-256 digest of ``data``."""
    rate = 136  # 1088 bits for Keccak-256
    state = [0] * 25

    # Padding: multi-rate 10*1 with Keccak domain byte 0x01.
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i] ^= lane
        _keccak_f(state)

    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            out += state[i].to_bytes(8, "little")
            if len(out) >= 32:
                break
        if len(out) < 32:
            _keccak_f(state)
    return bytes(out[:32])


def keccak_hex(data: bytes) -> str:
    return "0x" + keccak256(data).hex()


# --- EVM helpers ----------------------------------------------------------

def event_topic(signature: str) -> str:
    """Topic0 for an event signature, e.g. 'PairCreated(address,address,address,uint256)'."""
    return keccak_hex(signature.encode())


def _pad32(x: int | str) -> bytes:
    if isinstance(x, str):
        x = int(x, 16) if x.startswith("0x") else int(x)
    return x.to_bytes(32, "big")


def addr_to_topic(address: str) -> str:
    """Left-pad a 20-byte address to a 32-byte log topic."""
    a = address.lower().replace("0x", "")
    return "0x" + a.rjust(64, "0")


def mapping_slot(key_address: str, slot: int) -> str:
    """Storage key for ``mapping(address => _) at position `slot```.

    slot_key = keccak256(pad32(address) . pad32(slot))
    """
    key = int(key_address, 16)
    return "0x" + keccak256(_pad32(key) + _pad32(slot)).hex()


def nested_mapping_slot(key1_address: str, key2_address: str, slot: int) -> str:
    """Storage key for ``mapping(address => mapping(address => _)) at `slot```.

    Used for ERC-20 allowances: allowance[owner][spender].
    inner = keccak256(pad32(owner) . pad32(slot))
    final = keccak256(pad32(spender) . inner)
    """
    inner = keccak256(_pad32(int(key1_address, 16)) + _pad32(slot))
    final = keccak256(_pad32(int(key2_address, 16)) + inner)
    return "0x" + final.hex()
