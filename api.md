# API Reference

Comprehensive reference for the PaperDock Proxy HTTP API.

- OpenAPI UI: `GET /docs`
- OpenAPI JSON: `GET /openapi.json`
- Default base URL: `http://<host>:8000`
- Content type: `application/json` unless noted otherwise

## Type Aliases

### `OptionPrimitive`

```text
string | integer | number | boolean
```

### `OptionValue`

```text
OptionPrimitive | OptionPrimitive[] | null
```

Used by both print and scan option maps so you can pass arbitrary backend-specific flags.

## Shared Schemas

### `PrintSettings`

```json
{
  "printer": "string | null",
  "title": "string | null",
  "copies": "integer >= 1 | null",
  "job_priority": "integer 1..100 | null",
  "page_ranges": "string | null",
  "options": { "key": "OptionValue" },
  "raw_args": ["string"],
  "timeout_seconds": "integer 5..3600"
}
```

### `PrintRequest`

`PrintSettings` + required `file_path`.

```json
{
  "file_path": "string",
  "printer": "string | null",
  "title": "string | null",
  "copies": "integer >= 1 | null",
  "job_priority": "integer 1..100 | null",
  "page_ranges": "string | null",
  "options": { "key": "OptionValue" },
  "raw_args": ["string"],
  "timeout_seconds": "integer 5..3600"
}
```

### `ScanRequest`

```json
{
  "device": "string | null",
  "format": "png | jpeg | tiff | pnm | null",
  "mode": "string | null",
  "resolution": "integer >= 1 | null",
  "options": { "key": "OptionValue" },
  "raw_args": ["string"],
  "output_filename": "string | null",
  "timeout_seconds": "integer 5..7200",
  "return_base64": "boolean"
}
```

### `CopyRequest`

```json
{
  "scan": "ScanRequest",
  "print_settings": "PrintSettings",
  "delete_scanned_file": "boolean"
}
```

### `RawCommandRequest`

```json
{
  "args": ["string"],
  "binary_output": "boolean | null",
  "timeout_seconds": "integer 5..3600"
}
```

`binary_output` is only used by `POST /scan/raw`.

## Error Schema

Non-2xx responses use FastAPI `HTTPException`:

```json
{
  "detail": "string | object"
}
```

For command failures, `detail` is usually:

```json
{
  "message": "Command failed",
  "command": ["string"],
  "return_code": 1,
  "stdout": "string",
  "stderr": "string"
}
```

## Scan Progress Stream Events

`POST /scan/progress` returns newline-delimited JSON (`application/x-ndjson`).

Each line is one event object. Event types:

- `started`: emitted once when scan command starts
- `progress`: emitted when `scanimage` stderr includes percentage text
- `log`: emitted for non-percentage stderr lines
- `completed`: emitted once on successful scan completion
- `error`: emitted once if timeout or scan command failure occurs

### Event Schema (Union)

```json
{
  "event": "started | progress | log | completed | error",
  "command": ["string"],
  "output_file": "string",
  "started_at_unix": 1741086300.123,
  "completed_at_unix": 1741086310.456,
  "timestamp_unix": 1741086305.789,
  "progress": 42.5,
  "message": "string",
  "return_code": 0,
  "bytes_written": 123456,
  "stderr": "string",
  "base64_data": "string (only when return_base64=true)"
}
```

Not every field appears on every event type.

## Endpoints

### `GET /`

Health-style root endpoint.

#### Response `200`

```json
{
  "service": "paperdock-proxy",
  "status": "ok"
}
```

---

### `GET /health`

#### Response `200`

```json
{
  "status": "healthy"
}
```

---

### `GET /print/printers`

Lists printers and default destination via `lpstat -p -d`.

#### Response `200`

```json
{
  "parsed": {
    "printers": [
      {
        "name": "string",
        "state": "string",
        "raw": "string"
      }
    ],
    "default_destination": "string | null"
  },
  "raw": "string",
  "stderr": "string"
}
```

---

### `GET /print/options`

Lists printer capabilities/options via `lpoptions -l`.

#### Query Params

| Name | Type | Required | Description |
|---|---|---|---|
| `printer` | string | No | CUPS printer name |

#### Response `200`

```json
{
  "printer": "string | null",
  "options": [
    {
      "name": "string",
      "label": "string",
      "choices": ["string"],
      "default": "string | null",
      "raw": "string"
    }
  ],
  "raw": "string",
  "stderr": "string"
}
```

---

### `GET /print/jobs`

Lists queued jobs using `lpstat -o`.

#### Query Params

| Name | Type | Required | Description |
|---|---|---|---|
| `printer` | string | No | Optional queue filter |

#### Response `200`

```json
{
  "printer": "string | null",
  "raw": "string",
  "stderr": "string"
}
```

---

### `POST /print/jobs`

Submits a print job from a local file path.

#### Request Body

`PrintRequest`

#### Response `200`

```json
{
  "command": ["string"],
  "return_code": 0,
  "job_id": "string | null",
  "stdout": "string",
  "stderr": "string"
}
```

---

### `POST /print/upload`

Uploads a file and prints it.

#### Content Type

`multipart/form-data`

#### Form Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | File to upload and print |
| `printer` | string | No | Target printer |
| `title` | string | No | Job title |
| `copies` | integer | No | Copy count |
| `job_priority` | integer | No | 1..100 |
| `page_ranges` | string | No | Example: `1-2,5` |
| `options_json` | string | No | JSON object; default `{}` |
| `raw_args_json` | string | No | JSON array; default `[]` |
| `timeout_seconds` | integer | No | Default `120` |

