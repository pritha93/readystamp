"""Local web API and static front end. Serves on 127.0.0.1 only."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from readystamp import numbering, tokens
from readystamp.ingest.dat import DELIMITERS, KEY_CANDIDATES, QUALIFIERS, DatError, pick_key_column, read_dat
from readystamp.ingest.loose import load_loose
from readystamp.ingest.opt import LoadError, load_opt
from readystamp.ingest.validate import report_to_csv
from readystamp.model import Production, Settings, ZoneId, readable_errors
from readystamp.output.export import preflight
from readystamp.presets import PresetError, from_preset, to_preset
from readystamp.server.jobs import JobBusy, JobManager
from readystamp.stamping.layout import validate_zones
from readystamp.stamping.preview import PreviewError, render_preview

STATIC_DIR = Path(__file__).resolve().parent / "static"
_SEVERITY_RANK = {"error": 3, "warning": 2, "info": 1}
_PRESET_NAME = re.compile(r"^[A-Za-z0-9 _.-]{1,60}$")


def _user_error(status: int, message: str | list[str]) -> HTTPException:
    messages = message if isinstance(message, list) else [message]
    return HTTPException(status_code=status, detail={"messages": messages})


class OptLoadRequest(BaseModel):
    opt_path: str
    dat_path: str | None = None
    dat_key: str | None = None
    dat_delimiter: str | None = None
    dat_qualifier: str | None = None


class OrderRequest(BaseModel):
    order: list[int] | None = None       # document seqs in their new order
    remove: list[int] = []


class PresetSaveRequest(BaseModel):
    name: str
    settings: dict | None = None         # defaults to the current settings


class State:
    def __init__(self, work_dir: Path, presets_dir: Path):
        self.work_dir = work_dir
        self.presets_dir = presets_dir
        self.production: Production | None = None
        self.settings = Settings()
        self.extra_inputs: list[Path] = []
        self.jobs = JobManager()
        self.last_job: str | None = None
        self.lock = threading.RLock()

    def replace_production(self, production: Production) -> None:
        """Swap in a new production and drop the previous one's uploaded copies (never user files)."""
        old = self.production
        self.production = production
        uploads = self.work_dir / "uploads"
        if old is not None and old.upload_root is not None and old.upload_root.parent == uploads:
            shutil.rmtree(old.upload_root, ignore_errors=True)

    def job_running(self) -> bool:
        snap = self.jobs.get(self.last_job) if self.last_job else None
        return bool(snap and snap["status"] in ("queued", "running"))


