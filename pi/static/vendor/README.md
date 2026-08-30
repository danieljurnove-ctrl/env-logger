# Vendored third-party assets

Committed rather than fetched at runtime: the Pi may have no route to the internet,
and the dashboard has to work when it doesn't. See docs/design.md#dashboard.

| File | Version | Licence |
| --- | --- | --- |
| `uPlot.iife.min.js`, `uPlot.min.css` | uPlot 1.6.32 | MIT (`uPlot.LICENSE`) |

Source: https://registry.npmjs.org/uplot/-/uplot-1.6.32.tgz (`dist/`), unmodified.

To update, replace both files from a newer tarball and bump the version above.
