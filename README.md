# PaperDock Proxy

FastAPI service exposing HTTP endpoints for CUPS (`lp`, `lpstat`, `lpoptions`, `cancel`) and SANE (`scanimage`).

## 1) Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Swagger UI:

- http://localhost:8000/docs

## 3) Key Endpoints

- `GET /print/printers`: List CUPS printers (`lpstat -p -d`)
- `GET /print/options?printer=<name>`: Printer capabilities (`lpoptions -p <name> -l`)
- `POST /print/jobs`: Print an existing file path
- `POST /print/upload`: Upload a file and print it
- `POST /print/raw`: Raw `lp` args passthrough (full CUPS feature access)
- `POST /print/jobs/{job_id}/cancel`: Cancel print job
- `GET /scan/devices`: List scanners (`scanimage -L`)
- `GET /scan/options?device=<uri>&all_options=true`: Scan capabilities (`scanimage --help --all-options`)
- `POST /scan`: Scan to a saved file
- `POST /scan/download`: Scan and immediately download the output
- `POST /scan/raw`: Raw `scanimage` args passthrough (full SANE feature access)
- `POST /copy`: Scan then print (photocopy workflow)

## 4) Examples

List printers:

```bash
curl -s http://localhost:8000/print/printers | jq
```

List scan devices:

```bash
curl -s http://localhost:8000/scan/devices | jq
```

Scan to PNG:

```bash
curl -s -X POST http://localhost:8000/scan \
  -H 'Content-Type: application/json' \
  -d '{
    "device":"hpaio:/usb/HP_LaserJet_MFP_M129-M134?serial=VNFVY57093",
    "format":"png",
    "resolution":300,
    "options":{"source":"Flatbed","mode":"Gray"}
  }' | jq
```

Print an existing file:

```bash
curl -s -X POST http://localhost:8000/print/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "file_path":"/home/zeeshan/test.pdf",
    "printer":"HP-LaserJet-MFP-M129-M134",
    "copies":1,
    "options":{"media":"A4","sides":"one-sided"}
  }' | jq
```

Photocopy:

```bash
curl -s -X POST http://localhost:8000/copy \
  -H 'Content-Type: application/json' \
  -d '{
    "scan":{
      "device":"hpaio:/usb/HP_LaserJet_MFP_M129-M134?serial=VNFVY57093",
      "format":"png",
      "resolution":300,
      "options":{"source":"Flatbed","mode":"Gray"}
    },
    "print_settings":{
      "printer":"HP-LaserJet-MFP-M129-M134",
      "copies":1,
      "options":{"media":"A4","sides":"one-sided"}
    }
  }' | jq
```

Raw CUPS passthrough:

```bash
curl -s -X POST http://localhost:8000/print/raw \
  -H 'Content-Type: application/json' \
  -d '{
    "args":["-d","HP-LaserJet-MFP-M129-M134","-o","media=A4","/home/zeeshan/test.pdf"]
  }' | jq
```

Raw SANE passthrough:

```bash
curl -s -X POST http://localhost:8000/scan/raw \
  -H 'Content-Type: application/json' \
  -d '{
    "args":["--device-name","hpaio:/usb/HP_LaserJet_MFP_M129-M134?serial=VNFVY57093","--format=png","--resolution","300"]
  }' | jq
```

## Notes

- Raw endpoints let you pass scanner/printer-specific flags not explicitly modeled by structured fields.
- Scan outputs are written to `/tmp/paperdock-proxy` by default. Override with `PAPERDOCK_DATA_DIR`.
- The service assumes local OS-level permission to access USB printer/scanner devices.
