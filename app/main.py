from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import selectors
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

OptionPrimitive = Union[str, int, float, bool]
OptionValue = Union[OptionPrimitive, List[OptionPrimitive], None]

DEFAULT_TIMEOUT = 120
DEFAULT_SCAN_TIMEOUT = 300
ARTIFACTS_DIR = Path(os.getenv("PAPERDOCK_DATA_DIR", "/tmp/paperdock-proxy")).resolve()
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


class PrintSettings(BaseModel):
    printer: Optional[str] = None
    title: Optional[str] = None
    copies: Optional[int] = Field(default=None, ge=1)
    job_priority: Optional[int] = Field(default=None, ge=1, le=100)
    page_ranges: Optional[str] = None
    options: Dict[str, OptionValue] = Field(default_factory=dict)
    raw_args: List[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT, ge=5, le=3600)


class PrintRequest(PrintSettings):
    file_path: str


class ScanRequest(BaseModel):
    device: Optional[str] = None
    format: Optional[str] = Field(default=None, description="png, jpeg, tiff, pnm")
    mode: Optional[str] = None
    resolution: Optional[int] = Field(default=None, ge=1)
    options: Dict[str, OptionValue] = Field(default_factory=dict)
    raw_args: List[str] = Field(default_factory=list)
    output_filename: Optional[str] = None
    timeout_seconds: int = Field(default=DEFAULT_SCAN_TIMEOUT, ge=5, le=7200)
    return_base64: bool = False


class CopyRequest(BaseModel):
    scan: ScanRequest = Field(default_factory=ScanRequest)
    print_settings: PrintSettings = Field(default_factory=PrintSettings)
    delete_scanned_file: bool = True


class RawCommandRequest(BaseModel):
    args: List[str] = Field(default_factory=list)
    binary_output: Optional[bool] = Field(
        default=None,
        description="Only for /scan/raw. If omitted, service auto-detects text vs binary output.",
    )
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT, ge=5, le=3600)


def model_to_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()  # type: ignore[attr-defined]
    return model.dict()  # type: ignore[no-any-return]


def resolve_output_path(filename: Optional[str], suffix: str) -> Path:
    if filename:
        output = Path(filename)
        if not output.is_absolute():
            output = (ARTIFACTS_DIR / output).resolve()
            if not str(output).startswith(str(ARTIFACTS_DIR)):
                raise HTTPException(status_code=400, detail="Relative output path escapes artifact directory.")
        else:
            output = output.resolve()
    else:
        output = ARTIFACTS_DIR / f"scan-{uuid.uuid4().hex}{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def run_command(args: List[str], timeout_seconds: int, binary: bool = False) -> Dict[str, Any]:
    if not args:
        raise HTTPException(status_code=400, detail="Command is empty.")

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=not binary,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Command not found: {args[0]}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Command timed out after {timeout_seconds}s",
        ) from exc

    if binary:
        stdout_bytes = completed.stdout or b""
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = (completed.stderr or b"").decode("utf-8", errors="replace")
    else:
        stdout_bytes = b""
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""

    result = {
        "command": args,
        "return_code": completed.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_bytes": stdout_bytes,
    }

    if completed.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Command failed",
                "command": args,
                "return_code": completed.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
            },
        )
    return result


