"""Visual-regression tripwire for the /design style guide.

Screenshots /design at 1440px and 390px and diffs each against a committed
baseline (tests/baselines/design-<w>.png). Because /design renders every shared
component from the real app.css + _macros.html, a pixel change here means a shared
component's look changed — intended or not. Review the diff; if intended, refresh
the baselines.

    LIFEOS_UPDATE_BASELINES=1 pytest tests/test_visual.py   # accept current look

DEV-ONLY GUARD. Playwright + Pillow are dev deps (NOT in requirements.txt — the
runtime stays Flask-only), so this whole module SKIPS when either is missing or no
browser is installed. Baselines are rendered on Sam's Mac (Avenir Next, mac
font hinting); run the check on the same machine — the NAS container never runs
pytest. Full-page height is part of the compare, so a layout reflow trips it too.
"""

import os
import socket
import threading

import pytest

# Skip the module entirely unless the dev screenshot stack is present.
playwright = pytest.importorskip("playwright.sync_api")
Image = pytest.importorskip("PIL.Image")
ImageChops = pytest.importorskip("PIL.ImageChops")

BASELINE_DIR = os.path.join(os.path.dirname(__file__), "baselines")
VIEWPORTS = [1440, 390]

# Tolerance: antialiasing/subpixel jitter on the SAME machine is a handful of faint
# pixels — real component changes move thousands. Fail if >0.2% of pixels differ by
# more than a faint-edge threshold.
_PIXEL_DELTA = 24        # 0..255 per-pixel luma delta that counts as "changed"
_MAX_CHANGED_FRAC = 0.002
_UPDATE = os.environ.get("LIFEOS_UPDATE_BASELINES") == "1"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_url():
    """A real HTTP server for the app (Playwright needs a URL, not a test client).
    Reuses the throwaway DB/vault env the conftest already set up."""
    from werkzeug.serving import make_server
    from core import db_init, web_core
    from routes import (main, tasks, notes, journal, goals, settings as settings_bp,
                        docs, design)

    web_core._DB_PATH = os.environ["LIFEOS_DB_PATH"]
    db_init.init_db(os.environ["LIFEOS_DB_PATH"])
    for mod in (main, tasks, notes, journal, goals, settings_bp, docs, design):
        if mod.bp.name not in web_core.app.blueprints:
            web_core.app.register_blueprint(mod.bp)

    port = _free_port()
    srv = make_server("127.0.0.1", port, web_core.app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        t.join(timeout=5)


def _changed_fraction(a_path, b_img):
    """Fraction of pixels whose luma differs by more than _PIXEL_DELTA. Returns 1.0
    (total mismatch) if the images are different sizes — a reflow IS a regression."""
    a = Image.open(a_path).convert("RGB")
    if a.size != b_img.size:
        return 1.0, a.size, b_img.size
    diff = ImageChops.difference(a, b_img).convert("L")
    hist = diff.histogram()
    changed = sum(hist[_PIXEL_DELTA:])
    total = a.size[0] * a.size[1]
    return changed / total, a.size, b_img.size


@pytest.mark.parametrize("width", VIEWPORTS)
def test_design_matches_baseline(live_url, width, tmp_path):
    os.makedirs(BASELINE_DIR, exist_ok=True)
    baseline = os.path.join(BASELINE_DIR, f"design-{width}.png")

    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:                       # no browser binary installed
            pytest.skip(f"no chromium for visual test: {e}")
        page = browser.new_page(viewport={"width": width, "height": 1000},
                                device_scale_factor=1)
        # Determinism: block every cross-origin request (the CDN Sortable script) so a
        # slow/absent network can't change what renders or hang networkidle.
        page.route("**/*", lambda route: (
            route.continue_() if live_url in route.request.url else route.abort()))
        page.goto(f"{live_url}/design", wait_until="load")
        page.wait_for_timeout(300)                   # let load-in transitions settle
        shot = tmp_path / f"design-{width}.png"
        page.screenshot(path=str(shot), full_page=True, animations="disabled")
        browser.close()

    current = Image.open(shot).convert("RGB")

    if _UPDATE or not os.path.exists(baseline):
        current.save(baseline)
        if _UPDATE:
            pytest.skip(f"baseline refreshed: {baseline}")
        pytest.skip(f"baseline created (first run): {baseline}")

    frac, base_size, cur_size = _changed_fraction(baseline, current)
    # Keep the failing render next to the baseline for eyeballing the diff.
    if frac > _MAX_CHANGED_FRAC:
        current.save(os.path.join(BASELINE_DIR, f"design-{width}.actual.png"))
    assert frac <= _MAX_CHANGED_FRAC, (
        f"/design @ {width}px drifted from baseline: {frac:.4%} of pixels changed "
        f"(baseline {base_size}, current {cur_size}). If intended, refresh with "
        f"LIFEOS_UPDATE_BASELINES=1 pytest tests/test_visual.py. "
        f"Current render saved to design-{width}.actual.png.")
