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
