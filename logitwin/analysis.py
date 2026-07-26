"""Aggregate the packing, slotting, and simulation reports into one deterministic bundle."""

from __future__ import annotations

from . import SEED
from .data import dataset
from .packing import packing_report
from .simulate import simulation_report
from .slotting import slotting_report


def full_report(seed: int = SEED, n: int = 60) -> dict:
    """Run every analysis on one seeded dataset and return a single dict."""
    ds = dataset(seed=seed, n=n)
    packing = packing_report(ds["cartons"], ds["container"])
    slotting = slotting_report(ds["cartons"], ds["warehouse"])
    simulation = simulation_report(ds["cartons"], ds["warehouse"], ds["orders"])
    return {
        "seed": seed,
        "n_cartons": len(ds["cartons"]),
        "n_slots": ds["warehouse"].n_slots,
        "n_orders": len(ds["orders"]),
        "dataset": ds,
        "packing": packing,
        "slotting": slotting,
        "simulation": simulation,
    }


def headline_numbers(report: dict | None = None) -> dict:
    """Small flat dict of the marketing-safe headline figures for the UI and README."""
    r = report or full_report()
    p = r["packing"]
    s = r["slotting"]
    sim = r["simulation"]
    return {
        "naive_fill_pct": round(100.0 * p["naive_fill"], 1),
        "ffd_fill_pct": round(100.0 * p["ffd_fill"], 1),
        "containers_saved": p["containers_saved"],
        "naive_containers": p["naive_containers"],
        "ffd_containers": p["ffd_containers"],
        "cpsat_gap_pct": round(p["cpsat_gap_pct"], 1),
        "travel_reduction_pct": round(s["reduction_pct"], 1),
        "break_even_days": round(s["break_even_days"], 1),
        "n_moves": s["n_moves"],
        "n_steps": s["n_steps"],
        "n_cycles": s["n_cycles"],
        "cycle_time_reduction_pct": round(sim["cycle_time_reduction_pct"], 1),
        "sim_travel_reduction_pct": round(sim["travel_reduction_pct"], 1),
        "legacy_cycle_s": round(sim["legacy"].mean_cycle_time_s, 1),
        "modern_cycle_s": round(sim["modern"].mean_cycle_time_s, 1),
        "orders_completed": sim["modern"].orders_completed,
    }
