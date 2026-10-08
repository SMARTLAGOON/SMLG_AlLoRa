"""An ESP32 Edge's actuator for a device on its serial wire, such as a camera board.

Stock RF_CONFIG and RESET, plus CUSTOM: the signed bytes are written to a UART as they came,
behind two length bytes. That framing is this deployment's choice; change it to fit your device.
"""
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.Control.control_types import CUSTOM


class Serial_Device_Actuator(Node_Control_Actuator):

    handles = Node_Control_Actuator.handles + (CUSTOM,)

    def __init__(self, node, uart, reset_fn=None):
        super().__init__(node, reset_fn=reset_fn)
        self.uart = uart   # anything with write(bytes): machine.UART on a board

    def apply(self, control_type, payload):
        if control_type != CUSTOM:
            return super().apply(control_type, payload)
        uart = self.uart
        frame = len(payload).to_bytes(2, "big") + bytes(payload)
        # Queued, not written now: the node is still acknowledging the transfer, and a slow
        # device would hold that acknowledgement up until the Hub sends the file again.
        self.node.queue_control_action(lambda: uart.write(frame))
