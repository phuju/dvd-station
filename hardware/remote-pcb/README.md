# DiscStation Remote PCB (r1)

One 72 × 64 mm, 2-layer board that replaces the module-based remote. It has the
same outline, mounting holes and front-panel layout as the EasyEDA carrier board.
The GPIOs are unchanged, so the firmware and host need no changes. The design notes
are in [`docs/PCB.md`](../../docs/PCB.md).

| File | What it is |
|---|---|
| `discstation-remote-schematic.pdf` | Schematic, for review |
| `discstation-remote-gerbers.zip` | Gerbers + drill files to upload to JLCPCB |
| `fab/discstation-remote-bom.csv` | Bill of materials |
| `fab/discstation-remote-pos.csv` | Pick-and-place positions (top and bottom) |
| `preview-top.png`, `preview-bottom.png` | 3D renders |
| `discstation-remote.kicad_pro/.kicad_sch/.kicad_pcb` | KiCad 10 project |

- **Front:** PLAY, HOME and EJECT Cherry MX-style key switches (3- or 5-pin), the EC11 encoder, a 4-pin OLED
  header with 4 M2 standoff holes, the power and charge LEDs.
- **Back:** ESP32-WROOM-32E (antenna on the right edge, no copper under it),
  CP2102N with DevKit-style auto-program, MCP73831 charger with a power path,
  AP2112K 3.3 V regulator, USB-C and power switch on the bottom edge, JST-PH battery
  connector (bottom-left), RESET and BOOT buttons, and the always-on eye LED (D4,
  XL-1608UBC-04) at (55.6, 58.4) mm. The 22 × 13 mm area around D4 is kept free for the
  eye/logo silkscreen art; import it with KiCad's Image Converter onto B.Silkscreen.
- LEDs are XL-1608UBC-04 (0603 blue): D2 power and D3 charging on the front, D4 eye on the back.

## Editing

From r1 on, the KiCad files are edited by hand in KiCad 10: open
`discstation-remote.kicad_pro`. After editing, rerun DRC and export new Gerbers
from KiCad's Plot dialog, or run `./build.sh fab`, which regenerates the fab files
without touching the design.

**Do not run the full `./build.sh` after hand edits.** It regenerates the schematic
and board from `design.py` and overwrites them. The note in the schematic's title
block ("edit design.py") comes from that generator and no longer applies.

## Regenerating from scratch (generator, as used for r1)

```bash
FREEROUTING=/path/to/freerouting-1.9.0.jar ./build.sh
```

This runs the steps in order and stops at the first failure: firmware pin check →
schematic → ERC → netlist → placement (`gen_pcb.py`) → Freerouting (about 25 minutes)
→ ground pours → DRC with schematic parity → Gerbers, drill, BOM and positions.
`./build.sh fab` reruns only DRC and the fabrication outputs.

## State of r1

- ERC: 0 violations. DRC: 0 errors, 0 unconnected, 0 schematic-parity issues.
- Remaining warnings are silkscreen only (labels near the edge connectors, and labels
  over pads, which the fab house clips). The ESP32 footprint is reported as modified
  on purpose: see below.

## Review before ordering

- **Autorouted.** Check the USB D+/D− pair, the power traces and the ground pours.
- **Antenna keep-out:** Espressif's full keep-out (48 × 21 mm) does not fit on this
  board. Only the area under the antenna is kept clear, which is a common compromise
  on small boards. Wi-Fi range may be somewhat shorter than on the DevKit.
- **Positions come from a screenshot** of the old board (±1 mm). The encoder position
  is a guess, because the old board used a module with a header. Check every control
  against the enclosure, and allow for the USB-C port and power switch on the bottom
  edge (the switch lever sticks out about 0.5 mm).
- **Battery polarity:** pin 1 of J3 is `+`, but JST-PH battery leads are not wired
  the same way by every seller. Check before plugging one in.
- **Keycap clearance:** the keys keep your EasyEDA layout's spacing (about 18.4 mm
  between centres, against the usual 19.05 mm). Standard 1u keycaps (about 18 mm wide)
  leave only ~0.4 mm between them, so check that yours don't rub.
- **Encoder direction:** if turning the knob scrolls the wrong way, swap
  `ENC_CLK_PIN` and `ENC_DT_PIN` in the firmware.
- **The BOM has no LCSC part numbers.** Choose them when you upload to JLCPCB. Have
  the surface-mount parts (back side) assembled and hand-solder the through-hole parts.
