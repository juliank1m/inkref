"""UUID and CRDT version generation.

Every item carries a {replica_id, lamport_clock} stamp in two places that must agree:
the descriptor's field 2 and the item body's field 15. Descriptors also carry an
ascending integer that controls paint order.
"""
import uuid as _uuid

from . import protobuf as pb


def new_uuid():
    """GoodNotes writes uppercase canonical UUIDs."""
    return str(_uuid.uuid4()).upper()


def version_bytes(replica, clock):
    """The {1: replica, 2: clock} message shared by descriptor.f2 and item.f15."""
    return pb.varint_field(1, replica) + pb.varint_field(2, clock)


def read_version(msg):
    """-> (replica, clock) from a version message."""
    f = pb.fields(msg)
    return f.get(1, 0), f.get(2, 0)


class VersionAllocator:
    """Hands out monotonically increasing clocks for one replica.

    Seeded from the highest clock already present so new items sort after existing
    ones and cannot collide with them.
    """

    def __init__(self, replica=2, clock=0):
        self.replica = replica
        self.clock = clock

    @classmethod
    def seeded_from(cls, versions, replica=None):
        """versions: iterable of (replica, clock) already in the document."""
        versions = list(versions) or [(2, 0)]
        rep = replica if replica is not None else versions[-1][0]
        return cls(rep, max(c for _, c in versions))

    def next(self):
        self.clock += 1
        return self.replica, self.clock
