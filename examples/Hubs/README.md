# Hub node examples

A Hub is the node at the center of a deployment. It holds one `Digital_Endpoint` per Edge, polls
them, pulls the files they serve, and stays the authority for its sessions.

One class covers both deployment shapes, which is why there is no longer a `Requesters` folder
beside a `Gateways` one. The difference between those two was only scale and where the data goes:

- **[One-Edge](One-Edge)**: a Hub with a single Edge, the quick 1:1 case, running on a LoRa-enabled board. These examples drive the one endpoint directly with `listen_to_endpoint`.
- **[Many-Edges](Many-Edges)**: a Hub with several Edges, running on a computer with no radio of its own, which reaches the channel through an [Adapter](../Adapters). These examples let the Hub run its own visit loop with `run()`.

Either way it is the same object:

```python
from AlLoRa.Nodes.Hub import Hub

lora_hub = Hub(connector, config_file="LoRa.json")
```

`Requester` and `Gateway` survive as deprecated aliases so fielded main.py files keep booting
unchanged. New code says `Hub`.
