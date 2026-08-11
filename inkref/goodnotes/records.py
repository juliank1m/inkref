"""Descriptor/item record pairs.

Page content is a stream of alternating (descriptor, item) messages. They are linked
by a shared UUID and a shared {replica, clock} version stamp.

All mutation is field-level patching, so fields we have not decoded survive verbatim.
"""
from . import lz4
from . import protobuf as pb
from . import strokes as strokes_mod
from . import tplfmt
from . import ids

PEN_STROKE = 7          # top-level item field number == item type
IMAGE = 1
TEXT_BOX = 8
MATH_GROUP = 11

# descriptor fields
D_UUID, D_VERSION, D_ORDER = 1, 2, 9
D_DELETED = 3
# item body fields
I_UUID, I_GEOMETRY, I_COLOR, I_VERSION = 1, 2, 4, 15
I_DELETED = 14
I_SHAPE = 3

# Item body field 3 declares the stroke family, and MUST agree with the tpl signature
# inside the geometry blob. Absent = constant width, 1 = variable width. Established by
# controlled experiment: five otherwise-identical constant-width strokes differing only
# in this field and descriptor field 14 — every variant without field 3 rendered, every
# variant with it did not, regardless of field 14.
#
# Getting this wrong fails silently: the record imports, lands in `normal`, keeps
# byte-correct geometry, and simply never draws.
SHAPE_FOR_FAMILY = {
    strokes_mod.CONSTANT_WIDTH: None,       # field absent
    strokes_mod.CONSTANT_WIDTH_V1: None,
    strokes_mod.VARIABLE_WIDTH: 1,
}

# Deletion is marked by the mere presence of D_DELETED / I_DELETED (value 1). Verified
# across ~69k records in a real library: present on 100% of tombstoned items and 0% of
# live ones. A tombstone keeps its record but has its geometry arrays emptied, so it
# looks like a "degenerate" stroke — do not mistake one for a usable template.


def item_type(item_bytes):
    parts = pb.split(item_bytes)
    return parts[0][0] if parts else None


def encode_color(rgba):
    """GoodNotes omits zero-valued fixed32 fields; match that exactly."""
    return b"".join(pb.f32_field(i + 1, c) for i, c in enumerate(rgba) if c != 0.0)


def decode_color(color_msg):
    """Index by field number and default — never positionally."""
    got = {f: v for f, kind, v in pb.parse(color_msg) if kind == "fixed32"}
    return (got.get(1, 0.0), got.get(2, 0.0), got.get(3, 0.0), got.get(4, 1.0))


