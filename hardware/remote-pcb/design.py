"""DiscStation remote: the one source of truth for the schematic and the PCB.

GPIOs match arduino/DiscStation/DiscStation.ino exactly (no firmware change).
Each part: ref -> (lib_id, value, footprint, {symbol pin number: net}, block).
Unlisted pins of a part are no-connects.
"""

R0603 = "Resistor_SMD:R_0603_1608Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
SOT23 = "Package_TO_SOT_SMD:SOT-23"
SOT235 = "Package_TO_SOT_SMD:SOT-23-5"

PARTS = {}


def part(ref, lib_id, value, fp, pins, block):
    assert ref not in PARTS, ref
    PARTS[ref] = (lib_id, value, fp, pins, block)


def R(ref, value, a, b, block):
    part(ref, "Device:R", value, R0603, {"1": a, "2": b}, block)


def C(ref, value, a, b, block, fp=C0603):
    part(ref, "Device:C", value, fp, {"1": a, "2": b}, block)


# ---- USB-C input -------------------------------------------------------------
USB_VBUS = ["A4", "A9", "B4", "B9"]
USB_GND = ["A1", "B1", "A12", "B12", "SH"]
part("J1", "Connector:USB_C_Receptacle_USB2.0_16P", "USB-C", "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
     {**{p: "VBUS" for p in USB_VBUS}, **{p: "GND" for p in USB_GND},
      "A5": "CC1", "B5": "CC2", "A6": "USB_DP", "B6": "USB_DP", "A7": "USB_DN", "B7": "USB_DN"}, "usb")
R("R1", "5.1k", "CC1", "GND", "usb")
R("R2", "5.1k", "CC2", "GND", "usb")

# ---- Li-ion charger + power path ------------------------------------------------
part("U3", "Battery_Management:MCP73831-2-OT", "MCP73831-2-OT", SOT235,
     {"1": "CHG_STAT", "2": "GND", "3": "VBAT", "4": "VBUS", "5": "PROG"}, "power")
R("R3", "2k", "PROG", "GND", "power")                # charge current 1000/2k = 500 mA
C("C1", "4.7uF", "VBUS", "GND", "power", C0805)
C("C2", "4.7uF", "VBAT", "GND", "power", C0805)
R("R4", "1k", "VBUS", "CHG_LED_A", "power")
part("D3", "Device:LED", "XL-1608UBC-04 (charging)", "LED_SMD:LED_0603_1608Metric", {"1": "CHG_STAT", "2": "CHG_LED_A"}, "front")
part("J3", "Connector_Generic:Conn_01x02", "Battery 1S Li-ion", "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
     {"1": "VBAT", "2": "GND"}, "power")
part("SW4", "Switch:SW_SPDT", "Power", "Button_Switch_SMD:SW_SPDT_PCM12",
     {"1": "VBAT", "2": "BAT_SW"}, "power")
# USB present: gate high, P-FET off, load runs from VBUS via D1. USB absent:
# R5 pulls the gate low and the battery feeds SYS through the FET.
part("Q3", "Transistor_FET:AO3401A", "AO3401A", SOT23, {"1": "VBUS", "2": "SYS", "3": "BAT_SW"}, "power")
R("R5", "100k", "VBUS", "GND", "power")
part("D1", "Diode:SS14", "SS14", "Diode_SMD:D_SMA", {"1": "SYS", "2": "VBUS"}, "power")
C("C3", "10uF", "SYS", "GND", "power", C0805)

# ---- 3.3 V regulator ---------------------------------------------------------
part("U2", "Regulator_Linear:AP2112K-3.3", "AP2112K-3.3", SOT235,
     {"1": "SYS", "2": "GND", "3": "SYS", "5": "+3V3"}, "power")
C("C4", "1uF", "SYS", "GND", "power")
C("C5", "10uF", "+3V3", "GND", "power", C0805)

