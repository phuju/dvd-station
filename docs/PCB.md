# DiscStation Remote PCB

Moving the remote from hand-wired modules (ESP32 DevKit V1, OLED module,
encoder module, power-bank board) to one board. Rule: **every GPIO stays the
same**, so `arduino/DiscStation/DiscStation.ino` and the host need no changes.

## Net list

| Signal | ESP32 GPIO | Connects to | Parts on the net |
|---|---|---|---|
| SDA | 21 | OLED header pin SDA | 4.7 kΩ to 3V3 |
| SCL | 22 | OLED header pin SCL | 4.7 kΩ to 3V3 |
| ENC_CLK | 32 | EC11 A | 10 kΩ to 3V3, optional 10 nF to GND |
| ENC_DT | 33 | EC11 B | 10 kΩ to 3V3, optional 10 nF to GND |
| ENC_SW | 4 | EC11 switch (other side to GND) | 10 kΩ to 3V3 |
| BTN_EJECT | 14 | button to GND | 10 kΩ to 3V3 |
| BTN_HOME | 13 | button to GND (also the 10 s Wi-Fi reset hold) | 10 kΩ to 3V3 |
| BTN_PLAYPAUSE | 15 | button to GND | 10 kΩ to 3V3 (strapping pin, must idle high) |
| BOOT | 0 | BOOT button to GND, auto-program transistor | 10 kΩ to 3V3 |
| EN | EN | RESET button to GND, auto-program transistor | 10 kΩ to 3V3, 1 µF to GND |
| TXD0 / RXD0 | 1 / 3 | CP2102N RXD / TXD (crossed) | |
| VBAT_SENSE (optional) | 34 | battery divider | 100 kΩ / 100 kΩ, 100 nF to GND |

EC11 common pin (C) goes to GND. The OLED module and the encoder run from
**3V3**, never 5 V: ESP32 inputs are not 5 V tolerant.

Leave GPIO 6-11 unconnected (module flash). Don't use GPIO 12 (it sets the flash
voltage at boot).

## Bill of materials

| Function | Part | Notes |
|---|---|---|
| MCU | ESP32-WROOM-32E (4 MB) | Antenna at the board edge, no copper under it on any layer |
| USB-serial | CP2102N (QFN-24) | The host finds it by "cp210" in the port name (`serial_port()` in `src/discstation_host.py`). A CH340 is not auto-detected |
| Auto-program | Dual NPN (UMH3N or 2× S8050) + 2× 10 kΩ | Copy the DevKit V1 circuit: DTR/RTS → EN and GPIO0 |
| USB connector | USB-C 16-pin receptacle | 5.1 kΩ from CC1 and CC2 to GND each |
| 3.3 V regulator | AP2112K-3.3 or ME6211C33 (≥500 mA, low dropout) | 1 µF in, 10 µF + 100 nF out. Not AMS1117: its 1.1 V dropout fails on a Li-ion cell |
| Decoupling | 10 µF + 100 nF | At the module's 3V3 pin |
| Charger | TP4056 or MCP73831 | 1S Li-ion from USB 5 V; set the charge current with the program resistor |
| Power path | P-MOSFET + Schottky diode | Runs from USB while charging, from the battery otherwise |
| Power switch | Slide switch | Battery side |
| OLED | 0.96" SSD1306 I2C module on a 4-pin header | Address 0x3C. Recommended over the bare 30-pin FPC panel for v1 |
| Encoder | EC11 (with push switch) | Through-hole |
| Buttons | 3× Cherry MX-style key switches (PLAY, HOME, EJECT) + 2× small tactile (BOOT, RESET) | The PCB-mount footprint takes 3- and 5-pin switches |
| LED | Any colour + series resistor | ~1-2.2 kΩ for blue at 3.3 V. Without a resistor the LED and the 3V3 rail are stressed |

Check stock and pick exact part numbers on LCSC when ordering; availability
changes.

## Layout notes

- Two layers is enough: solid GND pour on the bottom.
- Keep USB D+/D- short and side by side.
- Put the LDO and its capacitors close to the module.
- Place the encoder, buttons and OLED header for the enclosure first, then route.
- Run EasyEDA's DRC before ordering.

## Fabrication

Order with assembly (e.g. JLCPCB) for the fine-pitch parts: the CP2102N, LDO,
USB-C connector and WROOM module. Hand-solder the through-hole parts (encoder,
buttons, OLED header).

## Bring-up checklist

1. Power only: 3V3 rail is 3.3 V from USB, then from the battery.
2. USB: `lsusb` lists a Silicon Labs CP210x.
3. Flash the current firmware with no button presses:
   `arduino-cli compile --upload -p /dev/ttyUSB0 -b esp32:esp32:esp32 arduino/DiscStation`
4. Host: the service log shows `DISCSTATION_READY` and "Remote attached".
5. OLED shows the menu; the encoder scrolls both ways and clicks; the three buttons work.
6. Wi-Fi: the setup portal appears and the remote joins the network.
7. Battery: it charges, runs unplugged, and does not shut off when idle.
