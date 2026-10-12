"""Browser walkthrough of the web UI: loose files to PDF, then an OPT with DAT to TIFF.

Usage: python scripts/ui_walkthrough.py WORK_DIR [CHROMIUM_EXECUTABLE]
Generates the test fixtures in WORK_DIR, starts `readystamp ui` with HOME=WORK_DIR/home,
drives every screen with Playwright, saves screenshots to WORK_DIR/shots, and checks that
the inputs were not changed.
"""
import hashlib
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests")]
import fixtures  # noqa: E402

SP = Path(sys.argv[1]).resolve()
CHROME = sys.argv[2] if len(sys.argv) > 2 else None
FX = SP / "fx"
SHOTS = SP / "shots"
PORT = 8731
URL = f"http://127.0.0.1:{PORT}/"
SP.mkdir(parents=True, exist_ok=True)
for leftover in ("out_loose", "out_opt"):
    if (SP / leftover).exists():
        sys.exit(f"{SP / leftover} already exists; use a fresh WORK_DIR.")
SHOTS.mkdir(exist_ok=True)
fixtures.generate_all(FX)


def hashes(d):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file()}


before = hashes(FX / "loose") | hashes(FX / "prod_good")
errors = []
server = subprocess.Popen([sys.executable, "-m", "readystamp", "ui", "--port", str(PORT), "--no-browser"],
                          cwd=REPO, env={**os.environ, "HOME": str(SP / "home"), "NO_PROXY": "127.0.0.1"})
