"""Milestone 6: QC read back from disk, the run log, and the background job runner."""

import datetime as dt
import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from readystamp import qc
from readystamp.ingest.loose import load_loose
from readystamp.ingest.opt import load_opt
from readystamp.model import NumberingSettings, Settings
from readystamp.output import export as export_mod
from readystamp.output.export import RunBlocked
from readystamp.server.jobs import JobBusy, JobManager, execute

RUN_DATE = dt.date(2026, 10, 11)


def settings(out: Path, fmt: str, **numbering) -> Settings:
    s = Settings(numbering=NumberingSettings(**({"prefix": "ABC"} | numbering)))
    s.output.format, s.output.output_dir = fmt, out
    return s


def good(root):
    data = root / "prod_good" / "VOL001" / "DATA"
    return load_opt(data / "VOL001.opt", data / "VOL001.dat")


def loose(root):
    p = load_loose([root / "loose"])
    p.remove_documents({7})
    return p


def results(report: qc.QCReport) -> dict[str, bool]:
    return {c.name: c.passed for c in report.checks}


def failing(report: qc.QCReport) -> dict[str, list[str]]:
    return {c.name: c.details for c in report.checks if not c.passed}


@pytest.fixture(scope="module")
def tiff_run(fixture_root, tmp_path_factory):
    out = tmp_path_factory.mktemp("tiffrun") / "out"
    outcome = execute(good(fixture_root), settings(out, "tiff"), RUN_DATE)
    return outcome


@pytest.fixture(scope="module")
def pdf_run(fixture_root, tmp_path_factory):
    out = tmp_path_factory.mktemp("pdfrun") / "out"
    return execute(loose(fixture_root), settings(out, "pdf"), RUN_DATE)


def rerun_qc(outcome, tmp_path, corrupt) -> qc.QCReport:
    """Copy a finished run, corrupt the copy, and run QC on it again."""
    vol = tmp_path / "VOL001"
    shutil.copytree(outcome.result.volume_dir, vol)
    corrupt(vol)
    exp = qc.QCExpectations(
        format=outcome.run_log["settings"]["output"]["format"], source_kind=outcome.run_log["source_kind"],
        numbering_mode="renumber", volume_name="VOL001",
        input_documents=outcome.run_log["counts"]["input_documents"], failed_documents=0,
        expected_bates=[op.page.new_bates for od in outcome.result.documents for op in od.pages])
    return qc.run_qc(vol, exp)


# --- good runs pass ---------------------------------------------------------------------------

def test_qc_passes_on_good_runs(tiff_run, pdf_run):
    for outcome in (tiff_run, pdf_run):
        assert outcome.qc.passed, failing(outcome.qc)
        assert outcome.status == "completed"
        assert len(outcome.qc.checks) == 7
    assert list(results(tiff_run.qc)) == ["Document count", "Page count", "Bates continuity", "OPT ↔ files",
                                          "DAT ↔ OPT", "Files open", "Cross-reference"]


@pytest.mark.parametrize("fmt", ["pdf", "tiff"])
def test_qc_passes_keep_mode_and_multipage(fixture_root, tmp_path, fmt):
    o = execute(good(fixture_root), settings(tmp_path / "a", fmt, mode="keep"), RUN_DATE)
    assert o.qc.passed, failing(o.qc)
    mp = load_opt(fixture_root / "prod_multipage" / "VOL001" / "DATA" / "VOL001.opt")
    o = execute(mp, settings(tmp_path / "b", fmt, prefix="MP"), RUN_DATE)
    assert o.qc.passed, failing(o.qc)


# --- deliberately corrupted outputs are caught -------------------------------------------------

def test_deleted_image_fails_opt_check_and_names_file(tiff_run, tmp_path):
    report = rerun_qc(tiff_run, tmp_path, lambda v: (v / "IMAGES" / "IMG001" / "ABC000005.tif").unlink())
    assert not results(report)["OPT ↔ files"]
    assert any("ABC000005.tif" in d and "does not exist" in d for d in failing(report)["OPT ↔ files"])


def test_extra_image_fails_opt_check(tiff_run, tmp_path):
    def add(v):
        shutil.copy(v / "IMAGES" / "IMG001" / "ABC000001.tif", v / "IMAGES" / "IMG001" / "STRAY.tif")
    report = rerun_qc(tiff_run, tmp_path, add)
    assert any("STRAY.tif" in d for d in failing(report)["OPT ↔ files"])


