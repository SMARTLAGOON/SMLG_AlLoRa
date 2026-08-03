# Edge node examples

This folder contains the code for enabling different devices as Edge nodes. An Edge is the node
that sits out with the sensors: it holds the data and serves it, and its Hub does the polling.
(`Edge` is the v3 name for what v2 called a `Source`. The old name still imports, as a deprecated
alias.)

There are three main files in these folders:

- **main.py**: This file contains a simple example of how to use the AlLoRa library to send files to a Hub. The Edge generates files with random content and serves them to the Hub that polls it.
  
- **main_pro.py**: This file contains a more complex example of how to use the AlLoRa library to send files to a Hub. It takes advantage of the device's hardware, and uses the screen and the SD card reader. It uses the screen of the device to display the status of the communication and the files being sent. The files to be sent are accessed from the SD card reader. This example can provide a deeper understanding of how to use the AlLoRa library and the device's hardware on a real-world application.
  
- **LoRa.json**: This file contains the configuration for the LoRa module. It is used by the AlLoRa library to configure the LoRa module. You should be sure to have both the Edge and the Hub with the same LoRa configuration in order to establish a proper communication between them.
