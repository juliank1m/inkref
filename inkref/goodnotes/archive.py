"""The .goodnotes ZIP container.

Members are rewritten in their original order with original timestamps and compression
settings, so anything we do not touch stays byte-identical.
"""
import io
import zipfile

from . import protobuf as pb

SCHEMA = "schema.pb"
INDEX_NOTES = "index.notes.pb"
INDEX_ATTACHMENTS = "index.attachments.pb"
INDEX_EVENTS = "index.events.pb"


def read_index(raw):
    """Index members map uuid -> zip-relative path."""
    out = []
    for msg in pb.read_stream(raw):
        f = pb.fields(msg)
        if 1 in f and 2 in f:
            out.append((f[1][0].decode(), f[2][0].decode()))
    return out


class Archive:
    """Loads every member into memory; small files, and it keeps rewriting simple."""

    def __init__(self, path):
        self.path = path
        with zipfile.ZipFile(path) as z:
            self.infos = list(z.infolist())
            self.members = {i.filename: z.read(i.filename) for i in self.infos}

    @property
    def names(self):
        return [i.filename for i in self.infos]

    @property
    def schema(self):
        raw = self.members.get(SCHEMA, b"")
        return pb.fields(raw).get(1) if raw else None

    def note_paths(self):
        return read_index(self.members[INDEX_NOTES])

    def attachment_paths(self):
        """uuid -> zip path. Page backgrounds are one-page PDFs stored here."""
        raw = self.members.get(INDEX_ATTACHMENTS)
        return dict(read_index(raw)) if raw else {}

    def attachment(self, uuid):
        path = self.attachment_paths().get(uuid)
        return self.members.get(path) if path else None

    def write(self, out_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for info in self.infos:
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                z.writestr(zi, self.members[info.filename])
        with open(out_path, "wb") as fh:
            fh.write(buf.getvalue())
        return out_path
