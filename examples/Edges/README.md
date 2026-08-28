# Edge node examples

This folder contains the code for enabling different devices as Edge nodes. An Edge is the node
that sits out with the sensors: it holds the data and serves it, and its Hub does the polling.
(`Edge` is the v3 name for what v2 called a `Source`. The old name is gone; a fielded main.py
needs the rename and a reflash, or the `v2.0.0` tag to keep running as it is.)

There are two main files in these folders:

- **main.py**: This file contains a simple example of how to use the AlLoRa library to send files to a Hub. The Edge generates files with random content and serves them to the Hub that polls it.
  
- **LoRa.json**: This file contains the configuration for the LoRa module. It is used by the AlLoRa library to configure the LoRa module. You should be sure to have both the Edge and the Hub with the same LoRa configuration in order to establish a proper communication between them.

There used to be a **main_pro.py** in each of these folders, a second copy of the whole node
program carrying the screen and the SD card along with it. Those are gone. A board's peripherals
are now a `device` block in its config, read by the same program every node runs: see
[`v3_hello/pro`](../v3_hello/pro). These `main.py` files are still v2 and still call
`establish_connection()`, which a v3 node refuses; [`v3_hello`](../v3_hello) is where to start.
