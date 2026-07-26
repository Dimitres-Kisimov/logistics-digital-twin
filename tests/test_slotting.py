from logitwin.data import VELOCITY_DEMAND, make_cartons, make_warehouse
from logitwin.slotting import (
    apply_moves,
    execution_sequence,
    legacy_slotting,
    optimize_slotting,
    reshuffle_plan,
    slotting_report,
)


def _fixture():
    cartons = make_cartons()
    warehouse = make_warehouse(n_slots=len(cartons))
    return cartons, warehouse


def test_optimized_travel_not_worse_than_legacy():
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh)
    assert opt.total_travel <= legacy.total_travel
    assert opt.total_travel < legacy.total_travel  # strictly better on this dataset


def test_assignment_is_a_valid_permutation():
    cartons, wh = _fixture()
    opt = optimize_slotting(cartons, wh)
    skus = {c.sku for c in cartons}
    assigned_skus = set(opt.assignment.keys())
    assigned_slots = list(opt.assignment.values())
    assert assigned_skus == skus
    # No slot used twice (valid one-to-one assignment).
    assert len(assigned_slots) == len(set(assigned_slots))


def test_reshuffle_plan_transforms_current_to_target():
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh)
    moves = reshuffle_plan(legacy.assignment, opt.assignment)
    reached = apply_moves(legacy.assignment, moves)
    assert reached == opt.assignment


def test_reshuffle_touches_only_misplaced_skus():
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh)
    moves = reshuffle_plan(legacy.assignment, opt.assignment)
    for m in moves:
        assert legacy.assignment[m.sku] != opt.assignment[m.sku]
        assert m.to_slot == opt.assignment[m.sku]


def test_slotting_report_break_even_and_validity():
    cartons, wh = _fixture()
    rep = slotting_report(cartons, wh)
    assert rep["plan_valid"] is True
    assert rep["sequence_valid"] is True
    assert rep["reduction_pct"] > 0
    assert rep["break_even_days"] >= 0


def test_prefer_tiebreak_keeps_exact_optimum_with_fewer_moves():
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    unbiased = optimize_slotting(cartons, wh)
    biased = optimize_slotting(cartons, wh, prefer=legacy.assignment)
    # Never trades travel for fewer moves: the biased layout is exactly as good.
    assert abs(biased.total_travel - unbiased.total_travel) < 1e-6
    # But it strictly reduces the number of SKUs that have to move (equal-demand SKUs shuffled
    # among equal-distance slots stay put).
    biased_moves = reshuffle_plan(legacy.assignment, biased.assignment)
    unbiased_moves = reshuffle_plan(legacy.assignment, unbiased.assignment)
    assert len(biased_moves) < len(unbiased_moves)
    assert len(biased_moves) < len(cartons)  # no longer a full re-slot


def test_execution_sequence_every_step_lands_in_an_empty_spot():
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh, prefer=legacy.assignment)
    seq = execution_sequence(legacy.assignment, opt.assignment, cartons, wh)
    moves = reshuffle_plan(legacy.assignment, opt.assignment)

    # k moves per cycle become k + 1 steps; the extras are exactly one staging park per cycle.
    n_cycles = max(st.cycle for st in seq)
    assert len(seq) == len(moves) + n_cycles

    # Replay the sequence: sources hold the right SKU, targets are empty, staging holds <= 1.
    occupant = {slot: sku for sku, slot in legacy.assignment.items()}
    staged = None
    for st in seq:
        if st.from_slot is None:
            assert staged == st.sku
            staged = None
        else:
            assert occupant.pop(st.from_slot, None) == st.sku
        if st.to_slot is None:
            assert staged is None  # the staging position never holds more than one carton
            staged = st.sku
        else:
            assert st.to_slot not in occupant
            occupant[st.to_slot] = st.sku
    assert staged is None
    assert {sku: slot for slot, sku in occupant.items()} == opt.assignment


def test_execution_sequence_orders_cycles_best_savings_first_and_deterministic():
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh, prefer=legacy.assignment)
    seq = execution_sequence(legacy.assignment, opt.assignment, cartons, wh)
    seq2 = execution_sequence(legacy.assignment, opt.assignment, cartons, wh)
    assert seq == seq2  # deterministic: same inputs, identical sequence

    # Per-step savings add up exactly to the total travel saved.
    total_saving = sum(st.saving for st in seq)
    assert abs(total_saving - (legacy.total_travel - opt.total_travel)) < 1e-6

    # After the tie-break every remaining cycle nets a strictly positive saving, and cycles are
    # ordered by saving-per-step descending (bang-for-buck).
    per_cycle: dict[int, list] = {}
    for st in seq:
        per_cycle.setdefault(st.cycle, []).append(st)
    rates = []
    for cycle_id in sorted(per_cycle):
        steps = per_cycle[cycle_id]
        net = sum(st.saving for st in steps)
        assert net > 0
        rates.append(net / len(steps))
    assert rates == sorted(rates, reverse=True)

    # Savings are demand-weighted metres: spot-check one landing step.
    dist = {sl.id: sl.distance for sl in wh.slots}
    demand = {c.sku: VELOCITY_DEMAND[c.velocity] for c in cartons}
    landing = next(st for st in seq if st.from_slot is not None and st.to_slot is not None)
    expected = demand[landing.sku] * (dist[landing.from_slot] - dist[landing.to_slot])
    assert abs(landing.saving - expected) < 1e-9
