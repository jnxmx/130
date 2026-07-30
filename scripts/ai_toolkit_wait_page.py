#!/usr/bin/env python3
import argparse
import http.server
import json
import mimetypes
import re
import socketserver
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOG_TAIL_BYTES = 120_000
LOCAL_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR_CANDIDATES = [
    Path("/usr/share/nginx/html/assets"),
    LOCAL_PROJECT_ROOT / "assets",
]


def _regex(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


def _detail_repo(line: str, current: str) -> str:
    if "cloning" in line.lower():
        return "Cloning fresh repo"
    if "updating repo" in line.lower():
        return "Updating existing repo"
    return current


def _detail_ui(line: str, current: str) -> str:
    if "installing ui dependencies" in line.lower():
        return "Installing npm dependencies"
    if "building ui" in line.lower():
        return "Building UI bundle"
    if "update_db" in line.lower():
        return "Updating database schema"
    return current


AITK_STAGE_DEFS = [
    {
        "id": "repo",
        "label": "Repository",
        "detail": "Cloning ai-toolkit source",
        "patterns": [
            _regex(r"\[ai-toolkit].*cloning repo"),
            _regex(r"\[ai-toolkit].*updating repo"),
        ],
        "detail_from_line": _detail_repo,
    },
    {
        "id": "venv",
        "label": "Python environment",
        "detail": "Creating virtualenv and upgrading pip",
        "patterns": [
            _regex(r"\[ai-toolkit].*creating venv"),
            _regex(r"pip install --upgrade pip"),
        ],
    },
    {
        "id": "deps",
        "label": "Python deps",
        "detail": "Installing requirements",
        "patterns": [
            _regex(r"\[ai-toolkit].*installing requirements"),
        ],
    },
    {
        "id": "ui-build",
        "label": "UI build",
        "detail": "Preparing AI Toolkit UI",
        "patterns": [
            _regex(r"installing UI dependencies"),
            _regex(r"npm run build"),
            _regex(r"npm run update_db"),
        ],
        "detail_from_line": _detail_ui,
    },
    {
        "id": "start",
        "label": "UI startup",
        "detail": "Starting ai-toolkit on :8675",
        "patterns": [
            _regex(r"\[ai-toolkit].*starting UI"),
        ],
    },
]


def tail_lines(path: str, max_bytes: int = LOG_TAIL_BYTES):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(size - max_bytes, 0))
            data = handle.read().decode("utf-8", errors="ignore")
            return data.splitlines()
    except OSError:
        return []


def _resolve_asset_file(request_path: str):
    marker = "/assets/"
    if marker not in request_path:
        return None
    relative = request_path.split(marker, 1)[1]
    if not relative:
        return None
    relative_path = Path(relative)
    for root in ASSET_DIR_CANDIDATES:
        try:
            if not root.exists():
                continue
            root_resolved = root.resolve()
            candidate = (root_resolved / relative_path).resolve()
            candidate.relative_to(root_resolved)
            if candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


def compute_stage_states(lines):
    stages = [
        {
            "id": definition["id"],
            "label": definition["label"],
            "detail": definition.get("detail", ""),
            "status": "pending",
        }
        for definition in AITK_STAGE_DEFS
    ]
    current_idx = None
    for line in lines:
        for idx, definition in enumerate(AITK_STAGE_DEFS):
            if any(pattern.search(line) for pattern in definition["patterns"]):
                current_idx = idx
                detail_fn = definition.get("detail_from_line")
                if detail_fn:
                    new_detail = detail_fn(line, stages[idx]["detail"])
                    if new_detail:
                        stages[idx]["detail"] = new_detail
                break
    if current_idx is None:
        if stages:
            stages[0]["status"] = "active"
        return stages
    for idx, stage in enumerate(stages):
        if idx < current_idx:
            stage["status"] = "done"
        elif idx == current_idx:
            stage["status"] = "active"
        else:
            stage["status"] = "pending"
    return stages


def build_state_payload(log_path: str):
    lines = tail_lines(log_path)
    return {
        "timestamp": time.time(),
        "stages": compute_stage_states(lines),
    }


