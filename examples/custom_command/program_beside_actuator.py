"""A Pi Edge's actuator for a program running beside AlLoRa, such as a camera's detector.

Stock RF_CONFIG and RESET, plus CUSTOM: the signed bytes are written where the program reads
them, and the program is restarted. RESET still restarts AlLoRa itself, never this program.
"""
import os

from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.Control.control_types import CUSTOM


class Program_Beside_Actuator(Node_Control_Actuator):

    handles = Node_Control_Actuator.handles + (CUSTOM,)

    def __init__(self, node, drop_path, restart, reset_fn=None):
        super().__init__(node, reset_fn=reset_fn)
        self.drop_path = drop_path
        self.restart = restart   # restarts the program that reads drop_path

    def apply(self, control_type, payload):
        if control_type != CUSTOM:
            return super().apply(control_type, payload)
        path, restart, data = self.drop_path, self.restart, bytes(payload)

        def hand_over():
            # Written beside the file and renamed into place, so the program never reads half.
            temp = path + ".part"
            with open(temp, "wb") as f:
                f.write(data)
            os.replace(temp, path)
            restart()

        # Queued, not done now: the node is still acknowledging the transfer.
        self.node.queue_control_action(hand_over)
