from pathlib import Path

from logitwin.data import make_cartons, make_warehouse
from logitwin.render import (
    canonical_figures,
    slotting_comparison_svg,
    warehouse_layout_svg,
)
from logitwin.sensitivity import golden_zone_occupancy_pct
from logitwin.slotting import legacy_slotting, optimize_slotting

DOCS_IMG = Path(__file__).resolve().parent.parent / "docs" / "img"
FIGURE_STEMS = ("warehouse_layout", "slotting_before_after")


def _fixture():
    cartons = make_cartons()
    wh = make_warehouse(n_slots=len(cartons))
    return cartons, wh


def test_canonical_figures_are_deterministic():
    """Byte-identical SVG across re-runs - no wall-clock / RNG leaks into a committed artefact."""
    a = canonical_figures()
    b = canonical_figures()
    assert a == b
    assert set(a) == set(FIGURE_STEMS)
    for svg in a.values():
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
        for token in ("2026", "random", "NaN", "Infinity"):
            assert token not in svg


def test_committed_docs_figures_are_in_sync_with_the_generator():
    """The README-referenced docs/img SVGs must equal freshly generated output byte-for-byte, so
    they can never silently drift from the seeded engine. Regenerate via `python -m logitwin.render`."""
    figs = canonical_figures()
    for stem in FIGURE_STEMS:
        committed = (DOCS_IMG / f"{stem}.svg").read_text(encoding="utf-8")
        assert committed == figs[stem], f"{stem}.svg is stale - re-run `python -m logitwin.render`"


def test_before_after_figure_pins_the_seeded_engine_numbers():
    """The figure must show the exact numbers the engine computes (honest, not decorative)."""
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh, prefer=legacy.assignment)
    reduction = 100.0 * (legacy.total_travel - opt.total_travel) / legacy.total_travel
    occ_before = golden_zone_occupancy_pct(legacy.assignment, cartons, wh)
    occ_after = golden_zone_occupancy_pct(opt.assignment, cartons, wh)

    # Sanity: these are the figures quoted in the README (25% -> 100%, -44.2%).
    assert round(reduction, 1) == 44.2
    assert (round(occ_before), round(occ_after)) == (25, 100)

    svg = slotting_comparison_svg(cartons, wh)
    assert f"{occ_before:.0f}% &#8594; {occ_after:.0f}%" in svg
    assert f"&#8722;{reduction:.1f}%" in svg


def test_layout_figure_colours_the_golden_zone_a_movers():
    """In the optimized layout the golden-zone cells are A-class (teal) - the figure's whole point."""
    cartons, wh = _fixture()
    legacy = legacy_slotting(cartons, wh)
    opt = optimize_slotting(cartons, wh, prefer=legacy.assignment)
    svg = warehouse_layout_svg(opt.assignment, cartons, wh)
    # Golden-zone occupancy is 100% A after optimizing, so the teal fill must appear in the figure.
    assert 'fill="#2a9d8f"' in svg  # A-class fill (matches the app's VEL_COLOR)
    assert 'stroke="#b8860b"' in svg  # golden-zone ring
