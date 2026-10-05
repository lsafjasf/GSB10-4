"""In-memory broker + producer/consumer used to demonstrate and test
order-preserving partition-count migration.

Two migration strategies are implemented:

1. "dual_read" (default, reorder-by-sequence):
   - The producer stamps every message with a per-key monotonically
     increasing sequence number (1, 2, 3, ...).
   - begin_migration() switches the write path to the new partition
     count immediately; the old partitions are kept read-only.
   - The consumer reads BOTH generations (dual read) and reorders per
     key by sequence number, emitting seq 1,2,3,... per key. A key's
     messages can never be delivered out of order, even though they are
     split across two topologies.
   - Rationale: no write downtime; per-key sequence numbers make the
     merge deterministic, so ordering does not depend on read timing.

2. "reject":
   - begin_migration() makes writes fail with MigrationInProgress.
   - The consumer drains the old partitions completely, then
     end_migration() switches to the new topology and writes resume.
   - Rationale: simplest to reason about (only one topology is ever
     active), at the cost of a write outage during the drain.
"""

from collections import defaultdict

from .router import Router


class MigrationInProgress(Exception):
    pass


class Producer:
    """Assigns a per-key monotonic sequence number to every message.

    The sequence number is only committed after the broker accepts the
    write, so a rejected write never leaves a gap in the sequence (a gap
    would stall the consumer's reorder buffer forever).
    """

    def __init__(self, broker):
        self.broker = broker
        self._seq = defaultdict(int)

    def send(self, key, payload):
        seq = self._seq[key] + 1
        self.broker.write(key, seq, payload)
        self._seq[key] = seq
        return seq


class Broker:
    def __init__(self, num_partitions, mode="dual_read"):
        if mode not in ("dual_read", "reject"):
            raise ValueError("mode must be 'dual_read' or 'reject'")
        self.mode = mode
        self.router = Router(num_partitions)
        self.partitions = [[] for _ in range(num_partitions)]
        self._gen = 0
        self.migrating = False
        self._old = None  # (generation_number, partition_lists)

    def write(self, key, seq, payload):
        if self.migrating and self.mode == "reject":
            raise MigrationInProgress(
                "partition resize in progress; writes are rejected"
            )
        self.partitions[self.router.partition_for(key)].append(
            (key, seq, payload)
        )

    def begin_migration(self, new_num_partitions):
        if self.migrating:
            raise RuntimeError("migration already in progress")
        self.migrating = True
        if self.mode == "dual_read":
            # Freeze current partitions as the old generation, switch the
            # write path to the new topology immediately.
            self._old = (self._gen, self.partitions)
            self._gen += 1
            self.router = Router(new_num_partitions)
            self.partitions = [[] for _ in range(new_num_partitions)]
        # In "reject" mode the topology switch happens in end_migration(),
        # after the old partitions have been fully drained.
        self._pending_num_partitions = new_num_partitions

    def old_drained(self, consumer):
        if self.mode == "dual_read":
            if self._old is None:
                return True
            old_gen, old_parts = self._old
            return all(
                consumer._offset((old_gen, i)) >= len(p)
                for i, p in enumerate(old_parts)
            )
        return all(
            consumer._offset((self._gen, i)) >= len(p)
            for i, p in enumerate(self.partitions)
        )

    def end_migration(self, consumer):
        if not self.migrating:
            raise RuntimeError("no migration in progress")
        if not self.old_drained(consumer):
            raise RuntimeError("old partitions not fully drained yet")
        if self.mode == "reject":
            self._gen += 1
            self.router = Router(self._pending_num_partitions)
            self.partitions = [
                [] for _ in range(self._pending_num_partitions)
            ]
        self._old = None
        self.migrating = False


class Consumer:
    """Dual-read consumer that reorders per key by sequence number.

    Reads round-robin across all active partition generations (old +
    new during a migration) and only delivers, per key, the next
    expected sequence number. Messages that arrive early are held in a
    per-key reorder buffer.
    """

    def __init__(self, broker):
        self.broker = broker
        self._offsets = defaultdict(int)
        self._next = defaultdict(lambda: 1)
        self._buf = defaultdict(dict)
        self.delivered = defaultdict(list)

    def _offset(self, partition_id):
        return self._offsets[partition_id]

    def _active_partitions(self):
        gens = []
        if self.broker._old is not None:
            gen, parts = self.broker._old
            gens.extend((gen, i, p) for i, p in enumerate(parts))
        gens.extend(
            (self.broker._gen, i, p)
            for i, p in enumerate(self.broker.partitions)
        )
        return gens

    def poll(self, max_messages=1):
        """Read up to max_messages from the active partitions."""
        read = 0
        for gen, idx, part in self._active_partitions():
            if read >= max_messages:
                break
            off = self._offsets[(gen, idx)]
            while off < len(part) and read < max_messages:
                key, seq, payload = part[off]
                off += 1
                read += 1
                self._buf[key][seq] = payload
                self._flush(key)
            self._offsets[(gen, idx)] = off
        return read

    def drain(self):
        """Read everything currently available in all generations."""
        total = 0
        for gen, idx, part in self._active_partitions():
            off = self._offsets[(gen, idx)]
            while off < len(part):
                key, seq, payload = part[off]
                off += 1
                total += 1
                self._buf[key][seq] = payload
                self._flush(key)
            self._offsets[(gen, idx)] = off
        return total

    def _flush(self, key):
        nxt = self._next[key]
        buf = self._buf[key]
        while nxt in buf:
            self.delivered[key].append((nxt, buf.pop(nxt)))
            nxt += 1
        self._next[key] = nxt

    def delivered_sequence(self, key):
        return [seq for seq, _ in self.delivered[key]]