def parse_scan_devices(scanimage_output: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for line in scanimage_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("device "):
            continue
        if " is a " not in stripped:
            continue

        device_part, description = stripped[len("device ") :].split(" is a ", 1)
        device = device_part.strip().strip("`'\"")
        description = description.strip()
        if not device:
            continue

        devices.append(
            {
                "device": device,
                "description": description,
            }
        )
    return devices


def parse_lpstat_printers(lpstat_output: str) -> Dict[str, Any]:
    printers: List[Dict[str, Any]] = []
    default_destination: Optional[str] = None

    for line in lpstat_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("printer "):
            # Example:
            # printer HP-LaserJet is idle.  enabled since Wed 04 Mar 2026 14:47:40 PKT
            parts = stripped.split()
            name = parts[1] if len(parts) > 1 else ""
            state = "unknown"
            if " is " in stripped:
                state = stripped.split(" is ", 1)[1].split(".", 1)[0]
            printers.append({"name": name, "state": state, "raw": stripped})
        elif stripped.startswith("system default destination:"):
            default_destination = stripped.split(":", 1)[1].strip()

    return {"printers": printers, "default_destination": default_destination}


def parse_lpoptions_text(options_text: str) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for line in options_text.splitlines():
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        left = left.strip()
        right = right.strip()
        if not left:
            continue

        if "/" in left:
            name, label = left.split("/", 1)
        else:
            name, label = left, ""

        values = []
        default_value = None
        for token in right.split():
            is_default = token.startswith("*")
            value = token[1:] if is_default else token
            values.append(value)
            if is_default:
                default_value = value

        parsed.append(
            {
                "name": name,
                "label": label,
                "choices": values,
                "default": default_value,
                "raw": line,
            }
        )
    return parsed


def extract_scan_flags(help_text: str) -> List[str]:
    flags: List[str] = []
    seen = set()
    pattern = re.compile(r"(--[a-zA-Z0-9][a-zA-Z0-9-]*)")
    for line in help_text.splitlines():
        for match in pattern.findall(line):
            if match not in seen:
                seen.add(match)
                flags.append(match)
    return flags


def build_scan_options(options: Dict[str, OptionValue]) -> List[str]:
    args: List[str] = []
    for key, value in options.items():
        if not key:
            continue
        flag = key if key.startswith("-") else f"--{key}"

        if isinstance(value, list):
            for item in value:
                args.extend(build_scan_options({key: item}))
            continue

        if value is None:
            args.append(flag)
            continue

        if isinstance(value, bool):
            if value:
                args.append(flag)
            continue

        if flag.startswith("--"):
            args.append(f"{flag}={value}")
        else:
            args.extend([flag, str(value)])
    return args


def build_lp_options(options: Dict[str, OptionValue]) -> List[str]:
    args: List[str] = []
    for key, value in options.items():
        if not key:
            continue

        if isinstance(value, list):
            for item in value:
                args.extend(build_lp_options({key: item}))
            continue

        if value is None:
            args.extend(["-o", key])
            continue

        if isinstance(value, bool):
            if value:
                args.extend(["-o", key])
            continue

        args.extend(["-o", f"{key}={value}"])
    return args


def build_lp_command(request: PrintRequest) -> List[str]:
    cmd = ["lp"]

    if request.printer:
        cmd.extend(["-d", request.printer])
    if request.title:
        cmd.extend(["-t", request.title])
    if request.copies is not None:
        cmd.extend(["-n", str(request.copies)])
    if request.job_priority is not None:
        cmd.extend(["-q", str(request.job_priority)])
    if request.page_ranges:
        cmd.extend(["-P", request.page_ranges])

    cmd.extend(build_lp_options(request.options))
    cmd.extend(request.raw_args)
    cmd.append(request.file_path)
    return cmd


def parse_lp_job_id(lp_output: str) -> Optional[str]:
    # Typical CUPS output: "request id is printer-123 (1 file(s))"
    match = re.search(r"request id is ([^\s]+)", lp_output)
    return match.group(1) if match else None


def guess_suffix(scan_format: Optional[str]) -> str:
    fmt = (scan_format or "pnm").lower()
    if fmt in {"jpg", "jpeg"}:
        return ".jpg"
    if fmt in {"tif", "tiff"}:
        return ".tiff"
    if fmt == "png":
        return ".png"
    return ".pnm"


def build_scan_command(request: ScanRequest) -> List[str]:
    cmd = ["scanimage"]
    if request.device:
        cmd.extend(["--device-name", request.device])
    if request.format:
        cmd.append(f"--format={request.format}")
    if request.mode:
        cmd.extend(["--mode", request.mode])
    if request.resolution:
        cmd.extend(["--resolution", str(request.resolution)])
    cmd.extend(build_scan_options(request.options))
    cmd.extend(request.raw_args)
    return cmd


def is_batch_scan_command(cmd: List[str]) -> bool:
    return any(arg == "--batch" or arg.startswith("--batch=") for arg in cmd)


def ensure_progress_flag(cmd: List[str]) -> List[str]:
    if "-p" in cmd or "--progress" in cmd:
        return cmd
    # Inject scanimage's generic progress flag right after binary name.
    return [cmd[0], "--progress", *cmd[1:]]


def extract_progress_values(stderr_line: str) -> List[float]:
    values: List[float] = []
    for match in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%", stderr_line):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if 0.0 <= value <= 100.0:
            values.append(value)
    return values


def as_ndjson_event(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True) + "\n"


def stream_scan_with_progress(request: ScanRequest) -> Iterator[str]:
    cmd = ensure_progress_flag(build_scan_command(request))
    if is_batch_scan_command(cmd):
        raise HTTPException(
            status_code=400,
            detail="Progress streaming does not support batch scan mode. Remove --batch options.",
        )

    output_path = resolve_output_path(request.output_filename, guess_suffix(request.format))
    started_at = time.time()

    process: Optional[subprocess.Popen[bytes]] = None
    selector: Optional[selectors.BaseSelector] = None
    stdout_chunks = bytearray()
    stderr_chunks: List[str] = []
    stderr_line_buffer = ""
    last_progress: Optional[float] = None
    progress_emitted = False
    timed_out = False

    def emit_stderr_line(line: str) -> Iterator[str]:
        nonlocal last_progress, progress_emitted
        stripped = line.strip()
        if not stripped:
            return

        progress_values = extract_progress_values(stripped)
        if progress_values:
            for progress in progress_values:
                if last_progress is None or progress != last_progress:
                    last_progress = progress
                    yield as_ndjson_event(
                        {
                            "event": "progress",
                            "progress": progress,
                            "message": stripped,
                            "timestamp_unix": time.time(),
                        }
                    )
                    progress_emitted = True
            return

        yield as_ndjson_event(
            {
                "event": "log",
                "message": stripped,
                "timestamp_unix": time.time(),
            }
        )

    try:
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=f"Command not found: {cmd[0]}") from exc

        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise HTTPException(status_code=500, detail="Failed to capture scanimage stdout/stderr.")

        # Tell clients immediately which command/output is being used.
        yield as_ndjson_event(
            {
                "event": "started",
                "command": cmd,
                "output_file": str(output_path),
                "started_at_unix": started_at,
            }
        )

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

        deadline = time.monotonic() + request.timeout_seconds
        while selector.get_map():
            if time.monotonic() > deadline:
                timed_out = True
                process.kill()
                break

            events = selector.select(timeout=0.2)
            if not events:
                continue

            for key, _ in events:
                stream = key.fileobj
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    selector.unregister(stream)
                    continue

                if key.data == "stdout":
                    stdout_chunks.extend(chunk)
                    continue

                stderr_text_chunk = chunk.decode("utf-8", errors="replace")
                stderr_chunks.append(stderr_text_chunk)
                stderr_line_buffer += stderr_text_chunk
                parts = re.split(r"[\r\n]+", stderr_line_buffer)
                stderr_line_buffer = parts.pop() if parts else ""
                for part in parts:
                    yield from emit_stderr_line(part)

        # Emit any residual stderr text that wasn't newline-terminated.
        yield from emit_stderr_line(stderr_line_buffer)

        return_code = process.wait()
        stderr_text = "".join(stderr_chunks)

        if timed_out:
            output_path.unlink(missing_ok=True)
            yield as_ndjson_event(
                {
                    "event": "error",
                    "message": f"Command timed out after {request.timeout_seconds}s",
                    "command": cmd,
                    "return_code": None,
                    "stderr": stderr_text,
                }
            )
            return

        if return_code != 0:
            output_path.unlink(missing_ok=True)
            yield as_ndjson_event(
                {
                    "event": "error",
                    "message": "Command failed",
                    "command": cmd,
                    "return_code": return_code,
                    "stderr": stderr_text,
                }
            )
            return

        if not progress_emitted:
            yield as_ndjson_event(
                {
                    "event": "log",
                    "message": (
                        "No percentage progress was emitted by scanimage/backend. "
                        "The scan still completed successfully."
                    ),
                    "timestamp_unix": time.time(),
                }
            )

        output_path.write_bytes(bytes(stdout_chunks))
        completed_payload: Dict[str, Any] = {
            "event": "completed",
            "command": cmd,
            "return_code": return_code,
            "output_file": str(output_path),
            "bytes_written": len(stdout_chunks),
            "stderr": stderr_text,
            "started_at_unix": started_at,
            "completed_at_unix": time.time(),
        }
        if request.return_base64:
            completed_payload["base64_data"] = base64.b64encode(bytes(stdout_chunks)).decode("ascii")
        yield as_ndjson_event(completed_payload)
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def execute_scan(request: ScanRequest) -> Dict[str, Any]:
    cmd = build_scan_command(request)
    is_batch_mode = is_batch_scan_command(cmd)

    if is_batch_mode:
        result = run_command(cmd, timeout_seconds=request.timeout_seconds, binary=False)
        return {
            "command": cmd,
            "return_code": result["return_code"],
            "batch_mode": True,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "note": "Batch mode enabled. Files are written by scanimage according to --batch arguments.",
        }

    result = run_command(cmd, timeout_seconds=request.timeout_seconds, binary=True)
    payload = result["stdout_bytes"]
    output_path = resolve_output_path(request.output_filename, guess_suffix(request.format))
    output_path.write_bytes(payload)

    response: Dict[str, Any] = {
        "command": cmd,
        "return_code": result["return_code"],
        "batch_mode": False,
        "output_file": str(output_path),
        "bytes_written": len(payload),
        "stderr": result["stderr"],
    }
    if request.return_base64:
        response["base64_data"] = base64.b64encode(payload).decode("ascii")
    return response


