# Business Case

*All numbers below come from a synthetic, deterministic model (seed 42). They demonstrate method and
relative gains, not any real facility. Figures labelled "estimate" are illustrative.*

## Situation

A distribution centre's economics are dominated by three levers: how full outbound containers are
(freight and handling cost), how far pickers walk (labour cost and throughput ceiling), and how
quickly orders clear (service level). Legacy operations tend to leave all three on the table:
alphabetical or "wherever it fits" slotting, shipping cartons one-to-a-container, and pickers walking
one order at a time.

## Quantified problem (from the model)

Running a synthetic 60-SKU facility with a ~100-order shift through the **legacy** regime:

- Container fill rate sits at **2.0%** - effectively one carton per pallet cage - needing **60
  containers** for the carton set.
- Demand-weighted pick travel is at its **baseline** (fast movers scattered arbitrarily).
- Mean order cycle time is **662.7 s**, with the single picker running near capacity across the
  shift.

## Solution

Three well-understood levers, modelled explicitly:

1. **Consolidate** cartons with a 3D First-Fit-Decreasing bin-packing heuristic (respecting weight
   and dimension limits).
2. **Slot by velocity** - solve the SKU-to-slot assignment as a linear assignment problem so
   A-movers land nearest dispatch.
3. **Batch picks** - one consolidated route per batch instead of a round trip per line.

## Results (modern vs legacy, from the model)

- Container fill rate **2.0% -> 30.2%**, containers **60 -> 4** (**56 fewer**).
- Pick travel **-44.2%** (slotting model).
- Order cycle time **662.7 s -> 158.0 s** (**-76.2%**); simulated picker travel **-67.2%**.
- Heuristic quality check: FFD-1D matched the CP-SAT optimum (6 bins, **0% gap**) on the checked
  small instance.

## ROI (labelled estimates)

These translate the model's physical savings into money using **illustrative** rates - swap in real
ones before quoting.

- *Estimate:* if a container/pallet shipped costs ~EUR 15 in freight+handling, cutting 56 containers
  on a comparable batch is on the order of **EUR 840 per batch** - directional only.
- *Estimate:* a 44-66% cut in picker travel converts, at a loaded labour rate of ~EUR 25/h, into
  meaningful recovered picker-hours per shift; the exact figure depends on volume.
- The re-slotting itself is cheap to justify: the model's plan of **36 moves (39 executable steps
  including staging) breaks even in ~0.4 days** of saved picker time, at the stated assumptions of
  120 s per step and a 1.2 m/s picker.

## Stakeholders

- **Operations / shift managers** - throughput and cycle time.
- **Logistics / transport** - container fill and freight cost.
- **Finance** - ROI and payback on a re-slotting or automation project.
- **IT / data** - the model and API that could sit on top of a real WMS feed.

## Deliverable

A multi-page executive **PDF** and a multi-sheet **Excel** workbook
(`python -m logitwin --deliverables`), plus an offline **web dashboard** (`python app.py`) for
interactive exploration of the layout and per-carton recommendations.
