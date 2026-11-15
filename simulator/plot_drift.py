#!/usr/bin/env python3
"""Grafico rapido de la deriva S1 de EX-017 (criterio de listo de T06).

    python simulator/plot_drift.py            -> simulator/out/ex017_s1.png
Simula 20 lotes en vivo (20 minutos reales = 100 de operacion con TIME_WARP=5) con S1 encendido en el lote 2.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fpsim.live import build_batch  # noqa: E402
from fpsim.scenarios import parse_active  # noqa: E402


def series(start: datetime, minutes: int = 20, s1_at: int = 2):
    since = start + timedelta(minutes=s1_at)
    rows = []
    for k in range(minutes):
        now = start + timedelta(minutes=k + 1)
        active = parse_active({"S1": {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}}) if now > since else {}
        rows += build_batch(now, active, machine_ids={"EX-017"})["readings"]
    return rows


def main() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    start = datetime(2026, 11, 15, 19, 30, tzinfo=timezone.utc)  # 14:30 en Lima, turno pleno
    rows = [r for r in series(start) if r["engine_status"] != "off"]
    x = [(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) - start).total_seconds() / 60 for r in rows]
    fig, axes = plt.subplots(3, 1, figsize=(9, 7), sharex=True)
    for ax, key, label in zip(axes, ["hydraulic_pressure_bar", "hydraulic_oil_temp_c", "vibration_mm_s"],
                              ["Presion hidraulica (bar)", "Aceite hidraulico (C)", "Vibracion RMS (mm/s)"]):
        ax.plot(x, [r[key] for r in rows], lw=1)
        ax.set_ylabel(label)
        ax.axvline(2, color="gray", ls=":", lw=1)
        ax.axvline(10, color="tab:orange", ls="--", lw=1, label="programar (~lote 8)")
        ax.axvline(17, color="tab:red", ls="--", lw=1, label="inmediato (~lote 15)")
        ax.grid(alpha=0.3)
    axes[0].legend(loc="lower left", fontsize=8)
    axes[0].set_title("EX-017 - escenario S1 (bomba hidraulica gastada), encendido en el minuto 2")
    axes[-1].set_xlabel("minutos reales (TIME_WARP=5)")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "ex017_s1.png")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    print(path)


if __name__ == "__main__":
    main()
