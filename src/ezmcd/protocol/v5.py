"""Account-independent v5 wire format, extracted from the verified prototype.

All runtime fields and pinned constants are explicit; no global account state.
"""
from dataclasses import dataclass
import hashlib
import re
import struct

MASK = 0xffffffff
SHIFTS = ([6, 13, 17, 21, 7, 12, 17, 20, 7, 12, 16, 22, 7, 13, 17, 22]
          + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4)
ROUND_ORDER = tuple(range(39)) + (40, 39, 42, 41) + tuple(range(43, 64))


def custom_digest(raw, constants):
    """0x101f6c698: modified MD5, including reordered third-round steps."""
    if not isinstance(raw, bytes) or len(raw) > 4096:
        raise ValueError('Expected bounded bytes')
    state = [0x10325476, 0x98badcfe, 0xefcdab89, 0x67452301]
    padded = raw + b'\x80' + b'\0' * ((55 - len(raw)) % 64) + struct.pack('<Q', len(raw) * 8)
    for offset in range(0, len(padded), 64):
        words = struct.unpack('<16I', padded[offset:offset + 64])
        value = state.copy()
        for index in ROUND_ORDER:
            a = -index % 4
            b, c, d = (a + 1) % 4, (a + 2) % 4, (a + 3) % 4
            if index < 16:
                f, g = (value[b] & value[c]) | (~value[b] & value[d]), index
            elif index < 32:
                f, g = (value[d] & value[b]) | (~value[d] & value[c]), (5 * index + 1) % 16
            elif index < 48:
                f, g = value[b] ^ value[c] ^ value[d], (3 * index + 5) % 16
            else:
                f, g = value[c] ^ (value[b] | ~value[d]), 7 * index % 16
            x = (value[a] + f + constants[index] + words[g]) & MASK
            shift = SHIFTS[index]
            value[a] = (((x << shift) | (x >> (32 - shift))) + value[b]) & MASK
        state = [(a + b) & MASK for a, b in zip(state, value)]
    return struct.pack('<4I', *state)


def footer(milliseconds, random_value, counter, constants):
    """0x101f6b094: interleave masked time and a checked 15-character suffix."""
    if (type(milliseconds) is not int or not 0 <= milliseconds < 2**63
            or type(random_value) is not int or not 1 <= random_value <= 4095
            or type(counter) is not int or not 1 <= counter <= 4095):
        raise ValueError('Invalid time, random value or per-path counter')
    derived = milliseconds + (random_value ^ ((milliseconds + counter) & 4095))
    digest_input = f'3{derived:016x}{counter:03x}{random_value:03x}'.encode('ascii')
    tail = custom_digest(digest_input, constants)[6:10].hex() + f'{random_value:03x}{counter:03x}3'
    masked = bytes(b ^ (random_value & 255) for b in milliseconds.to_bytes(8, 'big')).hex()
    return ''.join(masked[i] + (tail[i] if i < 15 else '') for i in range(16))


@dataclass(frozen=True, repr=False)
class ObservedRuntime:
    # Deliberately no defaults: never invent a "clean" environment or device ID.
    device_hex: str
    environment_hex: str


def encode(path, runtime, *, milliseconds, random_value, counter, constants):
    """Calculate bytes only. A captured runtime is historical, not revalidated."""
    from blake3 import blake3
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,255}', path):
        raise ValueError('Expected a path without URL, query or fragment')
    if (not isinstance(runtime, ObservedRuntime)
            or not re.fullmatch(r'[0-9A-F]{32}', runtime.device_hex)
            or not re.fullmatch(r'[0-9a-f]{16}', runtime.environment_hex)):
        raise ValueError('Explicit observed runtime fields required')
    tail = footer(milliseconds, random_value, counter, constants)
    path_hash = blake3((path + tail).encode('ascii')).hexdigest()[32:]
    device = ''.join(format((int(c, 16) + milliseconds + random_value) % 16, 'X')
                     for c in runtime.device_hex)
    core = path_hash + device
    digest = custom_digest((core + runtime.environment_hex).encode('ascii'), constants).hex()
    checksum = hashlib.sha256((core + digest + runtime.environment_hex + 'i').encode()).hexdigest()[:24]
    partial = 'i3' + core + tail + digest + runtime.environment_hex + checksum
    return partial + format(sum(partial.encode('ascii')) % 16, 'x')


def decode_verified_sample(path, signature, constants):
    """Return sensitive inputs only after reconstructing the entire saved sample."""
    if not isinstance(signature, str) or not re.fullmatch(r'i3[0-9a-fA-F]{168}', signature):
        raise ValueError('Unsupported signature format')
    tail = signature[66:97]
    masked, fields = tail[::2], tail[1::2]
    random_value, counter = int(fields[8:11], 16), int(fields[11:14], 16)
    milliseconds = int.from_bytes(bytes(b ^ (random_value & 255) for b in bytes.fromhex(masked)), 'big')
    device = ''.join(format((int(c, 16) - milliseconds - random_value) % 16, 'X')
                     for c in signature[34:66])
    runtime = ObservedRuntime(device, signature[129:145])
    inputs = dict(milliseconds=milliseconds, random_value=random_value, counter=counter)
    if encode(path, runtime, constants=constants, **inputs) != signature:
        raise ValueError('Sample failed full reconstruction')
    return runtime, inputs
