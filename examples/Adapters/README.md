## Adapters

Sometimes, another device is needed in order to bridge to LoRa, depending on the technology used for the connection. This folder contains multiple adapters for different use cases. 

An Adapter is named after the medium it bridges to (`Serial_adapter`, `WiFi_adapter`) and takes the LoRa connector as its argument, so `WiFi_adapter(SX127x_connector())` reads as "bridge to WiFi, over this radio". The radio varies per board independently of the link, which is why the two stay on separate axes.

For example, if we want to use a Raspberry Pi as a gateway, we can use the WiFi adapter to connect the LoRa module to the Raspberry Pi. The WiFi adapter can act as a hotspot or be connected to the same network as the Raspberry Pi. 
It is important to note that the WiFi adapter is not a gateway, it is just a bridge between the LoRa module and the Raspberry Pi. The Raspberry Pi will run the Gateway part of the code and send commands to the LoRa module through the WiFi adapter, which will send the AlLoRa commands to the Source Nodes, wait for the response and send it back to the Raspberry Pi.

If we don't want to relly on a WiFi connection, another alternative is to connect the LoRa-enabled device to the Raspberry Pi through UART. This way, the Raspberry Pi can send commands to the LoRa module through the Serial Adapter, which will send the AlLoRa commands to the Source Nodes, wait for the response and send it back to the Raspberry Pi, just like the WiFi adapter.

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

It contains some general settings like the name of the Gateway, the chunk_size of the messages, if the mesh_mode is enabled and if the debug mode is on. Then, it has the connector's LoRa settings, like the spreading factor, the frequency, and the timeouts. Finally, it has the specific adapter settings, in this case, for the Serial Adapter, it contains the UART id, the baud rate, the pins used for the communication, the bits, the parity, the stop bits, and the timeout for the communication. A board whose LoRa.json still names that block `interface` keeps booting: the old key is read as a fallback.