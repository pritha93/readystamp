"""API tests: every route of the local web server, through FastAPI's TestClient."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from readystamp.server import app as app_mod
from readystamp.server import jobs as jobs_mod
from readystamp.server.app import create_app

LOOSE = ["doc1_memo.pdf", "doc2_mixed.pdf", "doc4_scan.tif", "doc5_photo.jpg", "readme.txt"]


@pytest.fixture
def client(tmp_path):
    app = create_app(work_dir=tmp_path / "work", presets_dir=tmp_path / "presets")
    with TestClient(app) as c:
        c.rs = app.state.rs
        yield c


def good_opt(root: Path) -> Path:
    return root / "prod_good" / "VOL001" / "DATA" / "VOL001.opt"


def upload(client, root: Path, names=LOOSE, folder="batch"):
    files = [("files", (n, (root / "loose" / n).read_bytes(), "application/octet-stream")) for n in names]
    return client.post("/api/load/loose", files=files, data={"paths": [f"{folder}/{n}" for n in names]})


def load_good(client, root: Path, dat=True):
    body = {"opt_path": str(good_opt(root))}
    if dat:
        body["dat_path"] = str(good_opt(root).with_suffix(".dat"))
    r = client.post("/api/load/opt", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def settings(client) -> dict:
    return client.get("/api/settings").json()["settings"]


def put(client, s: dict):
    return client.put("/api/settings", json=s)


def wait(client, job_id: str) -> dict:
    return client.rs.jobs.wait(job_id, timeout=120)


def assert_messages(r, status: int) -> list[str]:
    assert r.status_code == status, r.text
    msgs = r.json()["messages"]
    assert msgs and all(isinstance(m, str) and "Traceback" not in m for m in msgs)
    return msgs


# --- pages ---------------------------------------------------------------------------------------

def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "ReadyStamp Lite" in r.text
    for name in ("app.js", "styles.css"):
        assert client.get(f"/static/{name}").status_code == 200


# --- loading ---------------------------------------------------------------------------------------

def test_load_loose_upload(client, fixture_root):
    r = upload(client, fixture_root)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["source_kind"] == "loose"
    assert [d["source_name"] for d in view["documents"]] == ["doc1_memo.pdf", "doc2_mixed.pdf", "doc4_scan.tif", "doc5_photo.jpg"]
    assert view["totals"]["documents"] == 4
    assert any(i["check"] == "unsupported_type" and i["severity"] == "info" for i in view["issues"])
    # Folder structure from the browser is kept inside the upload folder.
    uploads = list((client.rs.work_dir / "uploads").iterdir())
    assert len(uploads) == 1 and (uploads[0] / "batch" / "doc1_memo.pdf").is_file()
    assert view["documents"][0]["pages"][0]["bates"] == "000001"


def test_new_load_drops_previous_upload_copy(client, fixture_root):
    upload(client, fixture_root, names=["doc1_memo.pdf"])
    first = next((client.rs.work_dir / "uploads").iterdir())
    upload(client, fixture_root, names=["doc5_photo.jpg"])
    assert not first.exists() and len(list((client.rs.work_dir / "uploads").iterdir())) == 1
    load_good(client, fixture_root)
    assert list((client.rs.work_dir / "uploads").iterdir()) == []
    assert (fixture_root / "loose" / "doc1_memo.pdf").is_file()


def test_load_loose_rejects_path_escape(client, fixture_root):
    data = (fixture_root / "loose" / "doc1_memo.pdf").read_bytes()
    r = client.post("/api/load/loose", files=[("files", ("x.pdf", data, "application/pdf"))],
                    data={"paths": ["../../../evil/doc1.pdf"]})
    assert r.status_code == 200
    upload_dir = next((client.rs.work_dir / "uploads").iterdir())
    assert (upload_dir / "evil" / "doc1.pdf").is_file()
    assert not (client.rs.work_dir.parent / "evil").exists()


def test_load_loose_encrypted_is_error(client, fixture_root):
    view = upload(client, fixture_root, names=["doc1_memo.pdf", "doc10_locked.pdf"]).json()
    assert view["has_errors"]
    locked = next(d for d in view["documents"] if d["source_name"] == "doc10_locked.pdf")
    assert locked["status"] == "error" and locked["page_count"] == 0


def test_load_opt_with_dat(client, fixture_root):
    view = load_good(client, fixture_root)
    assert view["source_kind"] == "opt" and view["totals"]["pages"] == 13
    assert view["documents"][0]["original_bates"] == "REV000101"
    assert "CUSTODIAN" in view["dat_columns"]
    assert client.rs.extra_inputs == [good_opt(fixture_root), good_opt(fixture_root).with_suffix(".dat")]


def test_load_opt_errors_are_readable(client, fixture_root, tmp_path):
    assert_messages(client.post("/api/load/opt", json={"opt_path": str(tmp_path / "nope.opt")}), 400)
    r = client.post("/api/load/opt", json={"opt_path": str(good_opt(fixture_root)),
                                          "dat_path": str(good_opt(fixture_root).with_suffix(".dat")),
                                          "dat_key": "NoSuchColumn"})
    assert_messages(r, 400)
    assert any("opt_path" in m for m in assert_messages(client.post("/api/load/opt", json={}), 422))


def test_load_broken_opt_report(client, fixture_root):
    opt = fixture_root / "prod_broken" / "VOL001" / "DATA" / "VOL001.opt"
    r = client.post("/api/load/opt", json={"opt_path": str(opt), "dat_path": str(opt.with_suffix(".dat"))})
    view = r.json()
    assert view["has_errors"]
    checks = {i["check"] for i in view["issues"]}
    assert {"missing_file", "duplicate_key", "page_count_mismatch", "bates_gap"} <= checks
    assert any(d["status"] == "error" for d in view["documents"])


def test_dat_columns(client, fixture_root):
    dat = good_opt(fixture_root).with_suffix(".dat")
    info = client.get("/api/dat/columns", params={"path": str(dat)}).json()
    assert info["key"] == "BEGBATES" and info["rows"] == 5
    assert "comma" in info["delimiters"] and "double_quote" in info["qualifiers"]
    assert_messages(client.get("/api/dat/columns", params={"path": str(dat.with_name("missing.dat"))}), 400)


def test_fs_list(client, fixture_root):
    data_dir = good_opt(fixture_root).parent
    r = client.get("/api/fs/list", params={"path": str(data_dir), "kind": "opt"}).json()
    assert r["files"] == ["VOL001.opt"] and r["parent"] == str(data_dir.parent.resolve())
    r = client.get("/api/fs/list", params={"path": str(data_dir), "kind": "dat"}).json()
    assert r["files"] == ["VOL001.dat"]
    r = client.get("/api/fs/list", params={"path": str(data_dir.parent), "kind": "dir"}).json()
    assert r["dirs"] == ["DATA", "IMAGES"] and r["files"] == []
    assert client.get("/api/fs/list").json()["path"] == str(Path.home().resolve())
    assert_messages(client.get("/api/fs/list", params={"path": str(good_opt(fixture_root))}), 400)
    assert_messages(client.get("/api/fs/list", params={"path": str(data_dir), "kind": "exe"}), 400)


# --- production ------------------------------------------------------------------------------------

def test_production_requires_load(client):
    assert_messages(client.get("/api/production"), 409)
    assert_messages(client.get("/api/production/report.csv"), 409)
    assert_messages(client.get("/api/preview", params={"doc": 1}), 409)


def test_production_view_and_report_csv(client, fixture_root):
    load_good(client, fixture_root)
    view = client.get("/api/production").json()
    assert view["jumps"]["first_portrait"] == [1, 1]
    r = client.get("/api/production/report.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbf")


def test_order_loose_reorder_and_remove(client, fixture_root):
    upload(client, fixture_root)
    view = client.put("/api/production/order", json={"order": [4, 3, 2, 1]}).json()
    assert [d["source_name"] for d in view["documents"]] == ["doc5_photo.jpg", "doc4_scan.tif", "doc2_mixed.pdf", "doc1_memo.pdf"]
    assert [d["seq"] for d in view["documents"]] == [1, 2, 3, 4]
    assert view["documents"][0]["beg_bates"] == "000001"     # numbering follows the new order
    view = client.put("/api/production/order", json={"remove": [1]}).json()
    assert [d["source_name"] for d in view["documents"]] == ["doc4_scan.tif", "doc2_mixed.pdf", "doc1_memo.pdf"]
    assert_messages(client.put("/api/production/order", json={"order": [1, 2]}), 400)
    assert_messages(client.put("/api/production/order", json={"remove": [9]}), 400)


def test_order_remove_clears_errors(client, fixture_root):
    upload(client, fixture_root, names=["doc1_memo.pdf", "doc10_locked.pdf"])
    view = client.put("/api/production/order", json={"remove": [2]}).json()
    assert not view["has_errors"] and len(view["documents"]) == 1


def test_order_opt_allows_remove_only(client, fixture_root):
    load_good(client, fixture_root)
    assert_messages(client.put("/api/production/order", json={"order": [2, 1, 3, 4, 5]}), 400)
    view = client.put("/api/production/order", json={"remove": [2]}).json()
    assert len(view["documents"]) == 4


# --- settings ---------------------------------------------------------------------------------------

def test_settings_defaults(client, fixture_root):
    view = client.get("/api/settings").json()
    assert view["settings"]["output"]["format"] == "pdf" and view["settings"]["output"]["output_dir"] is None
    assert view["preflight"] is None
    load_good(client, fixture_root)
    view = client.get("/api/settings").json()
    assert view["numbering"]["sample"]["first"] == "000001"
    assert "Choose an output folder." in view["preflight"]["errors"]
    assert "FIELD:CUSTODIAN" in view["tokens"] and "BATES" in view["tokens"]


def test_settings_field_errors(client, fixture_root):
    load_good(client, fixture_root)
    s = settings(client)
    s["numbering"]["prefix"] = "AB:C"
    s["output"]["volume_name"] = "VOL/1"
    r = put(client, s)
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert "numbering.prefix" in body["field_errors"] and "output.volume_name" in body["field_errors"]
    assert settings(client)["numbering"]["prefix"] == ""          # the bad settings were not kept
    s = settings(client)
    s["unknown"] = 1
    assert "unknown" in put(client, s).json()["field_errors"]


def test_settings_numbering_and_zone_problems(client, fixture_root):
    load_good(client, fixture_root)
    s = settings(client)
    s["numbering"].update(prefix="ABC", separator="_", start=99, padding=2)
    view = put(client, s).json()
    assert view["ok"] and view["numbering"]["problems"] and view["numbering"]["sample"] is None
    s["numbering"].update(padding=6)
    s["zones"]["TR"] = {**s["zones"]["TR"], "enabled": True, "template": "{NOPE}"}
    view = put(client, s).json()
    assert view["numbering"]["sample"]["first"] == "ABC_000099"
    assert view["zones"]["TR"]
    assert_messages(client.get("/api/preview", params={"doc": 1}), 400)
    s["zones"]["TR"]["template"] = "{FIELD:CUSTODIAN}"
    assert put(client, s).json()["zones"] == {}
    assert client.get("/api/production").json()["documents"][0]["beg_bates"] == "ABC_000099"


# --- preview ----------------------------------------------------------------------------------------

def test_preview_pdf_and_tiff(client, fixture_root):
    upload(client, fixture_root)
    r = client.get("/api/preview", params={"doc": 1, "page": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    s = settings(client)
    s["output"]["format"] = "tiff"
    put(client, s)
    assert client.get("/api/preview", params={"doc": 3, "page": 1, "dpi": 50}).status_code == 200
    assert_messages(client.get("/api/preview", params={"doc": 9}), 404)
    assert_messages(client.get("/api/preview", params={"doc": 1, "page": 99}), 404)
    assert_messages(client.get("/api/preview", params={"doc": 1, "dpi": 1000}), 400)


def test_locate(client, fixture_root):
    load_good(client, fixture_root)
    assert client.get("/api/locate", params={"bates": "000006"}).json() == {"doc": 3, "page": 2}
    assert client.get("/api/locate", params={"bates": "rev000106"}).json() == {"doc": 3, "page": 2}
    assert_messages(client.get("/api/locate", params={"bates": "XYZ"}), 404)


# --- runs ---------------------------------------------------------------------------------------------

def _hashes(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_job_opt_to_tiff(client, fixture_root, tmp_path, monkeypatch):
    load_good(client, fixture_root)
    before = _hashes(fixture_root / "prod_good")
    s = settings(client)
    assert_messages(client.post("/api/jobs"), 400)                   # no output folder yet
    s["output"].update(format="tiff", output_dir=str(tmp_path / "out"))
    s["numbering"]["mode"] = "keep"
    assert put(client, s).json()["ok"]
    r = client.post("/api/jobs")
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    snap = wait(client, job_id)
    assert snap["status"] == "completed" and snap["qc_passed"], snap
    assert client.get(f"/api/jobs/{job_id}").json()["pages_done"] == 13
    vol = tmp_path / "out" / "VOL001"
    assert (vol / "IMAGES" / "IMG001" / "REV000101.tif").is_file()
    assert (vol / "IMAGES" / "IMG001" / "REV000113.jpg").is_file()
    qc = client.get(f"/api/jobs/{job_id}/qc")
    assert qc.status_code == 200 and "QC report" in qc.text
    log = json.loads((vol / "DATA" / "run_log.json").read_text(encoding="utf-8"))
    assert log["inputs_unchanged"] and any(i["path"].endswith("VOL001.dat") for i in log["inputs"])
    assert _hashes(fixture_root / "prod_good") == before

    opened = []
    monkeypatch.setattr(app_mod.subprocess, "Popen", lambda args, **kw: opened.append(args))
    monkeypatch.setattr(app_mod.sys, "platform", "linux")
    r = client.post(f"/api/jobs/{job_id}/open").json()
    assert r["opened"] and opened and opened[0][-1] == str(vol)

    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": False}
    # The folder now holds this run, so the next run is refused.
    assert any("not empty" in m for m in assert_messages(client.post("/api/jobs"), 400))


def test_job_loose_to_pdf(client, fixture_root, tmp_path):
    upload(client, fixture_root)
    s = settings(client)
    s["output"]["output_dir"] = str(tmp_path / "out")
    put(client, s)
    snap = wait(client, client.post("/api/jobs").json()["id"])
    assert snap["status"] == "completed" and snap["qc_passed"]
    pdfs = sorted(p.name for p in (tmp_path / "out" / "VOL001" / "PDF").iterdir())
    assert pdfs == ["000001.pdf", "000004.pdf", "000006.pdf", "000008.pdf"]
    xref = (tmp_path / "out" / "VOL001" / "DATA" / "VOL001_xref.csv").read_text(encoding="utf-8-sig")
    assert "batch\\doc1_memo.pdf" in xref and "uploads" not in xref


def test_job_unknown_ids(client):
    for method, url in [("get", "/api/jobs/nope"), ("post", "/api/jobs/nope/cancel"),
                        ("get", "/api/jobs/nope/qc"), ("post", "/api/jobs/nope/open")]:
        assert_messages(getattr(client, method)(url), 404)


def test_job_busy_cancel_and_locking(client, fixture_root, tmp_path, monkeypatch):
    load_good(client, fixture_root)
    s = settings(client)
    s["output"]["output_dir"] = str(tmp_path / "out")
    put(client, s)
    release = threading.Event()
    real = jobs_mod.execute

    def slow(production, settings, run_date=None, progress=None, should_cancel=None, extra_inputs=None):
        def gated_progress(*a):
            release.wait(10)
            progress(*a)
        return real(production, settings, run_date, progress=gated_progress, should_cancel=should_cancel,
                    extra_inputs=extra_inputs)

    monkeypatch.setattr(jobs_mod, "execute", slow)
    job_id = client.post("/api/jobs").json()["id"]
    assert_messages(client.post("/api/jobs"), 409)
    assert_messages(put(client, s), 409)
    assert_messages(client.put("/api/production/order", json={"remove": [1]}), 409)
    assert_messages(client.post("/api/load/opt", json={"opt_path": str(good_opt(fixture_root))}), 409)
    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": True}
    release.set()
    snap = wait(client, job_id)
    assert snap["status"] == "cancelled"
    assert not list((tmp_path / "out").rglob("*.partial"))


# --- presets --------------------------------------------------------------------------------------------

def test_presets(client, fixture_root, tmp_path):
    assert client.get("/api/presets").json() == {"presets": []}
    load_good(client, fixture_root)
    s = settings(client)
    s["output"]["output_dir"] = str(tmp_path / "out")
    s["zones"]["TC"] = {**s["zones"]["TC"], "enabled": True, "template": "{FIELD:CUSTODIAN}", "color": "#cc0000"}
    put(client, s)
    assert client.post("/api/presets", json={"name": "Client A"}).json() == {"saved": "Client A"}
    saved = json.loads((client.rs.presets_dir / "Client A.json").read_text(encoding="utf-8"))
    assert "output_dir" not in saved["output"] and saved["zones"]["TC"]["color"] == "#CC0000"
    assert client.get("/api/presets").json() == {"presets": ["Client A"]}

    # Loading a preset keeps the current output folder.
    got = client.get("/api/presets/Client A").json()["settings"]
    assert got["zones"]["TC"]["template"] == "{FIELD:CUSTODIAN}" and got["output"]["output_dir"] == str(tmp_path / "out")

    explicit = {"numbering": {"prefix": "X"}}
    assert client.post("/api/presets", json={"name": "partial", "settings": explicit}).status_code == 200
    assert client.get("/api/presets/partial").json()["settings"]["numbering"]["prefix"] == "X"

    assert_messages(client.post("/api/presets", json={"name": "../evil"}), 400)
    assert_messages(client.post("/api/presets", json={"name": "bad", "settings": {"output": {"output_dir": "/x"}}}), 400)
    assert_messages(client.get("/api/presets/missing"), 404)
    assert_messages(client.get("/api/presets/bad$name"), 400)
    (client.rs.presets_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert_messages(client.get("/api/presets/broken"), 400)


# --- serving ----------------------------------------------------------------------------------------------

def test_serve_binds_localhost_only(monkeypatch):
    calls = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(kw))
    app_mod.serve(port=8765, open_browser=False)
    assert calls["host"] == "127.0.0.1" and calls["port"] == 8765
