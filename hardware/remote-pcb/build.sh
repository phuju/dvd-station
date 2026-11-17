#!/usr/bin/env bash
# Regenerate everything from design.py: schematic -> ERC -> netlist -> placed PCB ->
# Freerouting -> pours -> DRC (with schematic parity) -> fabrication files.
# Needs: KiCad Flatpak (org.kicad.KiCad), Java 17+, Freerouting 1.9.0 jar (FREEROUTING=path).
set -euo pipefail
cd "$(dirname "$0")"
KI="flatpak run --command=kicad-cli org.kicad.KiCad"
PY="flatpak run --command=python3 org.kicad.KiCad"
[ "${1:-}" = fab ] || FREEROUTING="${FREEROUTING:?set FREEROUTING to freerouting-1.9.0.jar}"
N=discstation-remote

# `./build.sh fab` skips schematic, placement and routing: just DRC + fabrication files
if [ "${1:-}" != fab ]; then
python3 design.py
python3 gen_sch.py
$KI sch erc --severity-all --exit-code-violations -o erc.rpt $N.kicad_sch
$KI sch export pdf -o $N-schematic.pdf $N.kicad_sch
$KI sch export netlist -o $N.net $N.kicad_sch
$PY gen_pcb.py
# Freerouting 1.9 sometimes stops with a link or two left; a repeat pass starts from the
# routed board and only has to finish those.
for pass in 1 2 3; do
  $PY route.py export
  rm -f $N.ses
  java -jar "$FREEROUTING" -de $N.dsn -do $N.ses -mp 40 -da   # needs a display (1.9.0 has no headless mode)
  $PY route.py import
  $KI pcb drc --format json -o drc.json $N.kicad_pcb >/dev/null
  left=$(python3 -c "import json; print(len(json.load(open('drc.json'))['unconnected_items']))")
  echo "routing pass $pass: $left unconnected"
  [ "$left" = 0 ] && break
done
fi
$KI pcb drc --schematic-parity --severity-error --exit-code-violations --format json -o drc.json $N.kicad_pcb

mkdir -p fab
rm -f fab/*
$KI pcb export gerbers --layers F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts -o fab/ $N.kicad_pcb
$KI pcb export drill --format excellon --excellon-separate-th -o fab/ $N.kicad_pcb
$KI pcb export pos --format csv --units mm --side both -o fab/$N-pos.csv $N.kicad_pcb
$KI sch export bom --fields "Reference,Value,Footprint,\${QUANTITY}" --labels "Designator,Value,Footprint,Qty" \
    --group-by "Value,Footprint" --exclude-dnp -o fab/$N-bom.csv $N.kicad_sch
(cd fab && rm -f ../$N-gerbers.zip && zip -q ../$N-gerbers.zip *.g* *.drl)
echo "done: $N-gerbers.zip, fab/$N-bom.csv, fab/$N-pos.csv"