for _ in range(50):
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(URL, timeout=1)
        break
    except OSError:
        time.sleep(0.2)

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=CHROME, args=["--no-proxy-server"])
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
    page.on("dialog", lambda d: d.accept())
    page.on("response", lambda r: r.status >= 400 and errors.append(f"{r.status} {r.request.method} {r.url}"))
    page.goto(URL)
    shot = lambda name: page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)

    # ---- Walkthrough 1: loose files -> PDF ----
    expect(page.locator("#screen-load")).to_be_visible()
    assert page.locator('.step[data-screen="review"]').is_disabled()
    names = ["doc1_memo.pdf", "doc2_mixed.pdf", "doc3_rotated.pdf", "doc4_scan.tif", "doc5_photo.jpg", "doc6_diagram.png", "doc10_locked.pdf", "readme.txt"]
    page.set_input_files("#pick-files", [str(FX / "loose" / n) for n in names])
    expect(page.locator("#pending-count")).to_have_text("8 files ready to load")
    shot("01_load_loose")
    page.click("#load-loose")
    expect(page.locator("#screen-review")).to_be_visible()
    expect(page.locator("#docs-table tbody tr")).to_have_count(7)
    assert page.locator('.step[data-screen="number"]').is_disabled(), "locked pdf should block"
    shot("02_review_errors")
    page.click("#remove-errors")
    expect(page.locator("#docs-table tbody tr")).to_have_count(6)
    expect(page.locator('.step[data-screen="number"]')).to_be_enabled()
    # drag the last row (doc6) above the first
    rows = page.locator("#docs-table tbody tr")
    rows.nth(5).drag_to(rows.nth(0))
    expect(rows.nth(0).locator("td").nth(2)).to_have_text("doc6_diagram.png")
    page.click("#sort-name")
    expect(rows.nth(0).locator("td").nth(2)).to_have_text("doc1_memo.pdf")
    rows.nth(4).drag_to(rows.nth(1))   # doc5_photo to second place
    expect(rows.nth(1).locator("td").nth(2)).to_have_text("doc5_photo.jpg")
    shot("03_review_reordered")

    page.click('.step[data-screen="number"]')
    expect(page.locator('input[name="num-mode"][value="keep"]')).to_be_disabled()
    page.fill('[data-setting="numbering.prefix"]', "AB:C")
    expect(page.locator("#screen-number .field .error")).to_be_visible()
    assert page.locator('.step[data-screen="stamp"]').is_disabled()
    page.fill('[data-setting="numbering.prefix"]', "ACME")
    page.fill('[data-setting="numbering.separator"]', "_")
    page.fill('[data-setting="numbering.padding"]', "1")
    expect(page.locator("#numbering-problems li")).to_have_count(1)
    page.fill('[data-setting="numbering.padding"]', "7")
    expect(page.locator("#numbering-sample")).to_contain_text("ACME_0000001")
    shot("04_number")

    page.click('.step[data-screen="stamp"]')
    expect(page.locator("#preview-img")).to_be_visible()
    page.click('.zone:has-text("TC")')
    page.fill('[data-zone="template"]', "Page {PAGE} of {PAGES}\n")
    page.select_option("#token-picker", "FILENAME")
    page.fill('[data-zone="size_pt"]', "12")
    page.check('[data-zone="bold"]')
    expect(page.locator(".zone.enabled")).to_have_count(3)
    page.fill('[data-zone="template"]', "{BOGUS}")
    expect(page.locator("#zone-errors li")).to_have_count(1)
    expect(page.locator("#preview-error")).to_be_visible()
    page.fill('[data-zone="template"]', "Page {PAGE} of {PAGES}\n{FILENAME}")
    expect(page.locator("#zone-errors li")).to_have_count(0)
    expect(page.locator("#preview-img")).to_be_visible()
    page.click("#next-page"); page.click("#next-page"); page.click("#next-page")
    expect(page.locator("#preview-label")).to_contain_text("Document 2 of 6 · page 1 of 1")
    page.fill("#jump-bates", "ACME_0000006"); page.click("#jump-go")
    expect(page.locator("#preview-label")).to_contain_text("ACME_0000006")
    page.click("#next-doc")
    page.click("#jump-landscape")
    jl = page.evaluate("S.production.jumps.first_landscape")
    expect(page.locator("#preview-label")).to_contain_text(f"Document {jl[0]} of 6 · page {jl[1]} ")
    page.check('input[name="margin-mode"][value="add_band"]')
    page.wait_for_timeout(1500)
    shot("05_stamp_band")
    page.check('input[name="margin-mode"][value="overlay"]')
    page.click("#jump-portrait")
    page.wait_for_timeout(1000)
    shot("06_stamp")

    page.click('.step[data-screen="output"]')
    expect(page.locator('input[name="out-format"][value="pdf"]')).to_be_checked()
    expect(page.locator("#tiff-options")).to_be_hidden()
    expect(page.locator("#preflight-errors")).to_contain_text("Choose an output folder")
    assert page.locator('.step[data-screen="run"]').is_disabled()
    page.fill('[data-setting="output.output_dir"]', str(SP / "out_loose"))
    expect(page.locator("#preflight-errors li")).to_have_count(0)
    page.fill("#preset-name", "Walkthrough")
    page.click("#preset-save")
    expect(page.locator("#messages")).to_contain_text('Saved the preset "Walkthrough"')
    shot("07_output_pdf")

    page.click('.step[data-screen="run"]')
    page.click("#run-start")
    expect(page.locator("#run-result")).to_be_visible(timeout=120000)
    expect(page.locator("#run-status")).to_contain_text("QC passed")
    expect(page.locator("#qc-table tbody tr")).to_have_count(7)
    assert page.locator("#qc-table .badge.fail").count() == 0
    shot("08_run_pdf")
    with page.expect_popup() as pop:
        page.click("#open-qc")
    assert "QC report" in pop.value.content(); pop.value.close()

    # ---- Walkthrough 2: OPT + DAT -> TIFF ----
    page.click('.step[data-screen="load"]')
    page.click('.tab[data-tab="opt"]')
    page.click('[data-browse="opt"]')
    page.wait_for_selector("#browser-list li")
    page.fill("#browser-path", str(FX / "prod_good")); page.click("#browser-go")
    page.click('#browser-list li.dir:has-text("VOL001")')
    page.click('#browser-list li.dir:has-text("DATA")')
    page.click('#browser-list li.file:has-text("VOL001.opt")')
    expect(page.locator("#opt-path")).to_have_value(str(FX / "prod_good" / "VOL001" / "DATA" / "VOL001.opt"))
    page.click('[data-browse="dat"]')
    page.click('#browser-list li.file:has-text("VOL001.dat")')
    expect(page.locator("#dat-key")).to_have_value("BEGBATES")
    expect(page.locator("#dat-info")).to_contain_text("5 rows")
    shot("09_load_opt")
    page.click("#load-opt")
    expect(page.locator("#screen-review")).to_be_visible()
    expect(page.locator("#docs-table tbody tr")).to_have_count(5)
    expect(page.locator("#sort-name")).to_be_hidden()
    shot("10_review_opt")

    page.click('.step[data-screen="number"]')
    page.check('input[name="num-mode"][value="keep"]')
    expect(page.locator("#numbering-sample")).to_contain_text("REV000101")
    page.click('.step[data-screen="stamp"]')
    page.click('.zone:has-text("TR")')
    page.select_option("#token-picker", "FIELD:CUSTODIAN")
    page.fill('[data-zone="color"]', "#cc0000")
    page.click('.zone:has-text("MC")')
    page.fill('[data-zone="template"]', "DRAFT")
    page.select_option('[data-zone="rotation"]', "45")
    page.fill('[data-zone="size_pt"]', "60")
    page.click('.step[data-screen="output"]')
    page.check('input[name="out-format"][value="tiff"]')
    expect(page.locator("#tiff-options")).to_be_visible()
    # the output folder from the loose run is now full
    expect(page.locator("#preflight-errors")).to_contain_text("not empty")
    (SP / "out_opt").mkdir()
    page.click('[data-browse="dir"]')
    page.wait_for_selector("#browser-list li")
    page.fill("#browser-path", str(SP)); page.click("#browser-go")
    page.wait_for_timeout(500)
    try:
        page.click('#browser-list li.dir:has-text("out_opt")', timeout=5000)
    except Exception:
        shot("fail"); print(page.inner_html("#browser")); raise
    page.click("#browser-choose")
    expect(page.locator('[data-setting="output.output_dir"]')).to_have_value(str(SP / "out_opt"))
    expect(page.locator("#preflight-errors li")).to_have_count(0)
    page.click('.step[data-screen="stamp"]')
    expect(page.locator("#preview-note")).to_contain_text("TIFF")
    page.wait_for_timeout(1200)
    shot("11_stamp_tiff")
    page.click('.step[data-screen="output"]')
    shot("12_output_tiff")
    page.click('.step[data-screen="run"]')
    page.click("#run-start")
    expect(page.locator("#run-result")).to_be_visible(timeout=120000)
    expect(page.locator("#run-status")).to_contain_text("QC passed")
    shot("13_run_tiff")

    # preset load restores walkthrough-1 stamps but keeps the folder
    page.click('.step[data-screen="output"]')
    page.select_option("#preset-list", "Walkthrough")
    page.click("#preset-load")
    expect(page.locator("#messages")).to_contain_text("Loaded the preset")
    expect(page.locator('input[name="out-format"][value="pdf"]')).to_be_checked()
    expect(page.locator('[data-setting="output.output_dir"]')).to_have_value(str(SP / "out_opt"))
    browser.close()

server.terminate()
after = hashes(FX / "loose") | hashes(FX / "prod_good")
unexpected = [e for e in errors if "Failed to load resource" not in e and not any(
    e.startswith(ok) for ok in ("409 GET " + URL + "api/production", "422 PUT", "400 GET " + URL + "api/preview"))]
print("inputs unchanged:", before == after)
print("unexpected errors:", unexpected)
assert before == after and not unexpected
print("loose pdfs:", sorted(p.name for p in (SP / "out_loose" / "VOL001" / "PDF").iterdir()))
print("tiffs:", sorted(p.name for p in (SP / "out_opt" / "VOL001" / "IMAGES" / "IMG001").iterdir()))
