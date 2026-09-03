"""Drive a downlink delegation until it settles, instead of for a fixed number of seconds.

Every delegation test waits for the same thing: some number of whole deliveries, plus
however many reclaim windows the scenario forces. How long that takes is a property of the
machine the suite runs on, not of the protocol, so a fixed `listening_time` is a guess about
the host. The guess has now been wrong twice in the same file, the same way: one ticket
relaxed the delivery counts after they failed under full-suite load, and the next reported
the trailing `assert not hub.downlink_pending(endpoint)` failing for the same reason, five
times out of five passing in isolation. Relaxing assertions one at a time treats each
symptom and leaves the cause, which is that the test budgets wall-clock for work whose
duration it does not control.

So wait for the condition the test actually asserts. A loaded host takes another visit, an
idle one takes none, and neither is a failure.

**The Hub's visit length is deliberately left alone.** It would be tempting to poll in short
slices, and it would be wrong: `listen_to_endpoint` does per-visit bookkeeping, and one piece
of it counts *silent visits* toward tearing down a secure session
(`Hub._session_visit_end`, `session_recovery_after`). Chopping one visit into ten would make
an unrelated mechanism fire on drive shape rather than on the peer going quiet, which is the
very class of bug being fixed here. The first visit therefore keeps whatever length the test
already used, so on an unloaded machine the drive is exactly what it was before this helper
existed, and extra visits appear only where the test would otherwise have failed.

The Edge side has no such bookkeeping: `Edge.run` is a flat loop with no per-visit hooks, so
it is safe to slice, and slicing it is what lets the whole test stop as soon as the work is
done rather than sitting out a twelve-second serve window that finished in three.
"""
import threading
import time


def drive_until(hub, endpoint, edge, done, listening_time,
                edge_slice=1.0, ceiling=90.0, save_file=True):
    """Run the Edge's loop and the Hub's drive until `done()` holds.

    `done` is a zero-argument predicate, normally
    `lambda: not hub.downlink_pending(endpoint)`. `listening_time` is the per-visit budget
    the test used before, kept so an unloaded run behaves identically.

    Returns the Edge's thread, already stopped and joined, so a caller can keep asserting
    `not server.is_alive()` on it. Gives up after `ceiling` seconds rather than hanging: a
    genuine break must still fail the test, and it fails on the caller's own assertion.
    """
    deadline = time.monotonic() + ceiling
    stop = threading.Event()

    def edge_loop():
        # A slice boundary can never land mid-transfer: `run` checks its deadline only at the
        # top of a round, and a granted pull runs to completion inside one round. A slice may
        # therefore overrun, which is exactly the behaviour wanted here.
        while not stop.is_set() and time.monotonic() < deadline:
            edge.serve(timeout=edge_slice)

    server = threading.Thread(target=edge_loop, name="edge-serve", daemon=True)
    server.start()
    try:
        while True:
            hub.listen_to_endpoint(endpoint, listening_time=listening_time,
                                   save_file=save_file)
            if done() or time.monotonic() >= deadline:
                break
    finally:
        stop.set()
        # The join has to allow for a slice that was mid-pull when the flag went up, which is
        # a whole transfer rather than a whole slice.
        server.join(timeout=ceiling)
    return server
