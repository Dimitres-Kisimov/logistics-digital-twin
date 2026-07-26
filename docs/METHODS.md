# Methods

This document sets out the legacy-vs-modern taxonomy the model encodes, the algorithms used, and
their honest limitations. All data is synthetic and deterministic.

## The legacy-vs-modern taxonomy

| Lever | Legacy regime | Modern regime |
| --- | --- | --- |
| Slotting | alphabetical / arbitrary | velocity-based (ABC), travel-minimising assignment |
| Packing | one carton per container | 3D FFD consolidation with weight/dim limits |
| Picking | single picker, one order at a time, round trip per line | batched routes, consolidated travel |

## 1. Bin-packing (`logitwin/packing.py`)

**First-Fit-Decreasing (FFD) by volume** with a shelf/layer placement rule: cartons are sorted by
volume descending and placed into the first container/shelf where they fit, tracking remaining
footprint per row and locking each shelf's height to its first carton so the container height is
never exceeded. Weight and all three dimensions are respected.

Bin packing is **NP-hard**, so FFD is a heuristic. Classic results bound FFD for the 1D case at
`FFD(I) <= (11/9) OPT(I) + 6/9` (Dósa's tight bound), i.e. within ~22% of optimal in the worst case;
in practice it is usually much closer. To make the gap concrete rather than theoretical, I solve a
small instance to optimality with **CP-SAT** (OR-Tools) on the identical 1D bin-count problem and
compare: on the checked instance FFD-1D and CP-SAT both use 6 bins (**0% gap**). This is a
demonstration on a small case, not a general optimality claim.

## 2. Slotting (`logitwin/slotting.py`)

Slotting is posed as: assign each SKU to a slot to minimise `sum(demand[sku] * distance[slot])`. With
one SKU per slot and a square cost matrix `cost[i,j] = demand_i * distance_j`, this is a **balanced
linear assignment problem**, solved **exactly** by the Hungarian algorithm via
`scipy.optimize.linear_sum_assignment`. The result is optimal *for this model*; real slotting adds
slot-capacity, product-family, correlation, and congestion constraints that this abstraction omits.

Because demand comes in only three velocity classes, many layouts tie for the optimum. Ties are
broken **toward keeping each SKU in its current slot**: a tiny epsilon surcharge on every
off-current cell makes the solver relocate a SKU only when the move buys real travel, and the biased
solve is verified to reach the exact unbiased optimum (it is discarded otherwise). This cuts the
plan from a full re-slot to only the moves that pay.

The re-shuffle planner emits the minimal set of "move SKU from slot X to slot Y" instructions to turn
a current layout into the optimized one (each mis-placed SKU moves exactly once), then orders them
into an **executable sequence**: in a full warehouse the moves decompose into cycles of occupied
slots, so each cycle parks its first carton in an off-rack staging position once, shifts the rest
along the freed chain, and lands the staged carton last - `k` moves become `k + 1` steps, every step
targets an empty spot, and the staging position never holds more than one carton. Cycles run in
bang-for-buck order (net daily saving per step), so a partial execution banks the largest savings
first. The one-off cost prices **every step** (including staging parks, 120 s/step assumed, picker
speed 1.2 m/s) against the daily travel saving to report a **break-even in days**.

## 3. Discrete-event simulation (`logitwin/simulate.py`)

A **hand-rolled** discrete-event simulation using a `heapq` priority queue of `(time, seq, kind,
payload)` events - no SimPy. A single picker is the constrained resource; orders arrive over the
shift, queue in a backlog, and are served either one-at-a-time (legacy) or in consolidated batches
(modern). It is fully deterministic given the seed. The headline cycle-time reduction is amplified by
a **queueing effect** - the legacy configuration runs near capacity, so wait times grow - which is a
legitimate operational finding, not an artefact.

## Selected references

1. Dósa, G. (2007). *The Tight Bound of First Fit Decreasing Bin-Packing Algorithm Is
   FFD(I) <= 11/9 OPT(I) + 6/9.* ESCAPE 2007, LNCS 4614. (FFD worst-case bound.)
2. Kuhn, H. W. (1955). *The Hungarian Method for the Assignment Problem.* Naval Research Logistics
   Quarterly, 2(1-2), 83-97. (Linear assignment, used for slotting.)
3. de Koster, R., Le-Duc, T., & Roodbergen, K. J. (2007). *Design and control of warehouse order
   picking: A literature review.* European Journal of Operational Research, 182(2), 481-501.
   (Order-picking and batching taxonomy.)
4. Petersen, C. G., & Aase, G. (2004). *A comparison of picking, storage, and routing policies in
   manual order picking.* International Journal of Production Economics, 92(1), 11-19.
   (Velocity-based / class-based slotting benefits.)
5. Banks, J., Carson, J. S., Nelson, B. L., & Nicol, D. M. (2010). *Discrete-Event System
   Simulation* (5th ed.). Prentice Hall. (Event-scheduling / DES methodology.)
