"""Schema-less protobuf: parse, write, and surgically patch while preserving unknown fields.

GoodNotes' messages contain fields we have not decoded. Every mutation here is
field-level surgery so untouched bytes survive verbatim.
"""
import struct

WIRE_VARINT, WIRE_64, WIRE_LEN, WIRE_32 = 0, 1, 2, 5


# ---------- primitives ----------
def read_varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7F) << s; s += 7
        if not x & 0x80:
            return r, i


def write_varint(n):
    out = bytearray()
    while True:
        x = n & 0x7F; n >>= 7
        out.append(x | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(field, wire):
    return write_varint((field << 3) | wire)


def bytes_field(field, payload):
    return tag(field, WIRE_LEN) + write_varint(len(payload)) + payload


def varint_field(field, value):
    return tag(field, WIRE_VARINT) + write_varint(value)


def f32_field(field, value):
    return tag(field, WIRE_32) + struct.pack("<f", value)


# ---------- length-delimited streams ----------
def read_stream(b):
    """GoodNotes .pb members are streams of varint-length-delimited messages."""
    i = 0
    while i < len(b):
        n, i = read_varint(b, i)
        yield b[i:i + n]
        i += n


def write_stream(messages):
    return b"".join(write_varint(len(m)) + m for m in messages)


# ---------- field-level access ----------
def split(b):
    """-> [(field, wire, whole_bytes)] in original order, nothing lost."""
    out = []
    i = 0
    while i < len(b):
        start = i
        t, i = read_varint(b, i)
        field, wire = t >> 3, t & 7
        if wire == WIRE_VARINT:
            _, i = read_varint(b, i)
        elif wire == WIRE_64:
            i += 8
        elif wire == WIRE_LEN:
            n, i = read_varint(b, i)
            i += n
        elif wire == WIRE_32:
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((field, wire, b[start:i]))
    return out


def patch(b, replacements):
    """replacements: {field: new_whole_bytes or None to drop}. Order preserved."""
    out = []
    for field, wire, whole in split(b):
        if field in replacements:
            new = replacements[field]
            if new is not None:
                out.append(new)
        else:
            out.append(whole)
    return b"".join(out)


def upsert(b, field, whole):
    """Replace `field` if present, else insert it keeping ascending field order."""
    parts = split(b)
    if any(f == field for f, _, _ in parts):
        return patch(b, {field: whole})
    out, placed = [], False
    for f, _, chunk in parts:
        if not placed and f > field:
            out.append(whole); placed = True
        out.append(chunk)
    if not placed:
        out.append(whole)
    return b"".join(out)


def parse(b, depth=0, max_depth=6):
    """Best-effort decode: [(field, kind, value)] where kind is
    'varint' | 'fixed32' | 'fixed64' | 'len'. For 'len', value is (raw, sub_or_None)."""
    out = []
    i = 0
    while i < len(b):
        try:
            t, i = read_varint(b, i)
        except IndexError:
            break
        field, wire = t >> 3, t & 7
        if field == 0:
            break
        try:
            if wire == WIRE_VARINT:
                v, i = read_varint(b, i)
                out.append((field, "varint", v))
            elif wire == WIRE_64:
                out.append((field, "fixed64", struct.unpack("<q", b[i:i + 8])[0])); i += 8
            elif wire == WIRE_LEN:
                n, i = read_varint(b, i)
                raw = b[i:i + n]; i += n
                sub = None
                if depth < max_depth and n:
                    try:
                        sub = parse(raw, depth + 1, max_depth) or None
                    except Exception:
                        sub = None
                out.append((field, "len", (raw, sub)))
            elif wire == WIRE_32:
                out.append((field, "fixed32", struct.unpack("<f", b[i:i + 4])[0])); i += 4
            else:
                break
        except (IndexError, struct.error):
            break
    return out


def fields(b):
    """-> {field: value} using parse() semantics. Later duplicates win."""
    return {f: v for f, _, v in parse(b)}
