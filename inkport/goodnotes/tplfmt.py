"""Reader/writer for troydhanson/tpl images, as vendored into GoodNotes.

Layout:  "tpl\\0" <u32 total length> <NUL-terminated ASCII signature> <members in order>

The signature is authoritative — never hardcode member offsets.
Note that GoodNotes routinely stores float32 bit patterns in `u` slots.
"""
import struct

MAGIC = b"tpl\x00"

# tpl type char -> (struct format, size)
SCALARS = {
    "c": ("<b", 1), "j": ("<h", 2), "v": ("<H", 2), "i": ("<i", 4),
    "u": ("<I", 4), "I": ("<q", 8), "U": ("<Q", 8), "f": ("<d", 8),
}


def parse_signature(sig):
    """-> nodes: [('scalar', ch) | ('struct', nodes) | ('array', nodes)]"""
    def inner(i, stop=None):
        out = []
        while i < len(sig):
            c = sig[i]
            if c == stop:
                return out, i + 1
            if c in "AS" and i + 1 < len(sig) and sig[i + 1] == "(":
                sub, i = inner(i + 2, ")")
                out.append(("array" if c == "A" else "struct", sub))
            elif c in SCALARS:
                out.append(("scalar", c)); i += 1
            else:
                raise ValueError(f"unhandled tpl type char {c!r} in {sig!r}")
        return out, i
    nodes, _ = inner(0)
    return nodes


class _Reader:
    def __init__(self, b, o=0):
        self.b, self.o = b, o

    def take(self, n):
        v = self.b[self.o:self.o + n]
        if len(v) != n:
            raise EOFError("tpl image truncated")
        self.o += n
        return v

    def scalar(self, ch):
        fmt, size = SCALARS[ch]
        return struct.unpack(fmt, self.take(size))[0]

    def count(self):
        return struct.unpack("<I", self.take(4))[0]


def _read(r, nodes):
    out = []
    for kind, arg in nodes:
        if kind == "scalar":
            out.append(r.scalar(arg))
        elif kind == "struct":
            out.append(_read(r, arg))
        else:
            n = r.count()
            # A(x) of a single scalar or single struct yields flat elements
            if len(arg) == 1 and arg[0][0] == "scalar":
                out.append([r.scalar(arg[0][1]) for _ in range(n)])
            elif len(arg) == 1 and arg[0][0] == "struct":
                out.append([_read(r, arg[0][1]) for _ in range(n)])
            else:
                out.append([_read(r, arg) for _ in range(n)])
    return out


def _write(nodes, values, out):
    for (kind, arg), v in zip(nodes, values):
        if kind == "scalar":
            out.append(struct.pack(SCALARS[arg][0], v))
        elif kind == "struct":
            _write(arg, v, out)
        else:
            out.append(struct.pack("<I", len(v)))
            if len(arg) == 1 and arg[0][0] == "scalar":
                fmt = SCALARS[arg[0][1]][0]
                for e in v:
                    out.append(struct.pack(fmt, e))
            elif len(arg) == 1 and arg[0][0] == "struct":
                for e in v:
                    _write(arg[0][1], e, out)
            else:
                for e in v:
                    _write(arg, e, out)


def load(buf):
    """-> (signature, members)"""
    if buf[:4] != MAGIC:
        raise ValueError(f"not a tpl image: {buf[:8]!r}")
    end = buf.index(b"\x00", 8)
    sig = buf[8:end].decode("ascii")
    return sig, _read(_Reader(buf, end + 1), parse_signature(sig))


def dump(sig, members):
    body = []
    _write(parse_signature(sig), members, body)
    body = b"".join(body)
    total = 4 + 4 + len(sig) + 1 + len(body)
    return MAGIC + struct.pack("<I", total) + sig.encode("ascii") + b"\x00" + body


# GoodNotes packs float32 bit patterns into uint32 slots.
def f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]
