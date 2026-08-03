from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.Wifi_connector import WiFi_connector

config_file = "LoRa.json"
node_file = "Nodes.json"
if __name__ == "__main__":
    # First, let's set access to LoRa through a WiFi Connector and its adapter
    connector = WiFi_connector()

    # Set up the Hub with the connector. It reads its Edges from Nodes.json and visits each
    # one in turn for that endpoint's listening_time.
    lora_hub = Hub(connector = connector, config_file = config_file,
                            debug_hops = False)
    # Run the visit loop over the digital_endpoints, printing and saving the files as they come in
    lora_hub.run(print_file_content=True, save_files=True)