`options_json` example:

```json
{"media":"A4","sides":"one-sided"}
```

#### Response `200`

```json
{
  "command": ["string"],
  "return_code": 0,
  "job_id": "string | null",
  "stdout": "string",
  "stderr": "string",
  "uploaded_file": "string"
}
```

---

### `POST /print/jobs/{job_id}/cancel`

Cancels a queued job via `cancel <job_id>`.

#### Path Params

| Name | Type | Required |
|---|---|---|
| `job_id` | string | Yes |

#### Response `200`

```json
{
  "job_id": "string",
  "command": ["string"],
  "return_code": 0,
  "stdout": "string",
  "stderr": "string"
}
```

---

### `POST /print/raw`

Raw passthrough to `lp`. Use for advanced CUPS arguments not modeled elsewhere.

#### Request Body

`RawCommandRequest` (`binary_output` ignored)

#### Response `200`

```json
{
  "job_id": "string | null",
  "command": ["string"],
  "return_code": 0,
  "stdout": "string",
  "stderr": "string"
}
```

---

### `GET /scan/devices`

Lists scanner devices via `scanimage -L`.

#### Response `200`

```json
{
  "devices": [
    {
      "device": "string",
      "description": "string"
    }
  ],
  "raw": "string",
  "stderr": "string"
}
```

---

### `GET /scan/options`

Lists scanner flags/options for generic or device-specific backend.

#### Query Params

| Name | Type | Required | Description |
|---|---|---|---|
| `device` | string | No | `scanimage` device URI |
| `all_options` | boolean | No | Default `true`; adds `--all-options` |

#### Response `200`

```json
{
  "device": "string | null",
  "all_options": true,
  "flags": ["--flag-name"],
  "raw": "string",
  "stderr": "string"
}
```

---

### `POST /scan`

Scans a single document. Output is written to a file.

#### Request Body

`ScanRequest`

#### Response `200` (single output mode)

```json
{
  "command": ["string"],
  "return_code": 0,
  "batch_mode": false,
  "output_file": "string",
  "bytes_written": 123456,
  "stderr": "string",
  "base64_data": "string (optional; only when return_base64=true)"
}
```

#### Response `200` (batch mode)

If `raw_args` or options include `--batch`, files are written by `scanimage`.

```json
{
  "command": ["string"],
  "return_code": 0,
  "batch_mode": true,
  "stdout": "string",
  "stderr": "string",
  "note": "Batch mode enabled. Files are written by scanimage according to --batch arguments."
}
```

---

### `POST /scan/progress`

Scans document and streams progress/log events while scan is running.

#### Request Body

`ScanRequest`

#### Response `200`

`application/x-ndjson` stream.

Each line is a JSON event:

```json
{"event":"started","command":["scanimage","--format=png"],"output_file":"/tmp/paperdock-proxy/scan-abc.png","started_at_unix":1741086300.12}
{"event":"progress","progress":17.4,"message":"Progress: 17.4%","timestamp_unix":1741086302.10}
{"event":"progress","progress":56.1,"message":"Progress: 56.1%","timestamp_unix":1741086304.30}
{"event":"completed","command":["scanimage","--format=png"],"return_code":0,"output_file":"/tmp/paperdock-proxy/scan-abc.png","bytes_written":983421,"stderr":"...","started_at_unix":1741086300.12,"completed_at_unix":1741086308.67}
```

#### Notes

- API automatically adds `--progress` to `scanimage` command unless you already passed `-p/--progress` in `raw_args`.
- Batch mode (`--batch`) is not supported for this endpoint and returns `400`.
- On failure or timeout, stream ends with an `error` event.
- Some backends still do not emit percentage lines; in that case you will receive `started`, a final informational `log`, and `completed`.
- Use `curl -N` (or equivalent) to disable output buffering in client.

---

### `POST /scan/download`

Scans and returns the output as downloadable file response.

#### Request Body

`ScanRequest`

#### Response `200`

Binary file stream (`FileResponse`) with inferred `Content-Type`.

#### Notes

- This endpoint deletes the temporary output file after response is sent.
- Batch scans are rejected with `400`.

---

### `POST /scan/raw`

Raw passthrough to `scanimage`.

#### Request Body

`RawCommandRequest`

`binary_output` behavior:

- `null` (default): API auto-detects based on text flags (`--help`, `-L`, `-A`, etc.).
- `true`: treat stdout as binary scan data and save to a file.
- `false`: treat stdout as text.

#### Response `200` (binary mode)

```json
{
  "command": ["string"],
  "return_code": 0,
  "output_file": "string",
  "bytes_written": 123456,
  "stderr": "string"
}
```

#### Response `200` (text mode)

```json
{
  "command": ["string"],
  "return_code": 0,
  "stdout": "string",
  "stderr": "string"
}
```

---

### `POST /copy`

Photocopy flow: scan document, then print scanned output.

#### Request Body

`CopyRequest`

#### Response `200`

```json
{
  "scan": {
    "command": ["string"],
    "return_code": 0,
    "batch_mode": false,
    "output_file": "string",
    "bytes_written": 123456,
    "stderr": "string"
  },
  "print": {
    "command": ["string"],
    "return_code": 0,
    "job_id": "string | null",
    "stdout": "string",
    "stderr": "string"
  },
  "scanned_file_deleted": true
}
```

#### Notes

- Copy endpoint does not support scan batch mode.
- Temporary scan file is deleted when `delete_scanned_file=true`.