def test_wrong_compression_fails_files_open(tiff_run, tmp_path):
    def recompress(v):
        p = v / "IMAGES" / "IMG001" / "ABC000002.tif"
        with Image.open(p) as im:
            im.load()
            im.save(p, compression="tiff_lzw")
    report = rerun_qc(tiff_run, tmp_path, recompress)
    assert failing(report) == {"Files open": ["IMAGES/IMG001/ABC000002.tif is black-and-white but not Group IV compressed."
                                              .replace("/", __import__("os").sep)]}


def test_unreadable_image_fails_files_open(tiff_run, tmp_path):
    report = rerun_qc(tiff_run, tmp_path, lambda v: (v / "IMAGES" / "IMG001" / "ABC000003.tif").write_bytes(b"junk"))
    assert "Files open" in failing(report)
    assert "ABC000003.tif cannot be opened" in failing(report)["Files open"][0]


def _edit_dat(v, fn):
    p = v / "DATA" / "VOL001.dat"
    text = p.read_bytes().decode("utf-8-sig")
    p.write_bytes(fn(text).encode("utf-8-sig"))


def test_wrong_endbates_fails_dat_opt(tiff_run, tmp_path):
    report = rerun_qc(tiff_run, tmp_path,
                      lambda v: _edit_dat(v, lambda t: t.replace("\xfeABC000003\xfe", "\xfeABC000004\xfe", 1)))
    assert "DAT ↔ OPT" in failing(report)
    assert "ENDBATES is ABC000004" in failing(report)["DAT ↔ OPT"][0]


def test_missing_dat_row_fails_counts(tiff_run, tmp_path):
    def drop(t):
        lines = t.split("\r\n")
        return "\r\n".join(lines[:2] + lines[3:])
    report = rerun_qc(tiff_run, tmp_path, lambda v: _edit_dat(v, drop))
    assert {"Document count", "Page count", "DAT ↔ OPT"} <= set(failing(report))


def test_missing_xref_row_fails_cross_reference(tiff_run, tmp_path):
    def drop(v):
        p = v / "DATA" / "VOL001_xref.csv"
        lines = p.read_bytes().decode("utf-8-sig").split("\r\n")
        p.write_bytes("\r\n".join(lines[:3] + lines[4:]).encode("utf-8-sig"))
    report = rerun_qc(tiff_run, tmp_path, drop)
    assert "Cross-reference" in failing(report)


def test_duplicate_bates_in_opt_fails_continuity(tiff_run, tmp_path):
    def dup(v):
        p = v / "DATA" / "VOL001.opt"
        p.write_bytes(p.read_bytes().replace(b"ABC000002,", b"ABC000001,", 1))
    report = rerun_qc(tiff_run, tmp_path, dup)
    assert any("ABC000001 appears more than once" in d for d in failing(report)["Bates continuity"])


def test_pdf_run_corruptions(pdf_run, tmp_path):
    report = rerun_qc(pdf_run, tmp_path / "a", lambda v: (v / "PDF" / "ABC000004.pdf").unlink())
    assert any("ABC000004.pdf" in d for d in failing(report)["DAT ↔ files"])
    report = rerun_qc(pdf_run, tmp_path / "b", lambda v: (v / "PDF" / "ABC000006.pdf").write_bytes(b"%PDF-1.7 bad"))
    assert "Files open" in failing(report)
    report = rerun_qc(pdf_run, tmp_path / "c", lambda v: shutil.copy(v / "PDF" / "ABC000001.pdf", v / "PDF" / "X.pdf"))
    assert any("X.pdf" in d for d in failing(report)["DAT ↔ files"])


def test_missing_load_file(tiff_run, tmp_path):
    report = rerun_qc(tiff_run, tmp_path, lambda v: (v / "DATA" / "VOL001.opt").unlink())
    assert any("VOL001.opt is missing" in d for d in failing(report)["Document count"])


# --- run log -------------------------------------------------------------------------------------------

def test_run_log_contents_and_hashes(tiff_run, fixture_root):
    log = json.loads(tiff_run.run_log_path.read_text(encoding="utf-8"))
    vol = tiff_run.result.volume_dir
    assert log["tool"].startswith("ReadyStamp Lite") and log["status"] == "completed"
    assert log["started"] <= log["ended"]
    assert log["settings"]["numbering"]["prefix"] == "ABC"
    assert log["counts"] == {"input_documents": 5, "documents_written": 5, "pages_written": 13, "failed_documents": 0}
    assert log["qc"]["passed"] and len(log["qc"]["checks"]) == 7
    paths = {o["path"] for o in log["outputs"]}
    assert "DATA\\VOL001_qc.html" in paths and "DATA\\VOL001.opt" in paths and "DATA\\run_log.json" not in paths
    assert len([p for p in paths if p.startswith("IMAGES\\")]) == 13
    for o in log["outputs"]:
        f = vol.joinpath(*o["path"].split("\\"))
        assert hashlib.sha256(f.read_bytes()).hexdigest() == o["sha256"]
    # inputs: hashed before the run and still the same afterwards
    assert log["inputs_unchanged"] is True
    assert len(log["inputs"]) == 13
    for i in log["inputs"]:
        assert hashlib.sha256(Path(i["path"]).read_bytes()).hexdigest() == i["sha256"]


