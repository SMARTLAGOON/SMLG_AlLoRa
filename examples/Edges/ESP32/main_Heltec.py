from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.File import AlLoRa_File

from time import sleep
import micropython
import gc

gc.enable()

# For testing
sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]	#, 1024
file_counter = 0

def clean_timing_file():
	test_log = open('log.txt', "wb")
	test_log.write("")
	test_log.close()

if __name__ == "__main__":
	# First, we set the connector (basyc LoRa-LoPy connection to access to the LoPy's LoRa libraries)
	connector = SX127x_connector()

	# Then, we set up our Edge node, giving it the connector and the path for the configuration file
	lora_node = Edge(connector, config_file = "LoRa.json")

	chunk_size = lora_node.get_chunk_size()		# We use it to create the files to be sent...

	try:
		clean_timing_file()
		#show_in_screen(screen, "Waiting", '-')
		backup = lora_node.establish_connection()
		#print("Connected!")
		#show_in_screen(screen, "Connected!", '-')

		# This is how to handle a backup file if needed (not implemented in this example...)
		if backup:
			print("Asking backup")
			#file = datasource.get_backup()
			#lora_node.restore_file(file)

		# with an established connection, we start sending data periodically
		while True:
			if not lora_node.got_file():
				gc.collect()
				n = file_counter % len(sizes)
				file_counter += 1
				size = sizes[n]
				print("Setting file")

				file = AlLoRa_File(name = '{}.json'.format(size),
								content = bytearray('{}'.format(n%10)*(1024 * size)),
								chunk_size=chunk_size)
				lora_node.set_file(file)

				print("New file set, ", file.get_name())
				#show_in_screen(screen, file.get_name(), file.get_length())

			lora_node.send_file()

	except KeyboardInterrupt as e:
		print("THREAD_EXIT")
