# The Hub half of the wizard's verify step. Bounded: it pulls one file and exits.
#
# Run with `mpremote run`, which soft-resets rather than hard-resets, so the port does not
# re-enumerate and everything this prints is caught from the first line. A capture attached to
# the serial device cannot do that.
#
# Every line it prints for the wizard to read starts with VERIFY:, one fact per line, so the
# host side parses facts rather than scraping a log.
import gc

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.__RADIO_MODULE__ import __RADIO_CLASS__

gc.enable()

hub = Hub(__RADIO_CLASS__(), nodes_file="Nodes.json")
print("VERIFY:mode", hub.security_mode)
# A configured-secure node halts rather than running plaintext, so reaching here with a backend
# is what says the frames are actually sealed and not merely meant to be.
print("VERIFY:aead", "yes" if getattr(hub, "aead", None) is not None else "no")
print("VERIFY:endpoints", len(hub.digital_endpoints))

if not hub.digital_endpoints:
    print("VERIFY:result no-endpoint")
    raise SystemExit

endpoint = hub.digital_endpoints[0]
print("VERIFY:label", endpoint.get_label())
print("VERIFY:sid", endpoint.session_id)

hub.listen_to_endpoint(endpoint, listening_time=__WINDOW__,
                       print_file=False, save_file=True, one_file=True)

# A live session after the transfer is the positive evidence that the handshake completed. It is
# checked after rather than before because first contact is what establishes it.
session = None
if getattr(hub, "session_store", None) is not None:
    session = hub.session_store.get(endpoint.session_id)
print("VERIFY:session", "yes" if session is not None else "no")

# The definitive check. Bytes off the Hub's own flash, compared with what the Edge was serving:
# a transfer that completed and delivered something else is not a transfer that worked.
expected = bytes((i % 256) for i in range(__PAYLOAD_LEN__))
path = hub.result_path + "/" + endpoint.get_label() + "/__PAYLOAD_NAME__"
try:
    with open(path, "rb") as f:
        received = f.read()
    print("VERIFY:bytes", len(received))
    print("VERIFY:intact", "yes" if received == expected else "no")
    print("VERIFY:result pass" if received == expected else "VERIFY:result corrupt")
except OSError:
    print("VERIFY:bytes 0")
    print("VERIFY:intact no")
    print("VERIFY:result no-file")
