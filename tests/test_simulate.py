from logitwin.data import make_cartons, make_order_stream, make_warehouse
from logitwin.simulate import simulate, simulation_report


def _fixture():
    cartons = make_cartons()
    wh = make_warehouse(n_slots=len(cartons))
    orders = make_order_stream(cartons)
    return cartons, wh, orders


def test_simulation_is_deterministic():
    cartons, wh, orders = _fixture()
    a = simulate("modern", cartons, wh, orders)
    b = simulate("modern", cartons, wh, orders)
    assert a.mean_cycle_time_s == b.mean_cycle_time_s
    assert a.total_travel_m == b.total_travel_m
    assert a.orders_completed == b.orders_completed


def test_all_orders_completed_in_both_regimes():
    cartons, wh, orders = _fixture()
    for regime in ("legacy", "modern"):
        res = simulate(regime, cartons, wh, orders)
        assert res.orders_completed == len(orders)


def test_modern_beats_legacy_on_cycle_time_and_travel():
    cartons, wh, orders = _fixture()
    legacy = simulate("legacy", cartons, wh, orders)
    modern = simulate("modern", cartons, wh, orders)
    assert modern.mean_cycle_time_s < legacy.mean_cycle_time_s
    assert modern.total_travel_m < legacy.total_travel_m
    assert modern.container_utilization > legacy.container_utilization


def test_simulation_report_deltas_positive():
    cartons, wh, orders = _fixture()
    rep = simulation_report(cartons, wh, orders)
    assert rep["cycle_time_reduction_pct"] > 0
    assert rep["travel_reduction_pct"] > 0
