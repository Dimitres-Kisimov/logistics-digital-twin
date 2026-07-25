# Logistics Digital Twin

I built this to answer a very concrete warehouse question with numbers instead of opinions: **how
much does modern practice actually beat legacy practice** on the three things a distribution centre
lives and dies by — how full the outbound containers are, how far pickers walk, and how fast orders
get out the door?

So this is a small "digital twin" of a warehouse. It generates a synthetic facility (cartons with
sizes and ABC velocity classes, a rack layout with a dispatch distance per slot, and an order stream
over an 8-hour shift), then runs the same demand through two operating regimes and measures the gap:

- **Legacy** - arbitrary/alphabetical slotting, one carton per container, a single picker working
  orders one at a time with a round trip to dispatch for every line.
- **Modern** - velocity-based slotting, 3D bin-packing consolidation, and batched picking routes.

> **Everything here is synthetic and deterministic.** There is no real customer or facility data
> anywhere - the numbers come from seeded generators (`seed=42`), so anyone who runs this gets the
> exact figures below. Bin packing and slotting are NP-hard; I use heuristics and report the
> measured optimality gap rather than claiming anything is provably optimal.

## What the run measured

From my seeded run (`python -m logitwin --summary`):

| Metric | Legacy | Modern | Change |
| --- | --- | --- | --- |
| Container fill rate | 2.0% | 30.2% | consolidation from one carton/pallet to FFD |
| Containers used | 60 | 4 | **56 fewer** |
| Pick travel (slotting model) | baseline | optimized | **-44.2%** |
| Order cycle time (simulation) | 662.7 s | 158.2 s | **-76.1%** |
| Picker travel (simulation) | baseline | optimized | **-66.5%** |

Two honesty notes on those figures:

- The 2.0% "before" fill rate is literally one small carton sitting in a 120x80x100 cm pallet cage -
  that is what one-carton-per-container means, and it is exactly why consolidation matters. The
  after figure of 30.2% is a *shelf* heuristic, not a magic number; there is headroom left.
- On the small packing instance I check the heuristic against a CP-SAT exact solve: FFD-1D used **6
  bins and CP-SAT proved 6 is optimal - a 0% gap on that instance**. That is a genuinely good result
  on a small case, not a claim that FFD is optimal in general (it is not).

The re-slotting side also produces an actionable plan: **60 moves** to get from the legacy layout to
the optimized one, breaking even in about **0.7 days** of saved picker time.

## How to run

```bash
pip install -r requirements.txt

# 1) the warehouse-command UI (offline, no CDNs)
python app.py            # -> http://localhost:5000

# 2) the executive deliverables (multi-page PDF + multi-sheet Excel in deliverables/)
python -m logitwin --deliverables

# 3) just the headline numbers on the console
python -m logitwin --summary

# 4) containerised
docker compose up        # serves the app on port 5000 via gunicorn
```

Tests and lint:

```bash
python -m ruff check .
python -m pytest -q
```

> Note on port 5000: some Windows machines already run a service on 5000. If `python app.py` looks
> like it is serving someone else's page, another process holds the port - change the port in the
> `app.run(...)` call or free 5000.

## The UI

A single-page "warehouse command" dashboard, all hand-built and fully offline (inline SVG + vanilla
JS, no external libraries or fonts):

- a **warehouse map** you can flip between the legacy and optimized layout, with slots coloured by
  velocity class (the optimized view visibly pulls the A-movers toward dispatch);
- a **"scan a carton"** panel - give it dimensions, weight, and an SKU and it returns a recommended
  container/placement plus, if that SKU is due to move, the re-slot instruction;
- **KPI tiles** for fill rate, travel reduction, and the simulation's cycle-time / travel deltas;
- a **re-shuffle plan** table.

## Methods (and their honest limits)

- **3D bin-packing** (`logitwin/packing.py`) - First-Fit-Decreasing by volume with a shelf/layer
  placement rule that respects container dimensions and max weight. Bin packing is NP-hard, so this
  is a heuristic. I keep a CP-SAT exact solver (`ortools` CP-SAT) for a small 1D bin-count instance
  purely to measure how far FFD is from optimal on that case (0% on the checked instance; not a
  general guarantee).
- **Slotting** (`logitwin/slotting.py`) - assigning SKUs to slots to minimise demand-weighted travel
  is a balanced linear assignment problem, solved exactly with the Hungarian algorithm
  (`scipy.optimize.linear_sum_assignment`). The optimum here is exact *for the one-SKU-per-slot
  model*; real slotting has capacity, family, and congestion constraints this model omits.
- **Discrete-event simulation** (`logitwin/simulate.py`) - hand-rolled with a `heapq` event queue
  (no SimPy). It is deterministic given the seed. The large cycle-time gap is partly a queueing
  effect: the legacy single-picker configuration runs close to capacity, so its wait times amplify
  the per-pick inefficiency - which is itself a real and relevant finding, not a modelling trick.

More detail and citations are in [`docs/METHODS.md`](docs/METHODS.md); the framing for a
non-technical reader is in [`docs/BUSINESS_CASE.md`](docs/BUSINESS_CASE.md).

## Illustrative public case studies

Two well-known operators are sometimes cited as reference points for warehouse modernisation. I
mention them only as **illustrative, public-information** context - I am **not affiliated with either
company, have no internal data, and invent no internal figures**:

- **Würth** - a global distributor of fasteners and MRO supplies whose business depends heavily on
  fast, accurate small-parts picking across a very large SKU base; public materials describe ongoing
  logistics-automation investment.
- **Schwarz Group (Lidl / Kaufland)** - a large European retailer whose regional distribution
  centres are a standard public example of high-throughput warehouse operations.

These are context for *why* the levers modelled here (slotting, consolidation, batching) matter at
scale, not claims about those companies' internal numbers.

## Stack

Python 3.14 locally (3.12 in Docker/CI for OR-Tools wheels), NumPy, SciPy, OR-Tools (CP-SAT),
Matplotlib, Flask/Jinja2, openpyxl, pandas. No SimPy - the simulation is a plain `heapq` event loop.

## License

© 2026 Dimitres Kisimov — all rights reserved; published for portfolio review. See LICENSE. All code, data generators, and graphics are original - see
[`CREDITS.md`](CREDITS.md).
