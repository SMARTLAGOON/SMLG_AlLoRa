# **AlLoRa:** Advanced layer LoRa

Cite this repository: [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.7741245.svg)](https://doi.org/10.5281/zenodo.7741245)

<p align="center">
    <img src="readme_assets/logo.png"  width="50%">
</p>

AlLoRa turns a raw LoRa radio, which can only move a couple of hundred bytes at a time and drops
frames whenever the air is bad, into a link that transfers **whole files** reliably over long
distances. It is a Python / MicroPython library, so the same code runs on an ESP32-class board, a
Raspberry Pi, or a laptop. It is based on the original [LoRaCTP](https://github.com/pmanzoni/loractp),
adding a modular design, mesh capabilities, larger packets and faster transfers.

Details of the protocol can be found in these articles:

* [AlLoRa: Empowering environmental intelligence through an advanced LoRa-based IoT solution](https://www.sciencedirect.com/science/article/pii/S0140366424000641?via%3Dihub)

* [A modular and mesh-capable LoRa based Content Transfer Protocol for Environmental Sensing](https://ieeexplore.ieee.org/document/10060496)

* [AI*LoRa: Enabling Efficient Long-Range Communication with Machine Learning at the Edge](https://dl.acm.org/doi/10.1145/3641512.3690040)

We're also developing a custom GPT, [AlLoRa Genius](https://chat.openai.com/g/g-rOGxxA1BZ-allora-genius),
to assist in understanding and utilizing the AlLoRa protocol.

-----

# The model in one page

AlLoRa v3 separates three things that v2 fused into its node names: **where a node sits**, **what it
does in a given transfer**, and **who is in command**.

```
 Application │ DataSource ─▶ │             │ ─▶ DataSink
             │ (feeds files) │             │   (store / forward / website)
 ────────────┼───────────────┼─────────────┼───────────────────────────────
 Protocol    │     Edge ─────┤             ├──── Hub
             │ (source role) │             │  (collector role)
 ────────────┼───────────────┼─────────────┼───────────────────────────────
 Transport   │  Connector ───┤ ⇄ Packets ⇄ ├─── Connector
             │ (radio · tunnel · loopback) │   (… · Adapter bridge)
```

**There are exactly two node types, and they are named by placement.**

- **`Edge`** is the deployed, on-site node: it lives with the sensors and it *serves* what it has.
- **`Hub`** is the aggregation node and the permanent authority: it polls its Edges and *pulls*
  their files. One Edge or fifty, it is the same class.

**The role is a separate, swappable axis.** The **collector role** drives each round (it sets the
pace: polls, asks for metadata and chunks, reassembles) and the **source role** serves (it holds the
data and answers). By default the Hub is the collector and the Edge is the source, which is the
uplink. For a downlink the Hub *delegates* the collector role to one Edge, that Edge pulls the file,
and the Hub reclaims. Only the drive moves: the Hub stays in command throughout, and it is always
the authenticated peer and key holder.

That is why the type names say nothing about data direction. `Source` and `Collector` named a node by
what it did with data, and data direction is exactly what role reversal flips, so the old names
misdescribed the node half the time it mattered.

**Both ends carry both application boundaries.** A `DataSource` feeds files *in* to whoever is
serving; a `DataSink` drains completed files *out* of whoever is collecting. The diagram shows the
uplink; on a downlink they mirror.

## Coming from v2

| v2 / early-v3 term | v3 term | Kind of thing |
|---|---|---|
| `Source` node | **`Edge`** node | type (placement) |
| `Collector` node | **`Hub`** node | type (placement + authority) |
| `Requester` | a `Hub` with one Edge and a trivial sink | removed in v3 |
| `Gateway` | a `Hub` with many Edges and a cloud sink | removed in v3 |
| `source` / `collector` | the **role** names (the words that used to be type names) | role |
| `initiator` / `responder` | unchanged, but demoted to the per-round mechanic, not a role name | round / wire |
| `Adapter` node | transport: the far half of a split Connector | **not** a node type |
| `Interface` | absorbed into `Adapter` | deleted, not renamed |
| `CTP_File` | **`AlLoRa_File`** | core piece |
| `Gateway.check_digital_endpoints()` | **`Hub.run()`** | main-loop verb |
| `DataSource` / `DataSink` | unchanged, now present on **both** types | app boundary |

**`Source`, `Requester` and `Gateway` no longer exist.** They survived the migration as aliases and
were removed once nothing in the library, the examples or the firmware imported them, because a name
that only redirects is a name someone still has to learn. A year-old `main.py` needs two edits:
`Source` becomes `Edge`, and both `Requester` and `Gateway` become `Hub`. Pass the constructor
arguments **by keyword**, since the old positional orders are not the new one, and drop
`NEXT_ACTION_TIME_SLEEP`: it has been a silent no-op since v2.0 and the v3 constructors refuse it
rather than accept a knob they cannot honour (the adaptive gap lives on `Pacing`). To run old code
unchanged instead, use the `v2.0.0` tag, which is what it was written against.

Two verbs are worth keeping straight, because they are **not** a second pair of role names: the
collector role **drives** a round, the source role **serves** it. A node has a drive loop and a serve
loop; a role says which one it is running.

-----

# Learn by doing

Check the [examples folder](examples) for the full set. The shortest path:

- [Setting up AlLoRa in LilyGo T3S3 devices](firmware/T3S3/)
- [An Edge on a T3S3](examples/Edges/T3S3) and the [Hub that polls it](examples/Hubs/One-Edge/T3S3)
- [Setting up AlLoRa in LoPy4 devices](examples/Hubs/One-Edge/LoPy4)
- [A Hub on a Raspberry Pi or a computer, polling many Edges](examples/Hubs/Many-Edges)
- [Adapters](examples/Adapters), for a device with no radio of its own

A minimal Edge, serving a file to whoever asks:

```python
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

edge = Edge(SX127x_connector(), config_file="AlLoRa.json")
edge.set_file(AlLoRa_File(name="hello.bin", content=payload,
                          chunk_size=edge.get_chunk_size()))
edge.run()
```

And the Hub that pulls it:

```python
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint

hub = Hub(SX127x_connector(), config_file="AlLoRa.json")
hub.set_digital_endpoints([Digital_Endpoint(name="edge-1", mac_address="9eeff0dc",
                                            active=True, session_id=42)])
hub.run(save_files=True)
```

Both node types run with the same verb. `run()` without a `timeout` is the deployed main loop: it
never returns. A Hub with several endpoints registered in `Nodes.json` visits each in turn, most
overdue first, for that endpoint's own listening window.

-----

# The pieces

## Nodes

<details>
<summary>Two types on one shared base, plus the endpoint handle a Hub keeps per Edge.</summary>

### [Node.py](AlLoRa/Nodes/Node.py)

The shared base: identity and config, one exchange round, **both** loops (drive and serve), the
handshake, and the RF-config trial machinery. It is not meant to be instantiated. MicroPython has no
abstract classes, so a parent class does the job.

### [Edge.py](AlLoRa/Nodes/Edge.py)

The far-placement node. Home role `source`. `run()` answers its Hub's polls and uplink pulls, and
honors a delegation by driving one downlink pull before coming home. It holds one
`Digital_Endpoint`, for its Hub, so a downlink has somewhere to land.

An Edge never self-promotes and yields the instant it hears its Hub polling again.

### [Hub.py](AlLoRa/Nodes/Hub.py)

The center-placement node and the permanent authority. Home role `collector`. It holds a
`Digital_Endpoint` per registered Edge and `run()` is the visit loop over them. It also carries the
serve loop, because delegating a downlink means serving the file the delegated Edge then pulls.

One endpoint is the 1:1 case, many is the gateway deployment. Same class, which is why the old split
between a `Requester` and a `Gateway` dissolved: the difference was only how many endpoints and which
sink.

### [Digital_Endpoint.py](AlLoRa/Digital_Endpoint.py)

A Hub's **session handle for one remote Edge**: its identity and RF settings, the state machine of
the transfer, the mesh-retransmission state, the file currently in flight, and the secure session.
It also assembles the complete `AlLoRa_File` once every chunk has arrived. An Edge holds one too, for
its Hub.

The Hub retunes to an endpoint's RF settings before listening to it, which is how one Hub serves
several Edges on different configs. An endpoint that states no RF is polled on **the node's own
`AlLoRa.json` config**, so the common case, where both ends share a configuration, needs nothing
written down. To place one Edge on different settings, give its `Nodes.json` entry a `connector`
block copied from that Edge's own config; anything the block omits still comes from this node.
See [examples/Hubs/Many-Edges](examples/Hubs/Many-Edges).

</details>

## Transport: Connector, Link, Adapter

Three names sit at this layer and they are not synonyms:

- **Connector** = my access to the channel
- **Link** = the byte pipe joining two halves of a split Connector
- **Adapter** = the far half, the one on the bridge

> A node reaches the LoRa channel through a **Connector**. Usually that Connector owns a radio on the
> same board. When the radio sits on a *different* board (a Raspberry Pi has no LoRa), the Connector
> is **split in two halves**: the node holds the near half (`WiFi_connector`, `Serial_connector`) and
> the bridge board runs the far half inside an **Adapter**, which drives the real radio Connector.
> The halves exchange transport verbs (transmit, listen, exchange, get and set RF) over a **Link**: a
> dumb byte pipe with no knowledge of packets, sessions, or keys. The bridge holds no protocol logic,
> no files, and no keys; it just serves the channel on the node's orders. Adding a new medium means
> writing one new Link, never new protocol logic.

<details>
<summary>The Connectors, and what a Link actually has to implement.</summary>

### [Connectors/](AlLoRa/Connectors)

One contract (`transmit`, `listen`, `exchange`, plus RF config and `mac` / `rssi`), swappable, so
AlLoRa reaches as many kinds of device as possible.

| Connector | For |
|---|---|
| [`SX127x_connector`](AlLoRa/Connectors/SX127x_connector.py) | ESP32 boards with an SX127x, and a Raspberry Pi with a Dragino LoRa HAT |
| [`SX1262_connector`](AlLoRa/Connectors/SX1262_connector.py) | Boards with an SX1262 (LilyGo T3S3) |
| [`LoPy4_connector`](AlLoRa/Connectors/LoPy4_connector.py) | Pycom LoPy4, using its native LoRa library |
| [`E5_connector`](AlLoRa/Connectors/E5_connector.py) | A Seeed E5 module driven over AT commands |
| [`Serial_connector`](AlLoRa/Connectors/Serial_connector.py) | The near half of a tunnel over UART or USB |
| [`WiFi_connector`](AlLoRa/Connectors/WiFi_connector.py) | The near half of a tunnel over the network |
| [`Loopback_connector`](AlLoRa/Connectors/Loopback_connector.py) | Two nodes in one process, for tests |

The name is deliberate: the *channel* is the physical medium, usually the LoRa channel, while the
Connector is the medium-agnostic **access** to it.

### [Links/](AlLoRa/Links)

A Link is defined by its **contract**, not by a list of media: `rpc(request)` on the client half,
`read_request()` and `write_reply(reply)` on the bridge half, plus `close()`. It moves opaque
request and reply frames and knows nothing about verbs, the air wire, or keys.

There are three link **shapes**, and each covers a family of media rather than a single one:

| shape | class | media |
|---|---|---|
| byte stream, sentinel-framed | [`Serial_link`](AlLoRa/Links/Serial_link.py) | UART, **USB-CDC**, likely BLE over a stream profile |
| request and reply, natively framed | [`WiFi_link`](AlLoRa/Links/WiFi_link.py) | HTTP over TCP |
| in-process | [`Loopback_link`](AlLoRa/Links/Loopback_link.py) | CPython tests |

`Serial_link` hardcodes no UART. It takes any `port` object exposing write, read and
bytes-available, duck-typing pyserial's `in_waiting` or `machine.UART`'s `any()`, so a direct USB
connection already works with no new Link. A new medium needs a new Link only when its transport does
not fit one of the three shapes.

### [Adapters/](AlLoRa/Adapters)

The deployment name for the bridge device: the far half of the split Connector plus the radio
Connector it drives. `Serial_adapter` and `WiFi_adapter` mirror the connector names on the node side,
so the medium is in the class name at both ends and the radio stays an argument:

```python
# node side                             # bridge side
connector = WiFi_connector()            adapter = WiFi_adapter(SX1262_connector())
connector = Serial_connector()          adapter = Serial_adapter(SX127x_connector())
```

An Adapter is a **runner**: what a bridge board's `main.py` instantiates to boot a link and serve the
channel forever. It holds no protocol logic, no pacing, no keys, no files and no session, which is
exactly why it is transport and not a third node type.

</details>

## Application boundaries: DataSource and DataSink

<details>
<summary>Where a deployment plugs in: files in one side, files out the other.</summary>

### [DataSources/](AlLoRa/DataSources/DataSource.py)

A `DataSource` feeds `AlLoRa_File`s to whoever is in the source role: a sensor reading becomes a
file, an MQTT message becomes a file. Hand one to a node and its serve loop pumps it and sends
whatever it queues. The base class lives in the `DataSources` package, the input-boundary mirror of
`DataSinks`; the old top-level `AlLoRa.DataSource` import path was removed with the node aliases.

[`MQTT_DataSource`](AlLoRa/DataSources/MQTT_DataSource.py) subscribes to a broker and turns each
received message into a file. Paired with an [`MQTT_DataSink`](AlLoRa/DataSinks/MQTT_DataSink.py) on
the other side of the link, the original topic travels inside the file name (the payload crosses
untouched, byte for byte) and the message is republished on that same topic at the far broker.
Together they are a bidirectional, topic-preserving MQTT bridge over AlLoRa, with role reversal
carrying the downlink direction and a shared `Loop_guard` preventing a republished message from being
bridged back again.

[`Disk_DataSource`](AlLoRa/DataSources/Disk_DataSource.py) is the outbound queue that survives losing
power, and the mirror of `Disk_DataSink`: one is the folder files land in, the other is the folder
they wait in. The base queue lives in RAM, so a node that lost power between taking a reading and
getting it on the air lost the reading with nothing to retry. Here the file is on flash before it is
queued and is deleted only once the far end confirms it, so a node that reboots mid-transfer repeats
itself rather than going quiet.

It keeps two truths and reconciles them instead of trusting either. The **directory** says what is
pending: a file in it is queued, its absence is delivery. A small **`queue.json`** beside it says in
what order they go, holding names rather than contents. A file the index has never heard of is
adopted onto the end, so a producer can still drop one into the folder by hand; a name with no file
behind it is forgotten. So the worst an index can do is send a file in the wrong order, never lose
one. The split is needed because a directory does not remember the order things were put into it:
`listdir` returns entries however the filesystem is holding them, which is not a promise on either
filesystem an ESP32 might be flashed with, and file timestamps are no help when board clocks come up
unset. Writes are ordered payload first, index second, so an interruption costs order and never data.

The node's serve loop borrows the head of a queue rather than taking it, and drops it only on the
peer's acknowledgement. That is the same rule a Hub already followed when serving a delegated
downlink, so both directions now retire a queued file on the same evidence. The trade is
at-least-once: if the acknowledgement is what goes missing, the file is sent twice.

### [DataSinks/](AlLoRa/DataSinks/DataSink.py)

A `DataSink` takes ownership of each completed file: `consume(file, reception)`, where `Reception` is
a frozen snapshot of who sent it and how it arrived (RSSI, SNR, chunk count, session and device ids).
[`Disk_DataSink`](AlLoRa/DataSinks/Disk_DataSink.py) is the default and saves under a folder named
after the sender. A management website is a `DataSink`. So is a cloud uploader, or a database writer.

Before this seam existed, the collector hardwired a disk save, so "what to do with the data" leaked
into the node and every deployment re-invented it.

### [Control/](AlLoRa/Control)

The one application boundary that pushes **inward**, back down the stack, instead of outward to a
card or a broker. It is two objects on purpose:

- [`Control_Root_DataSink`](AlLoRa/DataSinks/Control_Root_DataSink.py) is the **verify gate**, and a
  genuine `DataSink`: it is what an Edge registers to receive downlink control artifacts. It parses
  the envelope off the reassembled file, checks the signature against the control root and the
  addressing against this device, and passes on only what is authentic and meant for this node. All
  crypto, no device knowledge, so it is the same class on every node.
- [`Control_Actuator`](AlLoRa/Control/Control_Actuator.py) is what the gate hands a **verified**
  artifact to: `apply(control_type, payload)`, where the effect happens. Device-specific, and it owns
  the one thing the gate cannot know, which is *when it is safe to act*. Acting immediately would
  break the acknowledgement the node is still sending, so `Node_Control_Actuator` queues a deferred
  action and the run loop drains it once the final OK is on the air.

A deployment adds an effect by subclassing the actuator and touching no protocol code. The control
types are a closed enum riding inside the signed region: `RF_CONFIG`, `RESET`, `MODEL`, `OTA`.

</details>

## Core pieces

<details>
<summary>File, Packet, Codec, Pacing.</summary>

### [File.py](AlLoRa/File.py)

`AlLoRa_File` is the unit being transferred. It can be instantiated **with content**, to be chunked
and served, or **as an empty container**, to receive chunks and be assembled once they have all
arrived. Writes are positioned and idempotent, so a re-sent chunk costs nothing and arrival order
does not matter.

### [Packet.py](AlLoRa/Packet.py) and [Packet_v3.py](AlLoRa/Packet_v3.py)

The typed wire unit: what a frame *means* (kind, session, flags, payload), as opposed to how it is
encoded. `Packet` is the v2 format, kept untouched so a v3 node can still fall back to a legacy peer.
`Packet_v3` is the current one. See [Packet structure](#-packet-structure).

### [Codec.py](AlLoRa/Codec.py)

The narrow seam that *speaks* a Packet on the wire: `frame`, `deframe`, plus a keyless `match_spec`
for matching a reply to its request and `payload_overhead()` for what the framing costs. Protocol
version and security posture both hide behind this one interface (v2, v3-open, v3-secure today), so a
new version or a new security mode is a new implementation and never a wider interface.

### [Pacing.py](AlLoRa/Pacing.py)

The one home for adaptive timing: the receive window, the gap between requests, and the airtime
budget. Pure policy, no I/O. The fixed inter-request sleep of early versions is gone; the controller
finds the shortest gap the link tolerates on its own. To bound it at runtime, use
`node.pacing.set_sleep_bounds(min_sleep, max_sleep)`.

</details>

-----

# How does it work?

<p align="center">
  <img width="700" src="readme_assets/figures/Modules-AlLoRa.png">
</p>

The protocol is structured symmetrically. On the left is the Edge, holding an `AlLoRa_File` fed to it
by a `DataSource` and reaching LoRa through a `Connector`. On the right is the Hub, holding a
`Digital_Endpoint` per Edge, reaching LoRa through its own `Connector`, and handing each completed
file to a `DataSink`.

*(The figure is from the v2 papers and still shows the old `Source` and `Requester` names. The
[mapping table](#coming-from-v2) reads it into the current vocabulary.)*

## → Communication logic

The transfer runs on requests from the collector-role node to the source-role node. Depending on the
state of the `Digital_Endpoint`, the collector sends a request and waits for a reply. If nothing
arrives, or it arrives corrupted, the request is repeated until it lands, with a timeout where one is
needed.

<img align="right" width="400" src="readme_assets/figures/Untitled%201.png">

A `Digital_Endpoint` moves through these states:

1. **Establish connection.** Every endpoint starts here. A simple OK packet goes out and the endpoint
   waits for an OK back. In secure mode this is also where the handshake happens.

2. **Ask metadata.** The first step of receiving a file: ask what is coming, and how much of it. The
   endpoint creates an empty `AlLoRa_File` to act as the container. v3's typed metadata also carries
   the sender's chunk size, so the receiver can place each arrival at the right offset instead of
   assuming both ends chose the same size.

3. **Ask for data.** Chunks are requested until the container is full. Each arrival is written into
   place; once every chunk is in, the file is assembled and handed to the `DataSink`.

4. **Final acknowledge.** A closing OK keeps the two nodes in step, and the source-role node waits
   for it before considering the file delivered and re-arming for the next one.

## → Packet structure

The Packet is the fundamental unit on the air. A LoRa frame carries at most 255 bytes at SF7 through
SF10 (less at SF11 and SF12), so every byte of header is a byte of payload lost, and the header is
where AlLoRa spends its optimisation effort.

### v3

v3 makes the format **self-describing** and stops paying for addresses it does not need:

```
 MAC-addressed  [src4][dst4][VT1][FL1][integ3]   = 13 B   (v2-compatible first contact)
 did-addressed  [did4][VT1][FL1][integ3]         =  9 B   (v3 first contact, no MAC on the wire)
 sid-addressed  [sid1][VT1][FL1][integ3]         =  6 B   (established session)

 VT     = version (4 bits, 0x3) | kind (4 bits)
 FL     = mesh | sleep | hop | debug_hops | role_token | auth | cfg_epoch | spare
 integ  = sha256(payload)[:3]
```

Three changes, no fattening:

- A **version nibble** makes the format extensible, and a **typed kind nibble** replaces v2's 2-bit
  command. v2 had three different features contending for one spare flag bit; v3 has room for role
  swap, control commands and encryption side by side.
- **Session-id addressing** collapses two 4-byte MACs to **one byte** once a session exists. MACs
  survive in the handshake, where no session exists yet, and in mesh, where a relay needs the real
  destination.
- The integrity trailer is a **real 24-bit** `sha256(payload)` digest, rather than v2's 12-bit hex in
  a 3-byte field.

Mesh inserts a 2-byte sequence number before the integrity trailer, so the sid-addressed mesh header
is 8 bytes.

**Secure mode** replaces the integrity trailer with an AEAD tag and adds an anti-replay counter. It
drops the flag byte entirely, since every flag it carried is either mesh-scoped or unbuilt and secure
mode is point to point:

```
 sid-addressed  [sid1][VT1][ctr2] + sealed(payload) + [tag4]   = 8 B overhead
```

So an authenticated, encrypted v3 chunk still costs **less header than an unencrypted v2 one**.

| framing | header cost | payload at SF7 |
|---|---|---|
| v2, point to point, compressed MACs | 12 B | 243 B |
| v2, point to point, full MACs | 20 B | 235 B |
| v2, mesh, compressed MACs | 14 B | 241 B |
| **v3 open, session-addressed** | **6 B** | **249 B** |
| v3 open, session-addressed, mesh | 8 B | 247 B |
| **v3 secure, session-addressed** | **8 B** | **247 B** |

Nodes do not have to be told any of this. `chunk_size` in `AlLoRa.json` is a request, and the node
caps it at whatever its own codec reports as the framing cost, so a v3 node is not silently held at
v2's ceiling and a chunk size that would not fit is corrected at startup rather than on the air.
Leave the key out and the node uses that computed ceiling directly, which is the number a config
file has no way of knowing.

### v2

<details>
<summary>The v2 header, still spoken by the v2 codec for legacy peers.</summary>

- **MAC addresses (16 bytes):** 8 bytes of source MAC then 8 of destination.
- **Command and flags (1 byte):** the command type plus several flags.
- **Checksum (3 bytes):** the last 3 bytes of a SHA-256 digest, as hex.
- **Message ID (2 bytes, mesh only):** a random number between 0 and 65,535, used to suppress
  duplicate retransmissions.

Optional MAC compression packs each address from 8 bytes to 4 with binary struct packing, which is
what the 12-byte and 14-byte rows above are. It is configurable per deployment, for backward
compatibility with older installs.

<div align="center">
<table>
<tr>
<th>Point-to-Point Packet</th>
<th>Mesh Packet</th>
</tr>
<tr>
<td>
<pre>
<img align="center"
  src="readme_assets/figures/Packet-p2p.png"
  title="Point-to-Point Packet"
  width="300"
  style="background-color: white; padding: 10px;" />
</pre>
</td>
<td>
<pre>
<img align="center"
  src="readme_assets/figures/Packet-mesh.png"
  title="Mesh Packet"
  width="300"
  style="background-color: white; padding: 10px;" />
</pre>
</td>
</tr>
</table>
</div>

The v2 flag byte:

<p align="center">
  <img src="readme_assets/figures/Flags.png"
       alt="Flag Byte Composition"
       width="500" />
</p>

1. **Command (2 bits):** `00` DATA (the payload is a requested chunk), `01` OK (connection
   acknowledged, or content correctly received), `10` CHUNK (a request for a specific chunk, whose
   number is in the payload), `11` METADATA (file name and size, requested or provided).
2. **Mesh bit:** the message should be forwarded.
3. **Hop bit:** the message was forwarded at least once.
4. **Debug hop bit:** replace the content with path details, for research.
5. **Change RF bit:** a change of radio configuration between nodes.

</details>

## → Identity and addressing

<details>
<summary>How a node is named, and how the first frame to it is addressed.</summary>

- **MAC.** A physical address. In v2 it was also the identity. In v3 it survives as a human label,
  as the folder and topic key, and as the bootstrap address for a node that might still be v2.
- **device_id.** An Edge's v3 identity: the fingerprint of its long-term public key,
  `SHA256(pubkey)`. Registered on the Hub the way a MAC was, but crypto-bound and therefore
  unforgeable. It is stable across reboots as long as `identity_file` is set.
- **session id (sid).** The 1-byte logical address of one Edge and Hub session, at offset 0 of every
  established-session frame. Identity-derived by default (the first byte of the `device_id` in secure
  mode, the device-specific low byte of the short MAC in open mode), overridable with an explicit
  `session_id` in the config, and reassigned by the Hub on the rare 1-byte clash. It stays one byte
  even when the link is tunnelled.

**How the first frame is addressed follows how the node was registered, never blind probing.**
Registered by MAC means v2-compatible framing, which is what makes interop and beacon upgrade
possible. Registered by `device_id` means v3 framing, with no MAC on the wire at all.

</details>

## → Security

<details>
<summary>Two postures, and what the secure one actually protects.</summary>

`security_mode` in `AlLoRa.json` selects the posture:

- **`open`** (the default): no crypto. The integrity trailer catches corruption, not an attacker.
- **`secure`**: an ephemeral-static **ECDH handshake on P-256** establishes a session, then every
  data frame is sealed with **AES-128-CTR and authenticated with a truncated HMAC-SHA256 tag**,
  encrypt-then-MAC, and carries a counter that a sliding replay window checks on arrival.

The asymmetric cost is paid once per session, never per frame. The curve math is dependency-free
pure Python so it runs on the firmware with no native crypto module; the per-frame AES is
platform-detected instead, so it uses the ESP32's hardware AES on-device and stays well under the
receive-to-reply turnaround the adaptive pacing depends on. Where no native AES exists, `secure`
degrades to `open` rather than pretending.

The security perimeter is the **Edge to Hub LoRa link**. Whatever happens downstream of the Hub, on
its way to a cloud or a database, is secured separately by that deployment.

The **control root** is the other anchor, and it is separate on purpose. It is a deployment-level
ECDSA P-256 signing root whose public key every node holds from provisioning. An Edge verifies that
any downlink control artifact, a config change, a reset, an OTA or a returned model, chains to the
control root before acting on it. Because it is per *deployment* rather than per Hub, Hubs stay
swappable without re-touching every Edge.

Role reversal moves the drive, never the trust anchor: a temporarily driving Edge is still
authenticating *to* its Hub.

</details>

## → RF configuration and the trial

<details>
<summary>Why a radio reconfiguration is never committed on arrival.</summary>

Changing spreading factor, bandwidth, frequency, coding rate or TX power has to happen over the very
link the change might break. A verified `RF_CONFIG` is therefore never committed on arrival. The node
switches, runs a bounded **trial** on the new config, and then either commits it or falls back to the
**last-known-good** config, which is the one that was in force before and is therefore known
reachable. Committing a trial is what promotes the new config to last-known-good.

The verdict is a **completed full-payload exchange**, not just any frame: a short frame is not proof
that a full chunk will land. Short frames put the trial in **hold-pending**, extending the window
without deciding it. Silence for the whole window restores.

The window is time-based, not a cycle count, because time on air swings roughly thirty-fold between
SF7 and SF12. The signed payload carries `trial` in seconds; when it is absent, both ends derive a
default scaled to time on air.

The two ends are deliberately asymmetric. The Edge self-restores on silence. The Hub keeps the old
config beside the new one for that endpoint and **probes both**, alternating on its normal poll loop,
so nothing new blocks the radio.

</details>

## → Mesh mode

With mesh mode active, communication works exactly as described above, except that when a request
goes unanswered a set number of times the `Digital_Endpoint` enters **retransmission mode**: it sets
the mesh bit, asking other nodes in range to forward the message and extend the system's reach.

A node that receives a packet not addressed to it normally discards it. With the mesh bit set, it
forwards it instead, after sleeping a random 0.1 to 1 second to reduce collisions when several nodes
are active and in range of each other. Each forward sets the hop bit, so the path is visible. When
the destination sees that a message arrived in retransmission mode, it replies with the mesh bit set
too, on the assumption that the reply needs the same path back, and it does *not* sleep first: the
node being asked for something always has priority.

If a reply arrives with the hop bit clear, it went straight there and retransmission is not needed,
so the collector clears retransmission mode for that endpoint.

To stop duplicates from collapsing the system, each new packet gets a random id, which every node
keeps in a fixed-size list and checks whenever a mesh-bit packet arrives. A second fixed-size list
holds forwarded ids, so nothing is forwarded twice.

**Secure mode is point to point only.** The sealed header has no sequence number, so a secure node
that believed it was forwarding would in fact be framing point to point and fail in a way that looks
like a radio fault. The library refuses that combination at startup, where a misconfiguration
belongs, rather than at the first exchange. (This is about AlLoRa's own flooding mesh. Riding a
Meshtastic mesh keeps the frame point to point, with the routing in the Meshtastic frame, so it needs
nothing here.)

## → Debug hops

Debug hops is an option on a collector-role node, and a useful tool for checking the path of a packet
in mesh mode. It overrides the message content and uses the payload to record each node the packet
passes through. The information can be retrieved from the collector's storage afterwards and used to
decide how to distribute nodes across an area.

The output is a `log_rssi.txt` that looks like this:

```
2022-06-17_17:11:40: ID=24768 -> [['B', -112, 0.5], ['A', -107, 0], ['B', -106, 0.3], ['C', -88, 0.2], ['G', -100, 0]]
2022-06-17_17:11:50: ID=2065 -> [['C', -99, 0.4], ['B', -93, 0], ['C', -93, 0.2], ['G', -105, 0]]
2022-06-17_17:11:53: ID=63728 -> [['C', -100, 0.4], ['B', -95, 0], ['C', -95, 0.5], ['G', -103, 0]]
2022-06-17_17:11:54: ID=32508 -> [['B', -114, 0], ['C', -95, 0.4], ['G', -103, 0]]
2022-06-17_17:11:56: ID=10063 -> [['C', -99, 0.1], ['B', -95, 0], ['C', -94, 0.1], ['G', -103, 0]]
```

Each line is a reception time, the message id, and the list of hops. Each hop records the node's
name, the RSSI of the last packet it received over LoRa, and the random time it waited before
forwarding. Some of those waits are 0, which is not chance: those nodes were the destination of the
request, and as noted above they have priority.

-----

# Repository layout

```
AlLoRa/          the library
examples/        runnable examples, by node type and by board
firmware/        MicroPython firmware and build targets for the supported boards
tests/           the test suite (CPython)
```

Inside `AlLoRa/`, three conventions are worth knowing before you go looking for something:

**1. Core at the root, interchangeable families in folders.** The loose top-level modules are the
core pieces every node uses: `Packet`, `Packet_v3`, `Codec`, `Pacing`, `File`, `Digital_Endpoint`,
`Status`, `negotiation`, `tunnel_codec`. A folder means a family of **alternatives** behind one shared
contract or base, from which a deployment picks: `Nodes/`, `Connectors/`, `Links/`, `Adapters/`,
`DataSources/`, `DataSinks/`.

**2. Some folders are planes, not families.** `Security/` and `Control/` are folders whose members are
*not* interchangeable with each other: they group a domain rather than a set of alternatives. You do
not choose one file from `Security/`, you use the whole thing. The test is whether the members
substitute for one another.

**3. A lowercase filename means the module defines no class.** `negotiation.py`, `tunnel_codec.py`,
`mqtt_naming.py`, `control_types.py` and everything in `utils/` are functions and constants. A
capitalized filename is the name of the class the module provides.

One naming rule falls out of this: a `_DataSink` suffix is a **plug type**, not
decoration. It promises `consume(file, reception)` and a node's sink slot. `Control_Root_DataSink`
carries the suffix because it really is one; `Control_Actuator` does not, because it takes
`apply(control_type, payload)` and plugs into the gate instead.