# battery gauge for later firmware use (GPIO34, ADC1)
R("R6", "100k", "VBAT", "VBAT_SENSE", "power")
R("R7", "100k", "VBAT_SENSE", "GND", "power")
C("C6", "100nF", "VBAT_SENSE", "GND", "power")

# ---- ESP32 ---------------------------------------------------------------------
part("U1", "RF_Module:ESP32-WROOM-32E", "ESP32-WROOM-32E-N4", "RF_Module:ESP32-WROOM-32E",
     {"[1,15,38,39]": "GND", "2": "+3V3", "3": "EN", "25": "BOOT",
      "26": "ENC_SW",       # IO4
      "16": "BTN_HOME",     # IO13
      "13": "BTN_EJECT",    # IO14
      "23": "BTN_PLAY",     # IO15 (strapping pin, idles high)
      "33": "SDA",          # IO21
      "36": "SCL",          # IO22
      "8": "ENC_A",         # IO32
      "9": "ENC_B",         # IO33
      "6": "VBAT_SENSE",    # IO34
      "35": "ESP_TX", "34": "ESP_RX"}, "mcu")
C("C7", "10uF", "+3V3", "GND", "mcu", C0805)
C("C8", "100nF", "+3V3", "GND", "mcu")
R("R8", "10k", "+3V3", "EN", "mcu")
C("C9", "1uF", "EN", "GND", "mcu")
R("R9", "10k", "+3V3", "BOOT", "mcu")
part("SW5", "Switch:SW_Push", "RESET", "Button_Switch_SMD:SW_SPST_TL3342", {"1": "EN", "2": "GND"}, "mcu")
part("SW6", "Switch:SW_Push", "BOOT", "Button_Switch_SMD:SW_SPST_TL3342", {"1": "BOOT", "2": "GND"}, "mcu")

# ---- USB-serial (CP2102N, self-powered from 3V3; VBUS sensed via divider) --------
part("U4", "Interface_USB:CP2102N-Axx-xQFN24", "CP2102N-A02-GQFN24", "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm",
     {"2": "GND", "25": "GND", "3": "USB_DP", "4": "USB_DN", "5": "+3V3", "6": "+3V3", "7": "+3V3",
      "8": "VBUS_SENSE", "9": "CP_RST", "20": "ESP_TX", "21": "ESP_RX", "19": "RTS", "23": "DTR"}, "usbserial")
R("R10", "22.1k", "VBUS", "VBUS_SENSE", "usbserial")
R("R11", "47.5k", "VBUS_SENSE", "GND", "usbserial")
R("R12", "1k", "+3V3", "CP_RST", "usbserial")
C("C10", "1uF", "+3V3", "GND", "usbserial")
C("C11", "100nF", "+3V3", "GND", "usbserial")
# auto-program (same as the DevKit): DTR/RTS drive EN and IO0
part("Q1", "Transistor_BJT:MMBT3904", "MMBT3904", SOT23, {"1": "Q1_B", "2": "RTS", "3": "EN"}, "usbserial")
part("Q2", "Transistor_BJT:MMBT3904", "MMBT3904", SOT23, {"1": "Q2_B", "2": "DTR", "3": "BOOT"}, "usbserial")
R("R13", "10k", "DTR", "Q1_B", "usbserial")
R("R14", "10k", "RTS", "Q2_B", "usbserial")

# ---- controls (front) -------------------------------------------------------------
for ref, net in (("SW1", "BTN_PLAY"), ("SW2", "BTN_HOME"), ("SW3", "BTN_EJECT")):
    # Cherry MX-style key switch; the PCB-mount footprint also takes 3-pin (plate) switches
    part(ref, "Switch:SW_Push", net.split("_")[1], "Button_Switch_Keyboard:SW_Cherry_MX_1.00u_PCB", {"1": net, "2": "GND"}, "front")