HTML_PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Toolkit is installing…</title>
  <style>
    @font-face { font-family: 'Inter'; font-style: normal; font-weight: 100 900; font-display: swap; src: url('/assets/fonts/InterVariable.woff2') format('woff2'); }
    @font-face { font-family: 'Inter'; font-style: italic; font-weight: 100 900; font-display: swap; src: url('/assets/fonts/InterVariable-Italic.woff2') format('woff2'); }
    html, body { margin: 0; padding: 0; min-height: 100%; background: #d8cec9; color: #0c4eb9; font-family: 'Inter', sans-serif; }
    body { display: flex; }
    .wrapper { max-width: 760px; margin: 0 auto; padding: 2rem 1.5rem 2.5rem; display: flex; flex-direction: column; gap: 1.4rem; width: 100%; }
    h1 { margin: 0; font-size: 2rem; font-weight: 600; }
    .linkline { font-size: 0.95rem; letter-spacing: 0.08em; text-transform: uppercase; margin-top: 0.15rem; }
    a { color: #d97725; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .note { font-size: 1rem; line-height: 1.55; }
    .stage-list { list-style: none; padding: 0; margin: 0; border-top: 1px solid rgba(12,78,185,0.3); }
    .stage { display: flex; justify-content: space-between; gap: 0.75rem; padding: 0.6rem 0; border-bottom: 1px solid rgba(12,78,185,0.15); }
    .stage-label { font-weight: 600; }
    .stage-detail { font-size: 0.95rem; opacity: 0.9; text-align: right; flex: 1; }
    .stage.stage-active .stage-label { color: #0c4eb9; }
    .stage.stage-done .stage-label { color: rgba(12,78,185,0.7); }
    .stage.stage-pending .stage-label { color: rgba(12,78,185,0.4); }
    @media (max-width: 600px) {
      .stage { flex-direction: column; align-items: flex-start; }
      .stage-detail { text-align: left; }
    }
  </style>
</head>
<body>
  <div class="wrapper">
    <header>
      <h1>comfy.course template</h1>
      <div class="linkline"><a href="https://course.yakushev.fr/" target="_blank" rel="noopener">course,yakushev.fr</a></div>
    </header>
    <section class="note">
      AI Toolkit add-on is installing. This usually takes a few minutes. The page will redirect automatically once the UI starts.
    </section>
    <ul class="stage-list" data-stage-list></ul>
    <section class="note">
      You can keep this tab open—when the installation finishes, it will switch to AI Toolkit. If nothing happens in ~5 minutes, redeploy the pod to try again.
    </section>
  </div>
  <script>
    const stageList = document.querySelector('[data-stage-list]');

    function renderStages(stages) {
      if (!Array.isArray(stages)) return;
      stageList.innerHTML = '';
      stages.forEach((stage) => {
        const li = document.createElement('li');
        li.className = `stage stage-${stage.status}`;
        const label = document.createElement('span');
        label.className = 'stage-label';
        label.textContent = stage.label;
        const detail = document.createElement('span');
        detail.className = 'stage-detail';
        detail.textContent = stage.detail || '';
        li.appendChild(label);
        li.appendChild(detail);
        stageList.appendChild(li);
      });
    }

    async function fetchState() {
      try {
        const res = await fetch('/state?ts=' + Date.now(), { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          renderStages(data.stages);
        }
      } catch (err) {
        // ignore transient errors
      } finally {
        window.setTimeout(fetchState, 2000);
      }
    }

    async function probeReady() {
      try {
        const res = await fetch('/status?ts=' + Date.now(), { cache: 'no-store' });
        if (res.status === 200) {
          const txt = (await res.text()).trim();
          if (txt !== 'placeholder') {
            window.location.replace('/');
            return;
          }
        } else if (res.status === 404) {
          window.location.replace('/');
          return;
        }
      } catch (err) {
        // fall through and retry
      }
      window.setTimeout(probeReady, 2000);
    }

    fetchState();
    probeReady();
  </script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    log_path = "/ai_toolkit_setup.log"

    def _send_file(self, file_path: Path) -> None:
        try:
            payload = file_path.read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"
        self.send_response(200)
        if content_type.startswith("text/"):
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        else:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path or "/")
        asset_file = _resolve_asset_file(path)
        if asset_file:
            self._send_file(asset_file)
            return
        if path.startswith("/assets/"):
            self.send_response(404)
            self.end_headers()
            return
        if path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path.startswith("/status"):
            data = b"placeholder"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/state"):
            payload = build_state_payload(self.log_path)
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        encoded = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):  # noqa: A003
        return


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8675)
    parser.add_argument("--files", nargs="*", default=["/ai_toolkit_setup.log"])
    args = parser.parse_args()
    Handler.log_path = args.files[0]
    with ReuseTCPServer(("", args.port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
