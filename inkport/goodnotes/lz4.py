"""Apple-framed LZ4, via the same libcompression GoodNotes itself uses.

Frame: bv41 <u32 raw_size> <u32 comp_size> <lz4 block>   compressed chunk
       bv4- <u32 size> <bytes>                            stored chunk
       bv4$                                               terminator
"""
import ctypes
import struct

_lib = ctypes.CDLL("/usr/lib/libcompression.dylib")
_lib.compression_decode_buffer.restype = ctypes.c_size_t
_lib.compression_decode_buffer.argtypes = [
    ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
    ctypes.c_void_p, ctypes.c_int]
_lib.compression_encode_buffer.restype = ctypes.c_size_t
_lib.compression_encode_buffer.argtypes = _lib.compression_decode_buffer.argtypes

COMPRESSION_LZ4 = 0x100
MAGIC = (b"bv41", b"bv4-")
TERMINATOR = b"bv4$"


def is_framed(b):
    return b[:4] in MAGIC


def decompress(buf):
    """Frame -> raw bytes. Uses the frame header for an exact size hint when present."""
    hint = struct.unpack("<I", buf[4:8])[0] if is_framed(buf) and len(buf) >= 12 else None
    for cap in ([hint, hint * 4, hint * 32] if hint else []) + [max(len(buf) * 8, 4096), 1 << 22]:
        dst = ctypes.create_string_buffer(cap)
        n = _lib.compression_decode_buffer(dst, cap, buf, len(buf), None, COMPRESSION_LZ4)
        if n and n < cap:
            return dst.raw[:n]
    raise ValueError("could not decompress Apple LZ4 frame")


def compress(buf):
    cap = len(buf) + 4096
    dst = ctypes.create_string_buffer(cap)
    n = _lib.compression_encode_buffer(dst, cap, buf, len(buf), None, COMPRESSION_LZ4)
    if not n:
        raise RuntimeError("compression_encode_buffer failed")
    return dst.raw[:n]
