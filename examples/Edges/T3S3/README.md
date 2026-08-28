# T3S3 Edge node

This folder contains the code for enabling a T3S3 device as an Edge node. 
There are two main files in this folder:

- **main.py**: This file contains a simple example of how to use the AlLoRa library to send files to a Hub. The Edge generates files with random content and serves them to the Hub that polls it. This is the one used in the example described in the [Readme from the examples folder](../../Readme.md).
- **LoRa.json**: This file contains the configuration for the LoRa module. It is used by the AlLoRa library to configure the LoRa module. You should be sure to have both the Edge and the Hub with the same LoRa configuration in order to establish a proper communication between them.

The **main_pro.py** that used to sit beside them, a second copy of the whole program built around
the screen and the SD card, is gone. Those two features are a `device` block in the config now,
read by the same program every v3 node runs: see [`v3_hello/pro`](../../v3_hello/pro).