def create_app(work_dir: Path | None = None, presets_dir: Path | None = None) -> FastAPI:
    home = Path.home() / ".readystamp"
    state = State(Path(work_dir or home / "work"), Path(presets_dir or home / "presets"))
    # Uploaded copies only live as long as the server's in-memory production.
    shutil.rmtree(state.work_dir / "uploads", ignore_errors=True)
    state.work_dir.mkdir(parents=True, exist_ok=True)
    state.presets_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="ReadyStamp Lite", docs_url=None, redoc_url=None)
    app.state.rs = state

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"messages": [str(exc.detail)]}
        return JSONResponse(status_code=exc.status_code, content=detail)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        messages = []
        for err in exc.errors():
            where = " → ".join(str(p) for p in err["loc"] if p not in ("body", "query"))
            messages.append(f"{where}: {err['msg']}" if where else err["msg"])
        return JSONResponse(status_code=422, content={"messages": messages})

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"messages": [f"Something went wrong: {exc}"]})

    def need_production() -> Production:
        if state.production is None:
            raise _user_error(409, "Load documents first.")
        return state.production

    def not_while_running():
        if state.job_running():
            raise _user_error(409, "A run is in progress. Wait for it to finish or cancel it first.")

    # --- pages ----------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # --- loading ------------------------------------------------------------------------------------

    @app.post("/api/load/loose")
    async def load_loose_route(files: list[UploadFile] = File(...), paths: list[str] = Form(default=[])):
        not_while_running()
        target = state.work_dir / "uploads" / uuid.uuid4().hex[:12]
        target.mkdir(parents=True)
        names = paths if len(paths) == len(files) else [f.filename or "" for f in files]
        for f, name in zip(files, names):
            # Folder uploads carry relative paths; keep them, but never outside target.
            parts = [p for p in re.split(r"[\\/]+", name) if p not in ("", ".", "..")]
            if not parts:
                continue
            dest = target.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
        with state.lock:
            production = load_loose([target], volume_name=state.settings.output.volume_name)
            production.upload_root = target
            state.replace_production(production)
            state.extra_inputs = []
            _apply_numbering()
        return production_view()

    @app.post("/api/load/opt")
    def load_opt_route(req: OptLoadRequest):
        not_while_running()
        try:
            prod = load_opt(Path(req.opt_path).expanduser(), Path(req.dat_path).expanduser() if req.dat_path else None,
                            req.dat_key or None, req.dat_delimiter or None, req.dat_qualifier or None)
        except (LoadError, DatError) as exc:
            raise _user_error(400, str(exc))
        with state.lock:
            state.replace_production(prod)
            state.extra_inputs = [Path(req.opt_path).expanduser()] + ([Path(req.dat_path).expanduser()] if req.dat_path else [])
            if state.settings.numbering.mode == "keep" and prod.source_kind != "opt":
                state.settings.numbering.mode = "renumber"
            _apply_numbering()
        return production_view()

    @app.get("/api/dat/columns")
    def dat_columns(path: str, delimiter: str | None = None, qualifier: str | None = None):
        try:
            header, rows, _, _ = read_dat(Path(path).expanduser(), delimiter or None, qualifier or None)
        except (DatError, OSError) as exc:
            raise _user_error(400, str(exc))
        try:
            key = pick_key_column(header, None)
        except DatError:
            key = None
        return {"columns": header, "key": key, "rows": len(rows), "candidates": KEY_CANDIDATES,
                "delimiters": list(DELIMITERS), "qualifiers": list(QUALIFIERS)}

    @app.get("/api/fs/list")
    def fs_list(path: str | None = None, kind: str = "opt"):
        """Folders, plus files matching `kind` (opt, dat or dir for folders only)."""
        base = Path(path).expanduser() if path else Path.home()
        if not base.is_dir():
            raise _user_error(400, f"{base} is not a folder.")
        exts = {"opt": {".opt"}, "dat": {".dat", ".csv", ".txt"}, "dir": set()}.get(kind)
        if exts is None:
            raise _user_error(400, "kind must be opt, dat or dir.")
        dirs, files = [], []
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            raise _user_error(403, f"You do not have permission to open {base}.")
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.suffix.lower() in exts:
                    files.append(entry.name)
            except OSError:
                continue
        return {"path": str(base.resolve()), "parent": str(base.resolve().parent) if base.resolve().parent != base.resolve() else None,
                "dirs": dirs, "files": files}

    # --- production ------------------------------------------------------------------------------------

    def production_view() -> dict:
        prod = need_production()
        worst: dict[int, str] = {}
        for issue in prod.load_report.issues:
            if issue.doc_seq is not None and _SEVERITY_RANK[issue.severity] > _SEVERITY_RANK.get(worst.get(issue.doc_seq), 0):
                worst[issue.doc_seq] = issue.severity
        first_portrait = next(((d.seq, p.index_in_doc) for d in prod.documents for p in d.pages if not p.is_landscape), None)
        first_landscape = next(((d.seq, p.index_in_doc) for d in prod.documents for p in d.pages if p.is_landscape), None)
        return {
            "source_kind": prod.source_kind,
            "volume_name": prod.volume_name,
            "documents": [{
                "seq": d.seq, "source_name": d.source_name, "page_count": d.page_count,
                "original_bates": d.pages[0].original_bates if d.pages else None,
                "beg_bates": d.beg_bates, "end_bates": d.end_bates,
                "status": worst.get(d.seq, "ok"),
                "pages": [{"landscape": p.is_landscape, "color": p.is_color, "bates": p.new_bates,
                           "original_bates": p.original_bates} for p in d.pages],
            } for d in prod.documents],
            "totals": prod.totals(),
            "issues": [i.model_dump() for i in prod.load_report.issues],
            "has_errors": prod.load_report.has_errors,
            "dat_columns": prod.dat_columns,
            "jumps": {"first_portrait": first_portrait, "first_landscape": first_landscape},
        }

    @app.get("/api/production")
    def get_production():
        return production_view()

    @app.get("/api/production/report.csv")
    def report_csv():
        prod = need_production()
        return Response(content="﻿" + report_to_csv(prod.load_report), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="load_report.csv"'})

    @app.put("/api/production/order")
    def set_order(req: OrderRequest):
        not_while_running()
        prod = need_production()
        with state.lock:
            seqs = [d.seq for d in prod.documents]
            if req.order is not None:
                if sorted(req.order) != sorted(seqs):
                    raise _user_error(400, "The new order must list every document exactly once.")
                if prod.source_kind == "opt" and req.order != seqs:
                    raise _user_error(400, "An OPT production keeps the OPT's order; documents can only be removed.")
                by_seq = {d.seq: d for d in prod.documents}
                prod.documents = [by_seq[s] for s in req.order]
                old_to_new = {d.seq: n for n, d in enumerate(prod.documents, start=1)}
                for issue in prod.load_report.issues:
                    if issue.doc_seq is not None:
                        issue.doc_seq = old_to_new[issue.doc_seq]
                prod.renumber_seq()
                remove = {old_to_new[s] for s in req.remove if s in old_to_new}
            else:
                remove = set(req.remove)
            unknown = remove - {d.seq for d in prod.documents}
            if unknown:
                raise _user_error(400, f"There is no document {sorted(unknown)[0]}.")
            if remove:
                prod.remove_documents(remove)
            _apply_numbering()
        return production_view()

    # --- settings ----------------------------------------------------------------------------------------

    def _apply_numbering() -> numbering.NumberingPlan | None:
        """Assign Bates numbers for the current settings, if they are valid, so previews show them."""
        prod = state.production
        if prod is None:
            return None
        result = numbering.plan(prod, state.settings.numbering)
        if result.ok:
            numbering.apply(prod, state.settings.numbering)
        else:
            for p in prod.pages():
                p.new_bates = None
        return result

    def settings_view(field_errors: dict[str, list[str]] | None = None) -> dict:
        prod = state.production
        result = _apply_numbering() if prod else None
        zones = {z.value: msgs for z, msgs in validate_zones(state.settings, prod).items()} if prod else {}
        check = preflight(prod.model_copy(deep=True), state.settings) if prod else None
        return {
            "settings": json.loads(state.settings.model_dump_json()),
            "field_errors": field_errors or {},
            "numbering": {"problems": result.problems if result else [],
                          "sample": result.sample() if result and result.ok else None},
            "zones": zones,
            "preflight": {"errors": check.errors, "warnings": check.warnings,
                          "estimated_bytes": check.estimated_bytes} if check else None,
            "tokens": sorted(tokens.TOKENS) + ["FIELD:" + c for c in (prod.dat_columns if prod else [])],
        }

    @app.get("/api/settings")
    def get_settings():
        return settings_view()

    @app.put("/api/settings")
    def put_settings(body: dict):
        not_while_running()
        try:
            new = Settings.model_validate(body)
        except ValidationError as exc:
            errors: dict[str, list[str]] = {}
            for err, msg in zip(exc.errors(), readable_errors(exc)):
                key = ".".join(str(p) for p in err["loc"]) or "settings"
                errors.setdefault(key, []).append(msg)
            return JSONResponse(status_code=422, content=settings_view(errors) | {"ok": False})
        with state.lock:
            state.settings = new
            return settings_view() | {"ok": True}

    # --- preview -------------------------------------------------------------------------------------------

    @app.get("/api/preview")
    def preview(doc: int, page: int = 1, dpi: int = 100):
        prod = need_production()
        if not 20 <= dpi <= 300:
            raise _user_error(400, "dpi must be between 20 and 300.")
        if validate_zones(state.settings, prod):
            raise _user_error(400, "Fix the zone template errors to see the preview.")
        try:
            with state.lock:
                png = render_preview(prod, state.settings, doc, page, dpi)
        except PreviewError as exc:
            raise _user_error(404, str(exc))
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/locate")
    def locate(bates: str):
        prod = need_production()
        wanted = bates.strip().upper()
        for d in prod.documents:
            for p in d.pages:
                if (p.new_bates or "").upper() == wanted or (p.original_bates or "").upper() == wanted:
                    return {"doc": d.seq, "page": p.index_in_doc}
        raise _user_error(404, f"No page has the Bates number {bates}.")

    # --- runs ------------------------------------------------------------------------------------------------

    @app.post("/api/jobs")
    def start_job():
        prod = need_production()
        with state.lock:
            snapshot = prod.model_copy(deep=True)
            settings = state.settings.model_copy(deep=True)
        check = preflight(snapshot.model_copy(deep=True), settings)
        if not check.ok:
            raise _user_error(400, check.errors)
        try:
            job_id = state.jobs.start(snapshot, settings, extra_inputs=state.extra_inputs)
        except JobBusy as exc:
            raise _user_error(409, str(exc))
        state.last_job = job_id
        return {"id": job_id, "warnings": check.warnings}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        snap = state.jobs.get(job_id)
        if snap is None:
            raise _user_error(404, "There is no such run.")
        return snap

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        if state.jobs.get(job_id) is None:
            raise _user_error(404, "There is no such run.")
        return {"cancelled": state.jobs.cancel(job_id)}

    @app.get("/api/jobs/{job_id}/qc")
    def job_qc(job_id: str):
        snap = state.jobs.get(job_id)
        if not snap or not snap.get("qc_report"):
            raise _user_error(404, "This run has no QC report.")
        return FileResponse(snap["qc_report"], media_type="text/html")

    @app.post("/api/jobs/{job_id}/open")
    def open_output(job_id: str):
        snap = state.jobs.get(job_id)
        if not snap or not snap.get("output_dir"):
            raise _user_error(404, "This run has no output folder.")
        path = snap["output_dir"]
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return {"opened": False, "path": path}
        return {"opened": True, "path": path}

    # --- presets -----------------------------------------------------------------------------------------------

    @app.get("/api/presets")
    def list_presets():
        return {"presets": sorted(p.stem for p in state.presets_dir.glob("*.json"))}

    @app.post("/api/presets")
    def save_preset_route(req: PresetSaveRequest):
        if not _PRESET_NAME.match(req.name):
            raise _user_error(400, "Preset names may use letters, numbers, spaces, '-', '_' and '.' (up to 60).")
        try:
            settings = from_preset(req.settings) if req.settings is not None else state.settings
        except PresetError as exc:
            raise _user_error(400, exc.problems)
        path = state.presets_dir / f"{req.name}.json"
        path.write_text(json.dumps(to_preset(settings), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"saved": req.name}

    @app.get("/api/presets/{name}")
    def get_preset(name: str):
        if not _PRESET_NAME.match(name):
            raise _user_error(400, "That is not a valid preset name.")
        path = state.presets_dir / f"{name}.json"
        if not path.is_file():
            raise _user_error(404, f"There is no preset named {name}.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            settings = from_preset(data)
        except json.JSONDecodeError as exc:
            raise _user_error(400, f"The preset {name} is not valid JSON: {exc.msg}.")
        except PresetError as exc:
            raise _user_error(400, exc.problems)
        out = json.loads(settings.model_dump_json())
        out["output"]["output_dir"] = str(state.settings.output.output_dir) if state.settings.output.output_dir else None
        return {"settings": out}

    return app


def serve(port: int | None = None, open_browser: bool = True) -> None:
    """Start the server on 127.0.0.1 and open the default browser."""
    import socket
    import webbrowser

    import uvicorn

    if port is None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"ReadyStamp Lite is running at {url} (press Ctrl+C to stop)")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
