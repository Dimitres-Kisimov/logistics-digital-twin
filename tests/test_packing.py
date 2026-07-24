from logitwin.data import Container, make_cartons
from logitwin.packing import (
    cpsat_min_bins,
    ffd_min_bins_1d,
    ffd_pack,
    naive_pack,
    packing_report,
)


def test_ffd_beats_naive_fill_rate():
    cartons = make_cartons()
    naive = naive_pack(cartons)
    ffd = ffd_pack(cartons)
    assert ffd.fill_rate > naive.fill_rate
    assert ffd.n_containers < naive.n_containers


def test_ffd_respects_weight_and_dimension_limits():
    cartons = make_cartons()
    container = Container()
    ffd = ffd_pack(cartons, container)
    for c in ffd.containers:
        assert c.used_weight <= container.max_weight + 1e-6
        for p in c.placements:
            assert p.x + p.length <= container.length + 1e-6
            assert p.y + p.width <= container.width + 1e-6
            assert p.z + p.height <= container.height + 1e-6


def test_all_cartons_packed_or_accounted():
    cartons = make_cartons()
    ffd = ffd_pack(cartons)
    # Every carton that fits a container should be placed exactly once.
    placed_ids = [p.carton_id for c in ffd.containers for p in c.placements]
    assert len(placed_ids) == len(set(placed_ids))
    assert len(placed_ids) == len(cartons)


def test_cpsat_count_not_greater_than_heuristic():
    cartons = make_cartons()
    small = sorted(cartons, key=lambda c: c.volume, reverse=True)[:14]
    vols = [c.volume for c in small]
    cap = Container(length=60, width=40, height=40).volume
    cpsat = cpsat_min_bins(vols, cap)
    ffd = ffd_min_bins_1d(vols, cap)
    assert cpsat >= 1
    assert cpsat <= ffd  # optimal never uses more bins than the heuristic


def test_packing_report_shape():
    rep = packing_report(make_cartons())
    assert rep["cpsat_small_bins"] <= rep["ffd_small_bins"]
    assert rep["containers_saved"] >= 0
    assert 0.0 <= rep["ffd_fill"] <= 1.0
