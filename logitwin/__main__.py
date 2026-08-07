"""Command-line entry point.

    python -m logitwin --deliverables   # write PDF + Excel to deliverables/
    python -m logitwin --summary        # print headline numbers
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows console safety: force UTF-8 so ASCII markers and any stray unicode never crash stdout.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .analysis import full_report, headline_numbers  # noqa: E402
from .exports import build_excel, build_pdf  # noqa: E402
from .labour import labour_report  # noqa: E402
from .labour import to_csv as labour_to_csv  # noqa: E402
from .labour import to_svg as labour_to_svg  # noqa: E402
from .render import write_figures  # noqa: E402
from .sensitivity import frontier_report, to_csv, to_svg  # noqa: E402


def _write_deliverables(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = full_report()
    pdf = build_pdf(report)
    xlsx = build_excel(report)
    pdf_path = out_dir / "warehouse_digital_twin.pdf"
    xlsx_path = out_dir / "warehouse_digital_twin.xlsx"
    pdf_path.write_bytes(pdf)
    xlsx_path.write_bytes(xlsx)

    # Slotting / pick-travel sensitivity: a small deterministic CSV + hand-drawn SVG frontier the
    # README references. These are legitimately small (well under the 10 KB deliverable gate below),
    # so they are written here but reported separately from the PDF/Excel size check.
    fr = frontier_report()
    csv_path = out_dir / "slotting_sensitivity.csv"
    svg_path = out_dir / "slotting_sensitivity.svg"
    csv_path.write_text(to_csv(fr["rows"]), encoding="utf-8")
    svg_path.write_text(to_svg(fr), encoding="utf-8")
    for pth in (csv_path, svg_path):
        print(f"[ok] wrote {pth} ({pth.stat().st_size / 1024:.1f} KB)")

    # Labour / crew-size sensitivity: the complementary "how many pickers" frontier (same small,
    # deterministic CSV + hand-drawn SVG treatment as the slotting sweep above; well under the
    # 10 KB deliverable gate, so reported separately from the PDF/Excel size check).
    lr = labour_report()
    lab_csv_path = out_dir / "labour_sensitivity.csv"
    lab_svg_path = out_dir / "labour_sensitivity.svg"
    lab_csv_path.write_text(labour_to_csv(lr), encoding="utf-8")
    lab_svg_path.write_text(labour_to_svg(lr), encoding="utf-8")
    for pth in (lab_csv_path, lab_svg_path):
        print(f"[ok] wrote {pth} ({pth.stat().st_size / 1024:.1f} KB)")

    # Rack-layout figures (velocity-slotted floor + before/after), the same deterministic SVGs the
    # README embeds from docs/img. Reported separately from the PDF/Excel > 10 KB size gate below.
    for pth in write_figures(out_dir):
        print(f"[ok] wrote {pth} ({pth.stat().st_size / 1024:.1f} KB)")

    return [pdf_path, xlsx_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logitwin", description="Warehouse digital twin CLI")
    parser.add_argument("--deliverables", action="store_true", help="write PDF + Excel to deliverables/")
    parser.add_argument("--summary", action="store_true", help="print headline numbers")
    parser.add_argument("--out", default="deliverables", help="output directory")
    args = parser.parse_args(argv)

    if args.summary:
        h = headline_numbers()
        print("[summary] warehouse digital twin (synthetic, seeded)")
        for k, v in h.items():
            print(f"  {k}: {v}")
        return 0

    if args.deliverables:
        paths = _write_deliverables(Path(args.out))
        for pth in paths:
            size_kb = pth.stat().st_size / 1024
            status = "[ok]" if size_kb > 10 else "[warn]"
            print(f"{status} wrote {pth} ({size_kb:.1f} KB)")
        if all(pth.stat().st_size > 10 * 1024 for pth in paths):
            print("[ok] all deliverables > 10 KB")
            return 0
        print("[warn] a deliverable is under 10 KB")
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
