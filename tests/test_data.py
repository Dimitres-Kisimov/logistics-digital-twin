from logitwin.data import (
    make_cartons,
    make_order_stream,
    make_warehouse,
)


def test_data_is_deterministic():
    a = make_cartons()
    b = make_cartons()
    assert [c.id for c in a] == [c.id for c in b]
    assert [c.volume for c in a] == [c.volume for c in b]


def test_velocity_classes_present():
    cartons = make_cartons()
    classes = {c.velocity for c in cartons}
    assert classes <= {"A", "B", "C"}
    assert "A" in classes and "C" in classes


def test_warehouse_slots_have_positive_distance():
    wh = make_warehouse(n_slots=60)
    assert wh.n_slots == 60
    assert all(s.distance > 0 for s in wh.slots)


def test_order_stream_within_shift_and_deterministic():
    cartons = make_cartons()
    o1 = make_order_stream(cartons)
    o2 = make_order_stream(cartons)
    assert len(o1) == len(o2) and len(o1) > 0
    assert all(0 <= o.arrival <= 8 * 3600 for o in o1)
    assert [o.skus for o in o1] == [o.skus for o in o2]