class StrokeRecord:
    """One (descriptor, item) pair holding a pen stroke."""

    __slots__ = ("_descriptor", "_item", "_body_cache", "_desc_fields", "_body_fields")

    def __init__(self, descriptor, item):
        self._descriptor = descriptor
        self._item = item
        self._invalidate()

    # Parsing a record is not cheap and callers hit the same fields repeatedly (a page
    # scan touches every record's deleted flag and paint order). Cache the parses and
    # drop them whenever the underlying bytes change.
    def _invalidate(self):
        self._body_cache = None
        self._desc_fields = None
        self._body_fields = None

    @property
    def descriptor(self):
        return self._descriptor

    @descriptor.setter
    def descriptor(self, value):
        self._descriptor = value
        self._desc_fields = None

    @property
    def item(self):
        return self._item

    @item.setter
    def item(self, value):
        self._item = value
        self._body_cache = None
        self._body_fields = None

    def _desc(self):
        if self._desc_fields is None:
            self._desc_fields = pb.fields(self._descriptor)
        return self._desc_fields

    # ---- body helpers ----
    def _body(self):
        if self._body_cache is None:
            for field, wire, whole in pb.split(self._item):
                if field == PEN_STROKE:
                    self._body_cache = pb.parse(whole)[0][2][0]
                    break
            else:
                raise ValueError("not a pen-stroke item")
        return self._body_cache

    def _bodyf(self):
        if self._body_fields is None:
            self._body_fields = pb.fields(self._body())
        return self._body_fields

    def _set_body(self, body):
        self._item = pb.patch(self._item, {PEN_STROKE: pb.bytes_field(PEN_STROKE, body)})
        self._body_cache = body
        self._body_fields = None

    def _body_field(self, num):
        v = self._bodyf().get(num)
        return v[0] if isinstance(v, tuple) else v

    # ---- identity ----
    @property
    def uuid(self):
        return self._body_field(I_UUID).decode()

    @property
    def order(self):
        return self._desc().get(D_ORDER)

    @property
    def version(self):
        return ids.read_version(self._desc()[D_VERSION][0])

    # ---- content ----
    @property
    def color(self):
        raw = self._body_field(I_COLOR)
        return decode_color(raw) if raw is not None else None

    @color.setter
    def color(self, rgba):
        self._set_body(pb.patch(self._body(),
                                {I_COLOR: pb.bytes_field(I_COLOR, encode_color(rgba))}))

    @property
    def geometry(self):
        """-> (tpl signature, members)"""
        return tplfmt.load(lz4.decompress(self._body_field(I_GEOMETRY)))

    @geometry.setter
    def geometry(self, sig_members):
        """Writes the blob AND keeps the family marker (field 3) in sync with it.

        These must never drift apart — see SHAPE_FOR_FAMILY.
        """
        sig, members = sig_members
        blob = lz4.compress(tplfmt.dump(sig, members))
        body = pb.patch(self._body(), {I_GEOMETRY: pb.bytes_field(I_GEOMETRY, blob)})
        if sig not in SHAPE_FOR_FAMILY:
            raise ValueError(
                f"family marker for {sig!r} is unknown; writing it would risk a "
                f"silently non-rendering stroke")
        shape = SHAPE_FOR_FAMILY[sig]
        if shape is None:
            body = pb.patch(body, {I_SHAPE: None})
        else:
            body = pb.upsert(body, I_SHAPE, pb.varint_field(I_SHAPE, shape))
        self._set_body(body)

    @property
    def family_marker(self):
        return self._bodyf().get(I_SHAPE)

    # ---- operations ----
    def translate(self, dx, dy):
        sig, members = self.geometry
        self.geometry = (sig, strokes_mod.translate(sig, members, dx, dy))

    @property
    def deleted(self):
        return D_DELETED in self._desc() or I_DELETED in self._bodyf()

    def undelete(self):
        """Drop the tombstone markers. Geometry is NOT restored — a tombstone's
        arrays were emptied when it was deleted."""
        self.descriptor = pb.patch(self.descriptor, {D_DELETED: None})
        self._set_body(pb.patch(self._body(), {I_DELETED: None}))

    def retag(self, uuid, version, order):
        """Give this record a fresh identity. Keeps every undecoded field.

        Also clears the deletion markers: a newly created record must never be born
        tombstoned, however it was cloned. Every creation path routes through here.
        """
        replica, clock = version
        vb = ids.version_bytes(replica, clock)
        self.descriptor = pb.patch(self.descriptor, {
            D_UUID: pb.bytes_field(D_UUID, uuid.encode()),
            D_VERSION: pb.bytes_field(D_VERSION, vb),
            D_ORDER: pb.varint_field(D_ORDER, order),
            D_DELETED: None,
        })
        self._set_body(pb.patch(self._body(), {
            I_UUID: pb.bytes_field(I_UUID, uuid.encode()),
            I_VERSION: pb.bytes_field(I_VERSION, vb),
            I_DELETED: None,
        }))

    def clone(self, uuid, version, order):
        copy = StrokeRecord(self.descriptor, self.item)
        copy.retag(uuid, version, order)
        return copy

    def is_consistent(self):
        """The two invariants that link a pair: matching UUID, and descriptor.f2
        byte-identical to the item body's f15."""
        desc = self._desc()
        body = self._bodyf()
        return (desc[D_UUID][0] == body[I_UUID][0]
                and desc[D_VERSION][0] == body[I_VERSION][0])

    def as_stroke(self):
        sig, members = self.geometry
        s = strokes_mod.from_tpl(sig, members)
        s.color = self.color or (0.0, 0.0, 0.0, 1.0)
        return s

    def __repr__(self):
        b = strokes_mod.bounds(*self.geometry)
        flag = " DELETED" if self.deleted else ""
        return (f"<StrokeRecord {self.uuid[:8]} order={self.order} "
                f"v={self.version} bounds={b}{flag}>")
