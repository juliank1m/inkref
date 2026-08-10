"""Public API.

    doc = Document.open("in.goodnotes")
    page = doc.pages[0]
    page.strokes[0].translate(100, 0)
    doc.add_stroke(page.id, Stroke(points=[(100,100),(160,100)], width=3))
    doc.write("out.goodnotes")

Records are held as raw bytes and only ever field-patched, so undecoded fields survive.
"""
from . import archive
from . import ids
from . import protobuf as pb
from . import records
from .strokes import Stroke  # noqa: F401  (re-exported for callers)

UNITS_PER_POINT = 11 / 6      # GoodNotes stores 1/132 inch
POINTS_PER_UNIT = 6 / 11


def to_units(points):
    return points * UNITS_PER_POINT


def to_points(units):
    return units * POINTS_PER_UNIT


class Page:
    def __init__(self, page_id, path, messages):
        self.id = page_id
        self.path = path
        # one ordered list so original interleaving is preserved on write
        self.entries = []          # StrokeRecord, or a raw (descriptor, item) tuple
        # content is strictly alternating (descriptor, item)
        if len(messages) % 2:
            raise ValueError(f"page {self.id}: odd record count {len(messages)}")
        for i in range(0, len(messages), 2):
            desc, item = messages[i], messages[i + 1]
            if records.item_type(item) == records.PEN_STROKE:
                self.entries.append(records.StrokeRecord(desc, item))
            else:
                self.entries.append((desc, item))

    @property
    def records(self):
        """Every pen-stroke record, including tombstones."""
        return [e for e in self.entries if isinstance(e, records.StrokeRecord)]

    @property
    def live(self):
        """Records that are not tombstoned — what the user actually sees."""
        return [r for r in self.records if not r.deleted]

    strokes = live

    def append(self, record):
        self.entries.append(record)

    def serialize(self):
        msgs = []
        for e in self.entries:
            msgs += [e.descriptor, e.item] if isinstance(e, records.StrokeRecord) else list(e)
        return pb.write_stream(msgs)

    def __repr__(self):
        n = len(self.records)
        return f"<Page {self.id[:8]} strokes={n} other={len(self.entries) - n}>"


class Document:
    def __init__(self, arc):
        self.archive = arc
        self.pages = []
        for page_id, path in arc.note_paths():
            raw = arc.members.get(path, b"")
            msgs = list(pb.read_stream(raw)) if raw else []
            self.pages.append(Page(page_id, path, msgs))
        self.versions = ids.VersionAllocator.seeded_from(
            [r.version for p in self.pages for r in p.records])
        self._order_cursor = {}
        self._template_cache = {}

    @classmethod
    def open(cls, path):
        return cls(archive.Archive(path))

    @property
    def schema(self):
        return self.archive.schema

    def page(self, page_id):
        for p in self.pages:
            if p.id == page_id or p.id.startswith(page_id):
                return p
        raise KeyError(page_id)

    def _next_order(self, page):
        """O(1) after the first call. Rescanning per add made bulk writes O(n^2)."""
        cur = self._order_cursor.get(page.id)
        if cur is None:
            orders = [r.order for r in page.records if r.order is not None]
            cur = max(orders) if orders else 99997
        cur += 3
        self._order_cursor[page.id] = cur
        return cur

    def _template(self, page, index):
        """Cache the structural template so add_stroke does not rescan page.live."""
        key = (page.id, index)
        rec = self._template_cache.get(key)
        if rec is None:
            live = page.live
            if not live:
                raise ValueError("no live template record on this page")
            rec = live[index]
            self._template_cache[key] = rec
        return rec

    def add_record(self, page_id, record):
        """Append an already-built StrokeRecord with a fresh identity."""
        page = self.page(page_id)
        record.retag(ids.new_uuid(), self.versions.next(), self._next_order(page))
        page.append(record)
        return record

    def duplicate_stroke(self, page_id, index=0, dx=0.0, dy=0.0):
        """Clone a live record — new identity, same undecoded fields. Indexes into
        page.live, so tombstones are never picked by accident."""
        page = self.page(page_id)
        src = self._template(page, index)
        copy = src.clone(ids.new_uuid(), self.versions.next(), self._next_order(page))
        if dx or dy:
            copy.translate(dx, dy)
        page.append(copy)
        return copy

    def add_stroke(self, page_id, stroke, template_index=0):
        """Add a stroke built from our own geometry.

        Fields whose semantics we have not reproduced are taken from an existing
        record on the page, which acts purely as a structural template. The template
        is chosen from page.live: cloning a tombstone would produce a stroke that
        imports correctly but is already deleted, and therefore never renders.
        """
        page = self.page(page_id)
        rec = self._template(page, template_index).clone(
            ids.new_uuid(), self.versions.next(), self._next_order(page))
        rec.geometry = stroke.to_tpl()
        if stroke.color is not None:
            rec.color = stroke.color
        page.append(rec)
        return rec

    def write(self, out_path):
        for p in self.pages:
            if p.path in self.archive.members:
                self.archive.members[p.path] = p.serialize()
        return self.archive.write(out_path)

    def __repr__(self):
        return f"<Document schema={self.schema} pages={len(self.pages)}>"
