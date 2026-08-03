
# Running an example in LoPy4

Check the examples folder for more information about how to run the examples!


### Hardware Requirements


- [Lopy4](https://pycom.io/product/lopy4/) with an expansion board like:
  - [Pysense 2.0 X](https://pycom.io/product/pysense-2-0-x/)
  - [Pygate 868](https://pycom.io/product/pygate/)

### Setup:

In order to run an example, if you are using a Python compatible device, you should install the latest version of the AlLoRa library. If your device has MicroPython support (like the LoPy4), we recommend copy the AlLoRa folder of this repo directly into your device.

### Setup a LoPy4
   
*   <details>
    <summary><b>1. Updating the expansion boards (Pysense 2.0 X or Pygate)</b></summary>
 
    
    Follow this: [Updating Expansion Board Firmware](https://docs.pycom.io/chapter/pytrackpysense/installation/firmware.html)
    * <details>
      <summary><b><i>TL;DR ⚡ </i></b></summary>

      >    
      > ⚠️ You should remove the LoPy4 from the board for this step, we are only working with the Pysense 2 or the Pygate
      >

        1. Download this:

            • **[Pysense 2 DFU](https://software.pycom.io/findupgrade?key=pysense2.dfu&type=all&redirect=true)**

            • **[Pygate](https://software.pycom.io/findupgrade?key=pygate.dfu&type=all&redirect=true)**

        2. Install dfu-util:
            - MacOs

                ```bash
                brew install dfu-util
                ```

            - Linux

                ```bash
                sudo apt-get install dfu-util
                ```

            - Windows

                Harder, follow the [official explanation](https://docs.pycom.io/chapter/pytrackpysense/installation/firmware.html) or check-out this video:

                [https://www.youtube.com/watch?v=FkycTZvj-ss](https://www.youtube.com/watch?v=FkycTZvj-ss)

        3. Use dfu-util to update each expansion board

            Write this in the terminal

            - MacOs and Linux
                - Update Pysense 2:

                    ```bash
                    sudo dfu-util -D pysense2_v16.dfu #This name will change with new versions, match it...
                    ```

                - Update Pygate:

                    ```bash
                    sudo dfu-util -D pygate_v13_1049665.dfu #This name will change with new versions, match it...
                    ```

            - Windows
                - Update Pysense 2:

                    ```bash
                    dfu-util-static.exe -D #This name will change with new versions, match it...
                    ```

                - Update Pygate:

                    ```bash
                    dfu-util-static.exe -D #This name will change with new versions, match it...
                    ```


            Connect the expansion board to your computer while pressing the DFU button (toggle to check where it is depending of the board...)

            - Pysense 2

                ![Untitled](../../../readme_assets/Hardware_Setup/Untitled.png)
                

            - Pygate

                ![Untitled](../../../readme_assets/Hardware_Setup/Untitled%201.png)


            Wait 1 second, release the DFU button and press enter in the terminal to run the code.

            As a result, you should expect something like this:

            ![Untitled](../../../readme_assets/Hardware_Setup/Untitled%202.png)

        4. Check it with:

            ```bash
            lsusb
            ```

            You should expect something like this:

            ```bash
            Bus 000 Device 001: ID 04d8:f012 Microchip Technology Inc. Pysense  Serial: Py8d245e
            ```
    </details>
*   <details>
    <summary><b>2. Update the Lopy4 </b></summary>
 

    1. Download the Pycom Firmware Tool from: [Updating Device Firmware](https://docs.pycom.io/updatefirmware/device/)

    2. Download this legacy firmware: [LoPy4-1.19.0.b4.tar.gz](https://software.pycom.io/downloads/LoPy4-1.19.0.b4.tar.gz)
        - (You can find it here) [Firmware Downgrade](https://docs.pycom.io/advance/downgrade/)

    3. Connect each LoPy4 to it’s respective Expansion Board (The LED side of the LoPy should be facing the USB port of the expansion board) ant then plug it on your computer
    4. Open Pycom Firmware Tool and press continue 2 times to get to the “Communication” section
    5. Select the port and the speed (for me 115200 worked ok), select the “Show Advanced Settings” checkbox and select “Flash from local file” and locate the firmware that we downloaded a few steps before (LoPy4-1.19.0.b4.tar.gz).
    6. Select the Erase flash file system and Force update LoRa region and press continue
    7. In the LoRa region selection select your country or region to establish your LoRa frequency.
    8. Press “Done” and it should start updating
    9. Repeat this step with the other LoPy4 with it’s respective expansion board...
    </details>

*   <details>
    <summary><b>3. Setting the environment</b></summary>

    Here is the official [documentation](https://docs.pycom.io/gettingstarted/software/) for this step.

    We’ll need to upload the programs using PyMakr, a library that can be installed into [VS Code](https://code.visualstudio.com/) and [Atom](https://atom.io/) (I will refer to them as [IDE](https://en.wikipedia.org/wiki/Integrated_development_environment))

    
    > ⚠️ I personally used an M1 Pro Macbook Pro and Atom with PyMakr and it worked fine for me.


    - Here is the official Pycom guide to using Atom + PyMakr: [Atom](https://docs.pycom.io/gettingstarted/software/atom/)

    - If you want to use VS Code, here are the official Pycom instructions: [Visual Studio Code](https://docs.pycom.io/gettingstarted/software/vscode/)


    Once you have everything installed and working, you should be able to connect your LoPy4 + expansion board (Pygate  and Pysense 2.0 X for the Edge and the Hub respectively) to your computer using an USB cable and PyMakr should recognise it.
    </details>

*   <details>
    <summary><b>4. Uploading and running code</b></summary>
   
       1. Open the folder of the example you want to run in the LoPy4 in your IDE
       2. Connect your LoPy4 + expansion board to your computer. PyMakr should recognise it and show you something like this:
        
    <p align="center">
     <img width="500" src="../../../readme_assets/Hardware_Setup/Untitled%203.png">
    </p>

     - If it doesn’t do it automatically, you can open the “Connect Device” option and manually select your Port:

     <p align="center">
     <img width="400" src="../../../readme_assets/Hardware_Setup/Untitled%204.png">
     </p>

       3. Press Ctrl+Alt/Opt + s or the “Upload Project to Device” button to upload the code to the LoPy4

    ![Untitled](../../../readme_assets/Hardware_Setup/Untitled%205.png)

    With this, the code will boot automatically each time the LoPy4 is on.

       4. If everything is ok, you should see something like this on the terminal:

    <p align="center">
    <img width="400" src="../../../readme_assets/Hardware_Setup/Untitled%207.png">
    </p>

</details>

</details>


</details>
