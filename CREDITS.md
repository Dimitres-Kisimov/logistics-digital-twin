# Credits

All assets in this repository are original work by Dimitres Kisimov — © 2026 Dimitres Kisimov,
all rights reserved.

- **Code**: written from scratch for this project (Python package `logitwin`, Flask app, tests).
- **Layout interchange format** (`logitwin/layout.py`): the schema, element-type vocabulary, storage
  densities, capacity formula, aisle guard, and share-link codec **mirror WarehouseTwin** (the
  sibling app in `logistics-flow-studio`, © Dimitres Kisimov) so the two tools exchange the same
  layout files. Both tools are the author's own work; nothing third-party is reused.
- **Data**: 100% synthetic and deterministic (seeded generators in `logitwin/data.py`). No real
  customer, order, or facility data is used anywhere.
- **Graphics**: the warehouse-map view and all charts are hand-built — inline SVG / HTML Canvas in
  the web UI, matplotlib for the PDF, and pure-string SVG (no plotting library) for the committed
  figures under `docs/` (`logitwin/render.py` draws the rack-layout / before-after figures and
  `logitwin/sensitivity.py` the frontier chart, all deterministic from the seeded engine). No
  external image assets, no CDN resources, no web fonts.
- **Illustrative case studies** (Würth, Schwarz / Lidl / Kaufland) are based only on publicly
  available information. This project is independent, not affiliated with and not endorsed by those
  companies, and uses no internal data from them.

Third-party libraries are used under their respective open-source licenses (NumPy, SciPy, OR-Tools,
Matplotlib, Flask, Jinja2, openpyxl, pandas).