def submit_print_job(request: PrintRequest) -> Dict[str, Any]:
    source = Path(request.file_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {request.file_path}")

    cmd = build_lp_command(request)
    result = run_command(cmd, timeout_seconds=request.timeout_seconds, binary=False)
    job_id = parse_lp_job_id(result["stdout"])
    return {
        "command": cmd,
        "return_code": result["return_code"],
        "job_id": job_id,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


app = FastAPI(
    title="PaperDock Printer & Scanner API",
    description=(
        "HTTP API around CUPS lp/lpstat/lpoptions and SANE scanimage commands for "
        "print, scan, and photocopy workflows."
    ),
    version="0.1.0",
)


@app.get("/")
def root() -> Dict[str, str]:
    return {"service": "paperdock-proxy", "status": "ok"}


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "healthy"}


@app.get("/print/printers")
def list_printers() -> Dict[str, Any]:
    result = run_command(["lpstat", "-p", "-d"], timeout_seconds=DEFAULT_TIMEOUT, binary=False)
    parsed = parse_lpstat_printers(result["stdout"])
    return {"parsed": parsed, "raw": result["stdout"], "stderr": result["stderr"]}


@app.get("/print/options")
def list_printer_options(
    printer: Optional[str] = Query(default=None, description="CUPS printer name"),
) -> Dict[str, Any]:
    cmd = ["lpoptions"]
    if printer:
        cmd.extend(["-p", printer])
    cmd.append("-l")

    result = run_command(cmd, timeout_seconds=DEFAULT_TIMEOUT, binary=False)
    parsed = parse_lpoptions_text(result["stdout"])
    return {"printer": printer, "options": parsed, "raw": result["stdout"], "stderr": result["stderr"]}


@app.get("/print/jobs")
def list_print_jobs(
    printer: Optional[str] = Query(default=None, description="Filter queue by printer name"),
) -> Dict[str, Any]:
    cmd = ["lpstat", "-o"]
    if printer:
        cmd.append(printer)
    result = run_command(cmd, timeout_seconds=DEFAULT_TIMEOUT, binary=False)
    return {"printer": printer, "raw": result["stdout"], "stderr": result["stderr"]}


@app.post("/print/jobs")
def create_print_job(request: PrintRequest) -> Dict[str, Any]:
    return submit_print_job(request)


@app.post("/print/upload")
async def create_print_job_from_upload(
    file: UploadFile = File(...),
    printer: Optional[str] = Form(default=None),
    title: Optional[str] = Form(default=None),
    copies: Optional[int] = Form(default=None),
    job_priority: Optional[int] = Form(default=None),
    page_ranges: Optional[str] = Form(default=None),
    options_json: str = Form(default="{}"),
    raw_args_json: str = Form(default="[]"),
    timeout_seconds: int = Form(default=DEFAULT_TIMEOUT),
) -> Dict[str, Any]:
    try:
        parsed_options = json.loads(options_json)
        parsed_raw_args = json.loads(raw_args_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON in options_json/raw_args_json") from exc

    if not isinstance(parsed_options, dict):
        raise HTTPException(status_code=400, detail="options_json must decode to an object.")
    if not isinstance(parsed_raw_args, list):
        raise HTTPException(status_code=400, detail="raw_args_json must decode to an array.")

    upload_suffix = Path(file.filename or "").suffix
    saved_file = ARTIFACTS_DIR / f"upload-{uuid.uuid4().hex}{upload_suffix}"
    with saved_file.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    print_request = PrintRequest(
        file_path=str(saved_file),
        printer=printer,
        title=title,
        copies=copies,
        job_priority=job_priority,
        page_ranges=page_ranges,
        options=parsed_options,
        raw_args=[str(arg) for arg in parsed_raw_args],
        timeout_seconds=timeout_seconds,
    )
    response = submit_print_job(print_request)
    response["uploaded_file"] = str(saved_file)
    return response


@app.post("/print/jobs/{job_id}/cancel")
def cancel_print_job(job_id: str) -> Dict[str, Any]:
    result = run_command(["cancel", job_id], timeout_seconds=DEFAULT_TIMEOUT, binary=False)
    return {
        "job_id": job_id,
        "command": result["command"],
        "return_code": result["return_code"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


@app.post("/print/raw")
def raw_lp_command(request: RawCommandRequest) -> Dict[str, Any]:
    cmd = ["lp", *request.args]
    result = run_command(cmd, timeout_seconds=request.timeout_seconds, binary=False)
    job_id = parse_lp_job_id(result["stdout"])
    return {
        "job_id": job_id,
        "command": cmd,
        "return_code": result["return_code"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


@app.get("/scan/devices")
def list_scan_devices() -> Dict[str, Any]:
    result = run_command(["scanimage", "-L"], timeout_seconds=DEFAULT_TIMEOUT, binary=False)
    devices = parse_scan_devices(result["stdout"])
    return {"devices": devices, "raw": result["stdout"], "stderr": result["stderr"]}


@app.get("/scan/options")
def list_scan_options(
    device: Optional[str] = Query(default=None, description="scanimage device URI"),
    all_options: bool = Query(default=True, description="Include backend-specific options"),
) -> Dict[str, Any]:
    cmd = ["scanimage", "--help"]
    if device:
        cmd.extend(["--device-name", device])
    if all_options:
        cmd.append("--all-options")

    result = run_command(cmd, timeout_seconds=DEFAULT_TIMEOUT, binary=False)
    return {
        "device": device,
        "all_options": all_options,
        "flags": extract_scan_flags(result["stdout"]),
        "raw": result["stdout"],
        "stderr": result["stderr"],
    }


@app.post("/scan")
def scan_document(request: ScanRequest) -> Dict[str, Any]:
    return execute_scan(request)


@app.post("/scan/progress")
def scan_document_with_progress(request: ScanRequest) -> StreamingResponse:
    return StreamingResponse(
        stream_scan_with_progress(request),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/scan/download")
def scan_document_download(request: ScanRequest) -> FileResponse:
    response = execute_scan(request)
    output_file = response.get("output_file")
    if not output_file:
        raise HTTPException(
            status_code=400,
            detail="Batch scans cannot be downloaded through this endpoint. Use /scan and --batch output files.",
        )

    output_path = Path(output_file)
    media_type, _ = mimetypes.guess_type(str(output_path))
    return FileResponse(
        path=str(output_path),
        filename=output_path.name,
        media_type=media_type or "application/octet-stream",
        background=BackgroundTask(lambda: output_path.unlink(missing_ok=True)),
    )


@app.post("/scan/raw")
def raw_scanimage_command(request: RawCommandRequest) -> Dict[str, Any]:
    cmd = ["scanimage", *request.args]
    if request.binary_output is None:
        text_flags = {
            "-h",
            "--help",
            "-L",
            "--list-devices",
            "-A",
            "--all-options",
            "-f",
            "--formatted-device-list",
        }
        is_binary = not any(flag in request.args for flag in text_flags)
    else:
        is_binary = request.binary_output
    result = run_command(cmd, timeout_seconds=request.timeout_seconds, binary=is_binary)

    if is_binary:
        output_path = resolve_output_path(None, ".pnm")
        output_path.write_bytes(result["stdout_bytes"])
        return {
            "command": cmd,
            "return_code": result["return_code"],
            "output_file": str(output_path),
            "bytes_written": len(result["stdout_bytes"]),
            "stderr": result["stderr"],
        }

    return {
        "command": cmd,
        "return_code": result["return_code"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


@app.post("/copy")
def photocopy(request: CopyRequest) -> Dict[str, Any]:
    scan_payload = model_to_dict(request.scan)
    scan_payload["return_base64"] = False
    scan_payload["output_filename"] = str(
        ARTIFACTS_DIR / f"copy-{uuid.uuid4().hex}{guess_suffix(request.scan.format)}"
    )

    scan_request = ScanRequest(**scan_payload)
    scan_result = execute_scan(scan_request)
    scanned_file = scan_result.get("output_file")
    if not scanned_file:
        raise HTTPException(
            status_code=400,
            detail="Photocopy does not support scan batch mode. Remove --batch options for /copy.",
        )

    print_payload = model_to_dict(request.print_settings)
    print_request = PrintRequest(file_path=scanned_file, **print_payload)
    print_result = submit_print_job(print_request)

    deleted = False
    if request.delete_scanned_file:
        temp_path = Path(scanned_file)
        temp_path.unlink(missing_ok=True)
        deleted = True

    return {
        "scan": scan_result,
        "print": print_result,
        "scanned_file_deleted": deleted,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
