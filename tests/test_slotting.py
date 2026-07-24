from logitwin.data import make_cartons, make_warehouse
from logitwin.slotting import (
    apply_moves,
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
    assert rep["reduction_pct"] > 0
    assert rep["break_even_days"] >= 0
