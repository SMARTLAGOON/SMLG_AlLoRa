# serial: a producer feeding an Edge whole files over a cable

Not a posture either, in the same sense [`pro/`](../pro) is not one. It is the [`open/`](../open)
Edge with the board's card and screen on and one more block in its config: its files come from a
producer on the other end of a UART instead of from its own code.

```
producer  --(UART1, 9600 baud, GPIO43 TX / GPIO44 RX)-->  T3S3  --(LoRa SF7, 868 MHz)-->  Hub
          whole files, sender-named                       Edge: protocol, outbox, radio
```

The producer is anything that can write a file and speak the wire below: a Raspberry Pi, a second
microcontroller, a laptop. It sends sensor readings, photographs, compressed archives, or anything
else it can name. What makes this the `serial` boundary rather than another one is that **the
producer decides where a file ends** and hands the board a finished thing.

Pair it with any Hub in this folder tree: the `open/hub` pair is the one it was tested against.

## The block

```json
"datasource": { "kind": "serial", "baudrate": 9600, "tx": 43, "rx": 44 }
```

Those three keys are the cable. Everything else has a default worth leaving alone until it is
not, and every key of the `disk` source works here too, because this **is** the disk outbox with
a producer filling it: `cleanup`, `archive_path` and `archive_budget` all mean what they mean
there, and `queue_path` at the top of the file is still the one place the outbox folder is named.

| key | default | what it does |
|---|---|---|
| `baudrate` | `9600` | a conservative default. Raise it on both ends together or not at all |
| `tx` / `rx` | `43` / `44` | the board's pins for the cable |
| `uart_id` | `1` | which hardware port. `0` is usually the console |
| `link_chunk_size` | `512` | how much the producer hands over between acknowledgements. **Not** the radio's chunk, which the node computes for itself. See below |
| `name_length` | `26` | the width of the name field the producer sends |
| `shorten_names` | `true` | see below |
| `stall_timeout` | `120` | seconds of silence mid-file before the arrival is abandoned |

There is deliberately no `chunk_size` key anywhere in this file. The node works out what a frame
can carry from the radio configuration and re-clamps it whenever that configuration changes, so a
number written here could only ever be the wrong one after the first retune.

## `link_chunk_size` is worth more than the baud rate

The cable is drained once per pass through the node's loop, and that loop also serves the radio.
So the producer sends one chunk, waits for `ACK`, and the board can only answer on its next turn.
Until the chunk is big enough to keep the wire busy across a whole radio round, the cable's
throughput is set by the radio's round period rather than by the baud rate, and raising the baud
rate alone moves nothing.

Measured on a running deployment, the same 503188-byte file over the same 9600-baud cable to the
same board, twenty minutes apart:

| `link_chunk_size` | elapsed | rate | resends |
|---|---|---|---|
| 512 | 1676.7 s | 300 B/s | 0 |
| 4096 | 630.4 s | 798 B/s | 0 |

The wire's own ceiling is 960 B/s, so 4096 gets most of the way there. What it costs is heap: the
port's receive buffer becomes `2 * link_chunk_size` and a partial chunk of that size is held in
RAM, so on a small board this is a trade rather than a free win.

**Both ends must change together.** The board takes a chunk's length to be
`min(link_chunk_size, remaining)`, so a producer and a board that disagree corrupt every transfer
rather than slowing one down.

## What the file ends up called

Say the producer sends a name like `2026-03-09_13-25-00.tar.xz`. By default the board shortens it to
`260309-132500.xz`: every digit of the timestamp, without the separators or the century, then
the real extension. Half the length, still sorts into chronological order, still reads as a date
at a glance, and no two captures more than a second apart can collide.

An earlier firmware cut the same name to `26030913.xz`, keeping only the hour, so two captures
from one hour landed on one name and one of them was lost. It had two reasons and neither
survives. The card is not an 8.3 filesystem: that same firmware created `outbox_files/` on it, a
twelve-character name no such filesystem can hold. And the name is not expensive on the air: v3
carries 255 bytes in a frame at every spreading factor, so the ten bytes this keeps are about
four percent of the single packet that ever carries them.

Set `"shorten_names": false` and every file keeps the exact name it was given.

Shortening reads the digits of a name as a date and checks that they are one, so a producer that
numbers its files some other way keeps its own name whatever this key says.

**A capture is never dropped for the sake of a name.** If a producer really does repeat a name
while the first file is still waiting to be sent, the new one is queued beside it as
`readings-1.json` rather than thrown away. The file already in the queue is never overwritten,
because it may be half way through a transfer whose remaining chunks would then come out of a
different file.

## The wire

What the producer has to speak. It is deliberately small enough to write in an afternoon on
whatever the producer happens to be:

```
producer                             board
START\n                       ->
                              <-     LISTEN\n
<8 byte size>\n
<26 byte name>\n
<10 byte crc32>\n             ->
<crc32 of chunk>\n <chunk>    ->
                              <-     ACK\n  (kept)  or  NACK\n  (send that chunk again)
...
END\n                         ->
                              <-     OK\n   (it is on the card, let go of your copy)
```

No `OK` means the board did not keep the file: the length or the checksum did not match what was
announced, so the producer still has the only copy and can offer it again.

## Flash and load

The same firmware and the same [`main.py`](../main.py) as every other node here. Copy
`edge/AlLoRa.json` onto the board, put a Hub config on the other board, and wire the producer to
GPIO43/44 with a common ground.
