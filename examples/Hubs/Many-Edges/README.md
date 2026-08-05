# Hub examples: many Edges

This folder contains the code for running a Hub on a device that has no LoRa module of its own, so
it reaches the channel through an [Adapter](../../Adapters). (This is the deployment v2 called a
`Gateway`: one node polling several Edges and passing the data onward.)

The Serial example has been developed for the Raspberry Pi, but it can be adapted to other devices with UART capabilities.
The WiFi example has been tested both in Raspberry Pi and Linux/MacOS devices connected to the same network or to the WiFi Adapter's hotspot.

There are three main files in each folder:

- **main.py**: This file contains a simple example of how to use the AlLoRa library to poll several Edges. `Hub.run()` visits each registered endpoint in turn, for that endpoint's listening time, pulls whatever it is serving, and comes back to the next one.
- **LoRa.json**: This file contains the configuration for the Connector module. It is used by the AlLoRa library to configure the Connector module. You should be sure to have both the Edge and the Hub with the same LoRa configuration in order to establish a proper communication between them.
- **Nodes.json**: This file contains the configuration for the Edges. It is used by the AlLoRa library to build the Digital Endpoint for each Edge. Each Edge must be registered and be active in order to be polled by the Hub. Different Edges can have different LoRa configurations, and the Hub will adjust its Connector configuration to match the Edge it is about to visit.

## Per-Edge radio settings

An entry that says nothing about radio is polled on **the Hub's own `LoRa.json` config**. That is the
common case: if both ends share the same LoRa configuration, there is nothing to write down.

To poll one Edge on different settings, paste that Edge's own `connector` block from its `LoRa.json`
into its entry, unedited:

```json
{
    "name": "GPS2",
    "mac_address": "da5ace8c",
    "active": true,
    "asking_frequency": 300,
    "listening_time": 60,
    "connector": {
        "freq": 868,
        "sf": 8,
        "bandwidth": 125,
        "coding_rate": 1,
        "tx_power": 14
    }
}
```

Anything the block leaves out still comes from the Hub's `LoRa.json`, so `"connector": {"sf": 12}` is
enough to say "this one is far away".

Only those five settings are read. A block pasted whole will also carry the Edge's own `min_timeout`,
`max_timeout`, `debug` and, on a serial node, `serial_port` and `baud`. Those describe how *that*
node reaches its own radio, so they mean nothing here and are ignored; run the Hub with `debug` on
and it prints which keys it skipped. The timeouts in particular never need copying: the Hub
recalculates them from the spreading factor and bandwidth every time it retunes.

Older `Nodes.json` files set `sf`, `bw` and `cr` directly on the entry instead of in a `connector`
block. That still works and does the same thing.
