# gps: a Raspberry Pi feeding an Edge over a cable

Not a posture either, in the same sense [`pro/`](../pro) is not one. It is the [`open/`](../open)
Edge with the board's card and screen on and one more block in its config: its files come from a
producer on the other end of a UART instead of from its own code.

```
Pi  --(UART1, 9600 baud, GPIO43 TX / GPIO44 RX)-->  T3S3  --(LoRa SF7, 868 MHz)-->  Hub
    whole .tar.xz captures                          Edge: protocol, outbox, radio
```

This is the deployment the GNSS rig has been running since 2024: a Pi logs NMEA, compresses an
hour of it, and pushes the archive down the cable. The board holds the protocol. There is no Edge
config here that a Pi could not equally well feed with sensor readings, photographs, or anything
else it can write to a file.

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
| `baudrate` | `9600` | what the rig runs. Raise it on both ends together or not at all |
| `tx` / `rx` | `43` / `44` | the board's pins for the cable |
| `uart_id` | `1` | which hardware port. `0` is usually the console |
| `link_chunk_size` | `512` | how much the producer hands over between acknowledgements. **Not** the radio's chunk, which the node computes for itself |
| `name_length` | `26` | the width of the name field the producer sends |
| `shorten_names` | `true` | see below |
| `stall_timeout` | `120` | seconds of silence mid-file before the arrival is abandoned |

There is deliberately no `chunk_size` key anywhere in this file. The node works out what a frame
can carry from the radio configuration and re-clamps it whenever that configuration changes, so a
number written here could only ever be the wrong one after the first retune.

## What the file ends up called

The Pi sends a name like `2026-03-09_13-25-00.tar.xz`. By default the board shortens it to
`260309-132500.xz`: every digit of the timestamp, without the separators or the century, then
the real extension. Half the length, still sorts into chronological order, still reads as a date
at a glance, and no two captures more than a second apart can collide.

The rig's old firmware cut the same name to `26030913.xz`, keeping only the hour, so two captures
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

Unchanged from what the rig's Pi already speaks, so no Pi has to be touched:

```
Pi                                   board
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
`edge/AlLoRa.json` onto the board, put a Hub config on the other board, and wire the Pi to
GPIO43/44 with a common ground.