def test_qc_html_written(tiff_run):
    text = tiff_run.qc_path.read_text(encoding="utf-8")
    assert "All checks passed" in text and "Bates continuity" in text


def test_failed_document_recorded(fixture_root, tmp_path, monkeypatch):
    real = export_mod.tiff_out.render_page

    def flaky(production, doc, page, *a, **k):
        if doc.seq == 2:
            raise RuntimeError("boom")
        return real(production, doc, page, *a, **k)

    monkeypatch.setattr(export_mod.tiff_out, "render_page", flaky)
    o = execute(good(fixture_root), settings(tmp_path / "out", "tiff"), RUN_DATE)
    assert o.status == "completed_with_failures"
    assert o.run_log["failures"][0]["document"] == 2
    assert results(o.qc)["Document count"]                      # 5 input - 1 failed = 4 written
    assert o.qc.passed, failing(o.qc)


def test_blocked_run_writes_nothing(fixture_root, tmp_path):
    data = fixture_root / "prod_broken" / "VOL001" / "DATA"
    with pytest.raises(RunBlocked):
        execute(load_opt(data / "VOL001.opt", data / "VOL001.dat"), settings(tmp_path / "out", "tiff"))
    assert not (tmp_path / "out").exists()


# --- job runner --------------------------------------------------------------------------------------------

def wait_for(manager, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        snap = manager.get(job_id)
        if snap["status"] not in ("queued", "running"):
            return snap
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_job_runs_in_background_with_progress(fixture_root, tmp_path):
    m = JobManager()
    job_id = m.start(good(fixture_root), settings(tmp_path / "out", "tiff"), RUN_DATE)
    snap = wait_for(m, job_id)
    assert snap["status"] == "completed" and snap["qc_passed"]
    assert snap["pages_done"] == snap["pages_total"] == 13
    assert snap["current_document"] == 5 and snap["elapsed_seconds"] >= 0
    assert Path(snap["run_log"]).exists() and Path(snap["qc_report"]).exists()


def test_only_one_job_at_a_time_and_cancel(fixture_root, tmp_path, monkeypatch):
    real = export_mod.tiff_out.render_page

    def slow(*a, **k):
        time.sleep(0.15)
        return real(*a, **k)

    monkeypatch.setattr(export_mod.tiff_out, "render_page", slow)
    m = JobManager()
    job_id = m.start(good(fixture_root), settings(tmp_path / "out", "tiff"), RUN_DATE)
    with pytest.raises(JobBusy):
        m.start(good(fixture_root), settings(tmp_path / "other", "tiff"))
    while m.get(job_id)["pages_done"] < 5:
        time.sleep(0.02)
    assert m.cancel(job_id)
    snap = wait_for(m, job_id)
    assert snap["status"] == "cancelled"
    files = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert not any(p.name.endswith(".partial") for p in files)         # nothing incomplete under any name
    log = json.loads(Path(snap["run_log"]).read_text())
    assert log["status"] == "cancelled"
    for o in log["outputs"]:                                            # every file left is complete and hashed
        f = (tmp_path / "out" / "VOL001").joinpath(*o["path"].split("\\"))
        assert hashlib.sha256(f.read_bytes()).hexdigest() == o["sha256"]
        if f.suffix == ".tif":
            with Image.open(f) as im:
                im.load()
    assert not m.cancel(job_id)


def test_blocked_and_failed_jobs_report_messages(fixture_root, tmp_path, monkeypatch):
    m = JobManager()
    s = settings(tmp_path / "x", "tiff")
    s.output.output_dir = None
    snap = wait_for(m, m.start(good(fixture_root), s))
    assert snap["status"] == "blocked" and "Choose an output folder." in snap["problems"]
    monkeypatch.setattr("readystamp.server.jobs.qc.run_qc", lambda *a: 1 / 0)
    snap = wait_for(m, m.start(good(fixture_root), settings(tmp_path / "y", "tiff")))
    assert snap["status"] == "failed" and snap["problems"][0].startswith("The run stopped unexpectedly")
