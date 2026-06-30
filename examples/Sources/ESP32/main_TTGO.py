from AlLoRa.Nodes.Source import Source
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.File import AlLoRa_File

from time import sleep
import micropython
import gc

from lora32 import Lora32
from lilygo_oled import OLED

gc.enable()
l = Lora32()
screen = OLED(l.i2c)

# For testing
sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]	#, 1024
file_counter = 0

def clean_timing_file():
	test_log = open('log.txt', "wb")
	test_log.write("")
	test_log.close()

def show_in_screen(screen, filename, total_chunks):
	screen.fill(0)
	screen.fill_rect(0, 0, 32, 32, 1)
	screen.fill_rect(2, 2, 28, 28, 0)
	screen.vline(9, 8, 22, 1)
	screen.vline(16, 2, 22, 1)
	screen.vline(23, 8, 22, 1)
	screen.fill_rect(26, 24, 2, 4, 1)
	screen.text("AlLoRa", 40, 0, 1)
	screen.text(filename, 40, 12, 1)
	screen.text("Chunks: {}".format(total_chunks), 40, 24, 1)
	screen.show()

if __name__ == "__main__":
	# First, we set the connector (basyc LoRa-LoPy connection to access to the LoPy's LoRa libraries)
	connector = SX127x_connector()

	# Then, we set up out Source Node, giving it the connector and the path for the configuration file
	lora_node = Source(connector, config_file = "LoRa.json")

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
			#file = Datasource.get_backup()
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
