# Custom command: send signed bytes to what runs beside AlLoRa

An Edge often has something next to it that AlLoRa does not control: a camera board on the
ESP32's serial wire, or a detector program on the same Pi. A `CUSTOM` command (control type 5)
carries bytes for it. The Hub signs them, the Edge checks the signature, and an actuator you write
hands them over. AlLoRa never reads them, so whether they are a command or a whole file is up to
you.

## Run it on your computer

No boards needed. From the repository root:

```bash
python3 examples/custom_command/run_on_host.py
```

It prints what each Edge handed over:

```
serial device got: take a photo
program restarted, it reads: new detection model
```

## The two actuators

- [`serial_device_actuator.py`](serial_device_actuator.py), for an ESP32: writes the bytes to a
  UART, behind two length bytes.
- [`program_beside_actuator.py`](program_beside_actuator.py), for a Pi: writes the bytes to a
  file the program reads, then restarts the program.

Both extend `Node_Control_Actuator`, so the Edge still takes radio changes and resets. On a board
or a Pi they are built like this:

```python
from machine import UART   # ESP32
actuator = Serial_Device_Actuator(node, UART(1, baudrate=115200, tx=43, rx=44))

import subprocess          # Pi
actuator = Program_Beside_Actuator(
    node, "/var/lib/camera/model.bin",
    restart=lambda: subprocess.run(["systemctl", "restart", "camera.service"]))
```

To use one on a node, build it in place of `Node_Control_Actuator` in `wire_control()` of
[`../v3_hello/main.py`](../v3_hello/main.py), and set up the control root as in
[`../v3_hello/control`](../v3_hello/control).

## Rules your actuator follows

- **List the types you handle** in `handles`. The gate forwards only those, so a command nothing
  handles is dropped, not acknowledged and ignored.
- **Queue, do not act**, in `apply()`. The node is still acknowledging the transfer when `apply()`
  runs. Hand over with `node.queue_control_action(...)`, and the node runs it after the final OK.
- **`CUSTOM` is only accepted signed.** A node without a control root never acts on one.
- **`RESET` restarts AlLoRa itself**: the board on an ESP32, the AlLoRa program on a Pi.
  Restarting your own program belongs in your `CUSTOM` hand-off.
