# The Edge half of the wizard's verify step. Bounded: it serves for a fixed window and exits.
#
# It serves the same payload the v3_hello examples serve, so what the wizard proves on a fresh
# deployment is the transfer the bench has already run many times, not a new one.
import gc

from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.__RADIO_MODULE__ import __RADIO_CLASS__

gc.enable()

edge = Edge(__RADIO_CLASS__(), config_file="LoRa.json")
print("VERIFY:mode", edge.security_mode)
print("VERIFY:device_id", edge.device_id.hex() if edge.device_id is not None else "none")
print("VERIFY:sid", edge.session_id)

payload = bytes((i % 256) for i in range(__PAYLOAD_LEN__))
edge.set_file(AlLoRa_File(name="__PAYLOAD_NAME__", content=bytearray(payload)))
print("VERIFY:serving __PAYLOAD_NAME__")

# No chunk_size: the node cuts the file at whatever its frames can carry in the posture it is
# speaking, which is the only place that number is known.
edge.run(timeout=__WINDOW__)
print("VERIFY:result served")
