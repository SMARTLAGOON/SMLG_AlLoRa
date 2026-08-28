# Hub examples: one Edge

This folder contains the code for enabling different devices as a Hub with a single Edge: the 1:1
case, on a board that has its own LoRa module. (This is the deployment v2 called a `Requester`.)

Similar to the Edge examples, these have two main files:

- **main.py**: This file contains a simple example of how to use the AlLoRa library to receive files from an Edge. The Hub polls the Edge until it has a complete file and saves it to the device's memory.

- **LoRa.json**: This file contains the configuration for the LoRa module. It is used by the AlLoRa library to configure the LoRa module. You should be sure to have both the Edge and the Hub with the same LoRa configuration in order to establish a proper communication between them.


In order to poll its Edge, the Hub must register the Edge's MAC address. This is done by setting the `node_mac_address` variable in the Hub's main.py file. The MAC address can be found in the console when the Edge boots up.

Each Edge to be polled is represented by its own instance of a Digital Endpoint (check line 18 in [main.py](T3S3/main.py)). The Digital Endpoint is the Hub's session handle for one remote Edge: it carries that Edge's identity and RF settings, the state of the exchange, and the file in flight.

These examples drive the one endpoint directly, calling `listen_to_endpoint` in their own loop. A
Hub with several Edges runs its own visit loop instead: see [Many-Edges](../Many-Edges).

The **main_pro.py** that used to sit here, a second copy of the program built around the screen
and the card, is gone. A board's peripherals are a `device` block in its config now, read by the
same program every node runs: see [`v3_hello/pro`](../../v3_hello/pro).