part("ENC1", "Device:RotaryEncoder_Switch", "EC11 encoder", "Rotary_Encoder:RotaryEncoder_Alps_EC11E-Switch_Vertical_H20mm",
     {"A": "ENC_A", "B": "ENC_B", "C": "GND", "S1": "ENC_SW", "S2": "GND"}, "front")
for i, net in enumerate(("BTN_PLAY", "BTN_HOME", "BTN_EJECT", "ENC_A", "ENC_B", "ENC_SW"), start=15):
    R(f"R{i}", "10k", "+3V3", net, "controls")
C("C12", "10nF", "ENC_A", "GND", "controls")
C("C13", "10nF", "ENC_B", "GND", "controls")
R("R21", "4.7k", "+3V3", "SDA", "controls")
R("R22", "4.7k", "+3V3", "SCL", "controls")
part("J2", "Connector_Generic:Conn_01x04", "OLED 0.96in I2C (GND VCC SCL SDA)", "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
     {"1": "GND", "2": "+3V3", "3": "SCL", "4": "SDA"}, "front")
R("R23", "220", "+3V3", "PWR_LED_A", "front")
part("D2", "Device:LED", "XL-1608UBC-04 (power)", "LED_SMD:LED_0603_1608Metric", {"1": "GND", "2": "PWR_LED_A"}, "front")
# decorative: lights the eye artwork on the back, always on while powered (~5 mA; lower R24 for brighter)
R("R24", "100", "+3V3", "EYE_LED_A", "front")
part("D4", "Device:LED", "XL-1608UBC-04 (eye)", "LED_SMD:LED_0603_1608Metric", {"1": "GND", "2": "EYE_LED_A"}, "front")

# ---- mechanical ---------------------------------------------------------------------
for i in range(1, 5):
    part(f"H{i}", "Mechanical:MountingHole", "M3", "MountingHole:MountingHole_3.2mm_M3", {}, "mech")
for i in range(5, 9):
    part(f"H{i}", "Mechanical:MountingHole", "M2 (OLED)", "MountingHole:MountingHole_2.2mm_M2", {}, "mech")

# Nets that need a PWR_FLAG in the schematic (driven only by passives).
PWR_FLAGS = ["GND", "VBUS", "SYS", "BAT_SW"]

# GPIO contract with the firmware: (#define name, GPIO, net)
FIRMWARE_PINS = {"BTN_EJECT_PIN": "BTN_EJECT", "BTN_HOME_PIN": "BTN_HOME", "BTN_PLAYPAUSE_PIN": "BTN_PLAY",
                 "ENC_CLK_PIN": "ENC_A", "ENC_DT_PIN": "ENC_B", "ENC_SW_PIN": "ENC_SW"}
ESP32_GPIO_PIN = {"IO4": "26", "IO13": "16", "IO14": "13", "IO15": "23", "IO21": "33", "IO22": "36", "IO32": "8", "IO33": "9"}


def nets():
    out = {}
    for ref, (_, _, _, pins, _) in PARTS.items():
        for pin, net in pins.items():
            out.setdefault(net, []).append((ref, pin))
    return out


if __name__ == "__main__":
    import re
    from pathlib import Path
    n = nets()
    # every net needs at least two connections
    lonely = {k: v for k, v in n.items() if len(v) < 2}
    assert not lonely, lonely
    # firmware pin contract
    ino = (Path(__file__).resolve().parents[2] / "arduino/DiscStation/DiscStation.ino").read_text()
    u1 = PARTS["U1"][3]
    for define, net in FIRMWARE_PINS.items():
        gpio = int(re.search(rf"#define {define}\s+(\d+)", ino).group(1))
        assert u1[ESP32_GPIO_PIN[f"IO{gpio}"]] == net, (define, gpio, net)
    assert u1[ESP32_GPIO_PIN["IO21"]] == "SDA" and u1[ESP32_GPIO_PIN["IO22"]] == "SCL"
    print(f"{len(PARTS)} parts, {len(n)} nets; firmware GPIO contract OK")
