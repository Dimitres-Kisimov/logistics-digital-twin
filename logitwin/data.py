"""Deterministic synthetic data generators.

Every generator is seeded so a given seed always yields byte-identical output. There is no
real-world data anywhere in this project - cartons, the warehouse layout, and the order stream
are all fabricated by the functions below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import SEED

# Velocity (ABC) classes: A = fast movers, C = slow movers.
VELOCITY_CLASSES = ("A", "B", "C")
# Relative daily demand weight used by the slotting objective (picks/day per SKU in that class).
VELOCITY_DEMAND = {"A": 40.0, "B": 12.0, "C": 3.0}


@dataclass(frozen=True)
class Carton:
    """A single carton / product unit to be stored and picked."""

    id: int
    sku: str
    length: float  # cm
    width: float  # cm
    height: float  # cm
    weight: float  # kg
    velocity: str  # A / B / C

    @property
    def volume(self) -> float:
        """Carton volume in cm^3."""
        return self.length * self.width * self.height


@dataclass(frozen=True)
class Container:
    """A shipping container / pallet cage that cartons are packed into."""

    length: float = 120.0  # cm (EUR-pallet footprint-ish)
    width: float = 80.0
    height: float = 100.0
    max_weight: float = 300.0  # kg

    @property
    def volume(self) -> float:
        return self.length * self.width * self.height


@dataclass(frozen=True)
class Slot:
    """A storage location in a rack bay."""

    id: int
    aisle: int
    bay: int
    level: int
    slot_l: float  # cm capacity
    slot_w: float
    slot_h: float
    distance: float  # metres of pick travel from dispatch to this slot and back proxy


@dataclass
class Warehouse:
    """A rack layout: a list of slots with per-slot dispatch distance."""

    slots: list[Slot] = field(default_factory=list)

    @property
    def n_slots(self) -> int:
        return len(self.slots)


@dataclass(frozen=True)
class Order:
    """A customer order arriving during the shift."""

    id: int
    arrival: float  # seconds since shift start
    skus: tuple[str, ...]  # SKUs requested (one unit each)


def make_cartons(n: int = 60, seed: int = SEED) -> list[Carton]:
    """Generate ``n`` deterministic cartons with SKUs and ABC velocity classes."""
    rng = np.random.default_rng(seed)
    cartons: list[Carton] = []
    # ABC split: 20% A, 30% B, 50% C (classic Pareto-ish slotting assumption).
    for i in range(n):
        r = rng.random()
        if r < 0.20:
            vel = "A"
        elif r < 0.50:
            vel = "B"
        else:
            vel = "C"
        length = float(round(rng.uniform(15, 55), 1))
        width = float(round(rng.uniform(12, 40), 1))
        height = float(round(rng.uniform(10, 35), 1))
        weight = float(round(rng.uniform(0.8, 18.0), 2))
        sku = f"SKU-{i:04d}"
        cartons.append(
            Carton(
                id=i,
                sku=sku,
                length=length,
                width=width,
                height=height,
                weight=weight,
                velocity=vel,
            )
        )
    return cartons


def make_warehouse(n_slots: int = 60, seed: int = SEED) -> Warehouse:
    """Generate a rack layout of ``n_slots`` slots with a dispatch distance for each.

    Distance grows with aisle index and level height (higher levels cost more travel via
    reach-truck cycle time), so there is a real ordering the slotting optimizer can exploit.
    """
    rng = np.random.default_rng(seed + 1)
    slots: list[Slot] = []
    aisles = 6
    per_aisle = int(np.ceil(n_slots / aisles))
    sid = 0
    for aisle in range(aisles):
        for pos in range(per_aisle):
            if sid >= n_slots:
                break
            bay = pos // 3
            level = pos % 3
            # Base distance: aisle depth + bay depth (metres, round trip proxy).
            base = 6.0 + aisle * 9.0 + bay * 2.5 + level * 1.5
            jitter = float(rng.uniform(-0.5, 0.5))
            slots.append(
                Slot(
                    id=sid,
                    aisle=aisle,
                    bay=bay,
                    level=level,
                    slot_l=60.0,
                    slot_w=45.0,
                    slot_h=40.0,
                    distance=round(base + jitter, 2),
                )
            )
            sid += 1
    return Warehouse(slots=slots)


def make_order_stream(
    cartons: list[Carton],
    shift_seconds: int = 8 * 3600,
    mean_interarrival: float = 260.0,
    lines_lambda: float = 2.2,
    seed: int = SEED,
) -> list[Order]:
    """Generate a Poisson-ish order stream over a shift.

    Orders arrive with exponential inter-arrival times; each order requests a small number of
    SKUs biased toward fast movers (A picked most often), which is what makes good slotting pay off.
    """
    rng = np.random.default_rng(seed + 2)
    skus = [c.sku for c in cartons]
    # Pick probability weighted by velocity class demand so A-movers dominate the pick stream.
    weights = np.array([VELOCITY_DEMAND[c.velocity] for c in cartons], dtype=float)
    weights = weights / weights.sum()

    orders: list[Order] = []
    t = 0.0
    oid = 0
    while True:
        t += float(rng.exponential(mean_interarrival))
        if t > shift_seconds:
            break
        n_lines = 1 + int(rng.poisson(lines_lambda))
        n_lines = min(n_lines, 6)
        chosen = rng.choice(len(skus), size=n_lines, replace=False, p=weights)
        order_skus = tuple(skus[j] for j in chosen)
        orders.append(Order(id=oid, arrival=round(t, 2), skus=order_skus))
        oid += 1
    return orders


def dataset(seed: int = SEED, n: int = 60) -> dict:
    """Bundle a full deterministic dataset used across the package."""
    cartons = make_cartons(n=n, seed=seed)
    warehouse = make_warehouse(n_slots=n, seed=seed)
    orders = make_order_stream(cartons, seed=seed)
    return {
        "cartons": cartons,
        "warehouse": warehouse,
        "orders": orders,
        "container": Container(),
    }
