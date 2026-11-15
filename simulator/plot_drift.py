#!/usr/bin/env python3
"""Graficos rapidos de los escenarios en vivo (criterio de listo de T06 y T27).

    python simulator/plot_drift.py                 -> simulator/out/ex017_s1.png  (S1, bomba de EX-017)
    python simulator/plot_drift.py --scenario S7   -> simulator/out/vq022_s7.png  (S7, junta de culata y resonancia)
Simula 20-30 lotes en vivo (minutos reales; TIME_WARP=5) con el escenario encendido en el lote 2.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fpsim import oem  # noqa: E402
from fpsim.live import build_batch  # noqa: E402
from fpsim.scenarios import S7_MACHINE, parse_active  # noqa: E402

START = datetime(2026, 11, 15, 19, 30, tzinfo=timezone.utc)  # 14:30 en Lima, turno pleno


def series(scenario: str, machine: str, minutes: int, on_at: int = 2):
    since = START + timedelta(minutes=on_at)
    rows = []
    for k in range(minutes):
        now = START + timedelta(minutes=k + 1)
        active = parse_active({scenario: {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}}) if now > since else {}
        rows += build_batch(now, active, machine_ids={machine})["readings"]
    return [r for r in rows if r["engine_status"] == "operating"]


def plot(panels, rows, title, path, marks=()):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = [(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) - START).total_seconds() / 60 for r in rows]
    fig, axes = plt.subplots(len(panels), 1, figsize=(9, 2.4 * len(panels)), sharex=True)
    for ax, (label, fn, hline) in zip(axes, panels):
        ax.plot(x, [fn(r) for r in rows], lw=1)
        ax.set_ylabel(label)
        ax.axvline(2, color="gray", ls=":", lw=1)
        for m, lbl, color in marks:
            ax.axvline(m, color=color, ls="--", lw=1, label=lbl)
        if hline:
            ax.axhline(hline[0], color="tab:red", ls="--", lw=1, label=hline[1])
            ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.3)
    axes[0].set_title(title)
    axes[-1].set_xlabel("minutos reales (TIME_WARP=5)")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    print(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=["S1", "S7"], default="S1")
    a = ap.parse_args()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    if a.scenario == "S1":
        rows = series("S1", "EX-017", 20)
        plot([("Presion hidraulica (bar)", lambda r: r["hydraulic_pressure_bar"], None),
              ("Aceite hidraulico (C)", lambda r: r["hydraulic_oil_temp_c"], None),
              ("Vibracion RMS (mm/s)", lambda r: r["vibration_mm_s"], None)],
             rows, "EX-017 - S1 (bomba hidraulica gastada), encendido en el minuto 2", os.path.join(out, "ex017_s1.png"),
             marks=[(10, "programar (~lote 8)", "tab:orange"), (17, "inmediato (~lote 15)", "tab:red")])
    else:
        spec = oem.model("VQ")
        mode = next(f for f in spec["vibration"]["natural_frequencies"] if f["hz"] == 62)
        rows = series("S7", S7_MACHINE, 30)
        plot([("Refrigerante (C)", lambda r: r["coolant_temp_c"], (spec["limits"]["coolant_temp_c"]["warning"], "advertencia del fabricante")),
              ("rpm", lambda r: r["engine_rpm"], None),
              ("Frecuencia del 2x (Hz) = 2 x rpm/60", lambda r: 2 * r["engine_rpm"] / 60, (mode["hz"], f"modo {mode['mode']}: {mode['component']} ({mode['hz']} Hz)")),
              ("Vibracion RMS (mm/s)", lambda r: r["vibration_mm_s"], (spec["limits"]["vibration_mm_s"]["normal"][1], "fin del rango normal"))],
             rows, f"{S7_MACHINE} - S7 (junta de culata): las rpm suben y el 2x cruza el modo del chasis",
             os.path.join(out, "vq022_s7.png"))


if __name__ == "__main__":
    main()
