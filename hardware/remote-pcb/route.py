"""Specctra round trip for Freerouting: `route.py export` writes the .dsn, `route.py import`
reads the .ses back, adds the ground pours and saves the board.
Run inside KiCad's Python (flatpak run --command=python3 org.kicad.KiCad route.py ...).
"""
import sys
from pathlib import Path

import pcbnew

HERE = Path(__file__).resolve().parent
PCB = HERE / "discstation-remote.kicad_pcb"
DSN = HERE / "discstation-remote.dsn"
SES = HERE / "discstation-remote.ses"
OX, OY, W, H = 100.0, 100.0, 72.0, 64.0


def ground_pour(board, layer):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNet(board.FindNet("GND"))
    z.SetLocalClearance(pcbnew.FromMM(0.3))
    z.SetMinThickness(pcbnew.FromMM(0.25))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetThermalReliefGap(pcbnew.FromMM(0.3))
    z.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.4))
    ol = z.Outline()
    ol.NewOutline()
    for x, y in ((0, 0), (W, 0), (W, H), (0, H)):
        ol.Append(pcbnew.VECTOR2I(pcbnew.FromMM(OX + x), pcbnew.FromMM(OY + y)))
    board.Add(z)


def main(cmd):
    board = pcbnew.LoadBoard(str(PCB))
    if cmd == "export":
        assert pcbnew.ExportSpecctraDSN(board, str(DSN)), "DSN export failed"
        print("exported", DSN.name)
        return
    assert pcbnew.ImportSpecctraSES(board, str(SES)), "SES import failed"
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        ground_pour(board, layer)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(PCB))
    print("routed board saved:", len(board.GetTracks()), "track/via items")


if __name__ == "__main__":
    main(sys.argv[1])
