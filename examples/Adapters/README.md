## Adapters

Sometimes, another device is needed in order to bridge to LoRa, depending on the technology used for the connection. This folder contains multiple adapters for different use cases. 

An Adapter is named after the medium it bridges to (`Serial_adapter`, `WiFi_adapter`) and takes the LoRa connector as its argument, so `WiFi_adapter(SX127x_connector())` reads as "bridge to WiFi, over this radio". The radio varies per board independently of the link, which is why the two stay on separate axes.

For example, if we want to use a Raspberry Pi as a Hub, we can use the WiFi adapter to connect the LoRa module to the Raspberry Pi. The WiFi adapter can act as a hotspot or be connected to the same network as the Raspberry Pi. 
It is important to note that the WiFi adapter is not a node at all, it is just a bridge between the LoRa module and the Raspberry Pi. The Raspberry Pi will run the Hub part of the code and send commands to the LoRa module through the WiFi adapter, which will send the AlLoRa commands to the Edges, wait for the response and send it back to the Raspberry Pi.

The simplest wiring of all is USB. Plug the board into the Raspberry Pi with one cable and it is that host's LoRa modem: no pins, no baud rate, nothing to agree on. That is the `USB_adapter`, and on the host side it is the same serial connector as the UART case, pointed at the port the board turned up on (`/dev/ttyACM0` on a Pi, `/dev/cu.usbmodem*` on macOS).

Which adapter a board needs depends on what is behind its USB socket. A board with native USB has no UART there, so it needs `USB_adapter`; the T3S3 is that kind, and so are the other ESP32-S3, ESP32-C3 and RP2040 boards, though only the T3S3 has been run. A board whose socket goes through a USB-to-serial chip (a LoPy4, an E5 on a Grove-to-USB adapter) already sees an ordinary UART, so it uses `Serial_adapter` on UART0 and needs nothing new at all. Either way the host sees a plain serial device and does not need to know which it is.

One thing is different about a USB adapter, and it is worth knowing before you debug one: the board's console is the cable, so the board cannot print to it. Debug output would land inside the data and corrupt it. `USB_adapter` therefore silences the library's logging for you, and offers `"log": "file"` in its `adapter` block for a bench session. Do not leave that on in a deployment; a line per verb is more writing than a board's flash should be asked to do. Pressing Ctrl-C on the host still drops the board back to its REPL, which is the way back in.

If we don't want to relly on a WiFi connection, another alternative is to connect the LoRa-enabled device to the Raspberry Pi through UART. This way, the Raspberry Pi can send commands to the LoRa module through the Serial Adapter, which will send the AlLoRa commands to the Edges, wait for the response and send it back to the Raspberry Pi, just like the WiFi adapter.

The most critical part to configure is the LoRa.json file, which should have parameters for the Connector and the Adapter. For example, here is the LoRa.json for the Serial adapter:

```json
{
    "name": "T",
    "chunk_size": 235,
    "mesh_mode": false,
    "debug": true,
  
    "connector": {
      "sf": 7,
      "freq": 868,
      "min_timeout": 0.5,
      "max_timeout": 12
      },
	
	"adapter":{
		"uartid":0,
		"baud": 9600,
    "tx": 12,
    "rx": 13,
		"bits":8, 
		"parity": null, 
		"stop": 1, 
		"timeout_char":1000
	}
  }
```

It contains some general settings like the name of the board, the chunk_size of the messages, if the mesh_mode is enabled and if the debug mode is on. Then, it has the connector's LoRa settings, like the spreading factor, the frequency, and the timeouts. Finally, it has the specific adapter settings, in this case, for the Serial Adapter, it contains the UART id, the baud rate, the pins used for the communication, the bits, the parity, the stop bits, and the timeout for the communication. A board whose LoRa.json still names that block `interface` keeps booting: the old key is read as a fallback.