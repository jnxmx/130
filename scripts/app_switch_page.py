#!/usr/bin/env python3
import argparse
import http.server
import json
import mimetypes
import socketserver
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


LOCAL_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR_CANDIDATES = [
    Path("/usr/share/nginx/html/assets"),
    Path("/workspace/ComfyUI/web/assets"),
    Path("/Comfy/web/assets"),
    LOCAL_PROJECT_ROOT / "assets",
]
VALID_APPS = {"comfyui", "ai-toolkit"}


APP_LABELS = {
    "comfyui": "ComfyUI",
    "ai-toolkit": "AI Toolkit",
}


def _read_text(path: Path, default: str = "") -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return value or default


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def _label(app_name: str) -> str:
    return APP_LABELS.get(app_name, app_name)


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


def build_state_payload(state_dir: Path, target_app: str, source_app: str) -> dict:
    active_app = _read_text(state_dir / "active_app", source_app)
    desired_app = _read_text(state_dir / "desired_app", active_app or source_app)
    phase = _read_text(state_dir / "phase", "idle")
    detail = _read_text(state_dir / "detail", "")
    switching_to_target = desired_app == target_app
    active_is_source = active_app == source_app
    active_is_target = active_app == target_app
    button_label = f"Turn off {_label(source_app)} and start {_label(target_app)}"
    if switching_to_target:
        if not detail:
            detail = f"Switching from {_label(source_app)} to {_label(target_app)}"
    elif active_is_target:
        detail = detail or f"{_label(target_app)} is selected as the main app"
    else:
        detail = detail or f"{_label(source_app)} is currently selected as the main app"
    return {
        "timestamp": time.time(),
        "active_app": active_app,
        "desired_app": desired_app,
        "target_app": target_app,
        "source_app": source_app,
        "target_label": _label(target_app),
        "source_label": _label(source_app),
        "phase": phase,
        "detail": detail,
        "show_button": bool(active_is_source and not switching_to_target),
        "switching": bool(switching_to_target),
        "button_label": button_label,
    }


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>App Switcher</title>
  <style>
    @font-face {
      font-family: 'Inter';
      font-style: normal;
      font-weight: 100 900;
      font-display: swap;
      src: url('/assets/fonts/InterVariable.woff2') format('woff2');
    }
    @font-face {
      font-family: 'Inter';
      font-style: italic;
      font-weight: 100 900;
      font-display: swap;
      src: url('/assets/fonts/InterVariable-Italic.woff2') format('woff2');
    }
    :root {
      --bg: #161717;
      --text-main: #c4c7cf;
      --text-dim: #6b7180;
      --accent: #f0ff41;
      --accent-text: #0f1114;
      --track: #34394a;
      --screen-gap: 27px;
      --stage-font-size: 26px;
      --stage-line-height: 1.2;
      --stage-line-size: calc(var(--stage-font-size) * var(--stage-line-height));
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      min-height: 100%;
      background: var(--bg);
      color: var(--text-main);
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .page {
      min-height: 100vh;
      padding: var(--screen-gap);
    }
    .layout {
      min-height: calc(100vh - (var(--screen-gap) * 2));
      display: grid;
      grid-template-columns: minmax(220px, 320px) 1fr minmax(220px, 320px);
      gap: 28px;
    }
    .left-column {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
    }
    .template-name {
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
      font-weight: 700;
      color: var(--accent);
      letter-spacing: 0.01em;
    }
    .template-by {
      margin-top: 4px;
      font-size: 14px;
      line-height: 18px;
      color: var(--text-main);
      font-weight: 400;
    }
    .template-by a {
      color: var(--accent);
      text-decoration: underline;
      text-underline-offset: 2px;
      text-decoration-thickness: 1px;
    }
    .state-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      line-height: 1.3;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .state-chip::before {
      content: '';
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent);
      flex: 0 0 auto;
    }
    .center-column {
      display: flex;
      align-items: center;
      justify-content: center;
      min-width: 0;
    }
    .center-stack {
      width: min(520px, 94%);
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      gap: 0;
    }
    .brand-wrap {
      height: 104px;
      display: flex;
      align-items: flex-end;
      justify-content: center;
      margin-bottom: calc(var(--stage-line-size) * 1.5);
    }
    .brand-mark {
      width: min(236px, 84vw);
      height: auto;
      user-select: none;
      pointer-events: none;
    }
    .progress-track {
      width: min(360px, 86vw);
      height: 8px;
      border-radius: 999px;
      background: var(--track);
      overflow: hidden;
      position: relative;
      opacity: 0;
      transition: opacity 160ms ease;
    }
    .progress-track.is-visible { opacity: 1; }
    .progress-runner {
      position: absolute;
      top: 0;
      left: -30%;
      width: 30%;
      height: 100%;
      border-radius: 999px;
      background: var(--accent);
      animation: loading-runner 1.25s linear infinite;
    }
    @keyframes loading-runner {
      from { left: -30%; }
      to { left: 100%; }
    }
    .current-stage {
      margin-top: var(--stage-line-size);
      font-size: var(--stage-font-size);
      line-height: var(--stage-line-height);
      font-weight: 700;
      color: var(--text-main);
    }
    .hint-note {
      margin-top: calc(var(--stage-line-size) * 1.1);
      font-size: 19px;
      line-height: 1.35;
      color: var(--text-dim);
      max-width: 440px;
    }
    .switch-button {
      margin-top: calc(var(--stage-line-size) * 1.35);
      border: 0;
      border-radius: 999px;
      padding: 15px 26px;
      background: var(--accent);
      color: var(--accent-text);
      font: inherit;
      font-size: 17px;
      font-weight: 700;
      cursor: pointer;
      transition: transform 140ms ease, opacity 140ms ease;
    }
    .switch-button:hover { transform: translateY(-1px); }
    .switch-button[disabled] {
      opacity: 0.65;
      cursor: default;
      transform: none;
    }
    .status-line {
      margin-top: 18px;
      min-height: 24px;
      font-size: 14px;
      line-height: 1.4;
      color: var(--text-dim);
    }
    .right-column {
      min-width: 0;
    }
    @media (max-width: 980px) {
      .layout {
        grid-template-columns: 1fr;
        gap: 24px;
      }
      .left-column { justify-content: flex-start; }
      .center-column { min-height: 360px; }
      .right-column { display: none; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="layout">
      <aside class="left-column">
        <div class="left-top">
          <h1 class="template-name">comfy.work</h1>
          <div class="template-by">by <a href="https://course.yakushev.fr/" target="_blank" rel="noopener">course.yakushev.fr</a></div>
        </div>
        <div class="left-bottom">
          <div class="state-chip" data-state-chip>Single app mode</div>
        </div>
      </aside>
      <main class="center-column">
        <div class="center-stack">
          <div class="brand-wrap">
            <img class="brand-mark" data-brand-mark src="/assets/images/comfy-brand-mark.svg" alt="Comfy" draggable="false">
          </div>
          <div class="progress-track" data-progress-track aria-hidden="true">
            <div class="progress-runner"></div>
          </div>
          <div class="current-stage" data-stage-title>Loading app state</div>
          <div class="hint-note" data-stage-note>Only one GPU app runs at a time on this pod.</div>
          <button type="button" class="switch-button" data-switch-btn hidden>Switch app</button>
          <div class="status-line" data-status-line></div>
        </div>
      </main>
      <aside class="right-column" aria-hidden="true"></aside>
    </div>
  </div>
  <script>
    const brandMark = document.querySelector('[data-brand-mark]');
    const progressTrack = document.querySelector('[data-progress-track]');
    const stageTitle = document.querySelector('[data-stage-title]');
    const stageNote = document.querySelector('[data-stage-note]');
    const switchBtn = document.querySelector('[data-switch-btn]');
    const statusLine = document.querySelector('[data-status-line]');
    const stateChip = document.querySelector('[data-state-chip]');

    const BRAND_MARK_FALLBACK = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjc4IiBoZWlnaHQ9Ijc4IiB2aWV3Qm94PSIwIDAgMjc4IDc4IiBmaWxsPSJub25lIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPgo8cGF0aCBkPSJNMjMyLjE1MSA3Ny4xNzYxQzIzMC42NDUgNzcuMTc2MSAyMjkuNDMgNzYuNjIwOCAyMjguNjM4IDc1LjU2OTdDMjI3LjgyMyA3NC40ODg5IDIyNy42MTEgNzIuOTgxMSAyMjguMDU1IDcxLjQzMzNMMjMwLjgwMSA2MS44NTc2QzIzMS43MDggNTguNjkxNCAyMzUuMDIzIDU2LjExNTcgMjM4LjE5IDU2LjExNTdIMjQ0LjM0NkMyNDUuMDc4IDU2LjExNTcgMjQ1Ljc2MSA1NS42MzEgMjQ1LjkyMyA1NC45Mjc2TDI0Ni45ODUgNTEuMjI0NUMyNDcuMTI3IDUwLjcyODYgMjQ3LjAyOCA1MC4xOTU4IDI0Ni43MTggNDkuNzg0QzI0Ni40MDggNDkuMzczIDI0NS45MjMgNDkuMTMxIDI0NS40MDcgNDkuMTMxTDIzMS45MTcgNDkuMTMzNEMyMzEuODQxIDQ5LjEyMyAyMzEuNzcxIDQ5LjExODEgMjMxLjY5NCA0OS4xMTgxSDIyNy4xNDlDMjI1LjY0MyA0OS4xMTgxIDIyNC40MjggNDguNTYyOSAyMjMuNjM2IDQ3LjUxMThDMjIyLjgyMiA0Ni40MDEgMjIyLjYwOSA0NC45MjMyIDIyMy4wNTMgNDMuMzc2MkwyMjMuNTgzIDQxLjUyNzFDMjIzLjcyNSA0MS4wMzIgMjIzLjYyNiA0MC40OTkyIDIyMy4zMTYgNDAuMDg3M0MyMjMuMDA2IDM5LjY3NjMgMjIyLjUyMSAzOS40MzQ0IDIyMi4wMDYgMzkuNDM0NEgyMjAuMjY1QzIxOS41MzIgMzkuNDM0NCAyMTguODg5IDM5LjkxOTEgMjE4LjY4NyA0MC42MjMzTDIxNy44OTggNDMuMzc2MkMyMTYuOTkxIDQ2LjU0MjQgMjEzLjY3NiA0OS4xMTgxIDIxMC41MDkgNDkuMTE4MUgyMDQuMzY5QzIwMy42MzggNDkuMTE4MSAyMDIuOTk1IDQ5LjYwMTMgMjAyLjc5MiA1MC4zMDNMMjAxLjA0NyA1Ni4zNDE1QzIwMS4wNCA1Ni4zNjQ3IDIwMS4wMiA1Ni40MjY0IDIwMS4wMTMgNTYuNDQ5NkwxOTguNTIyIDY1LjA3ODNDMTk4LjUxMSA2NS4xMDc5IDE5OC40OTYgNjUuMTU1MiAxOTguNDg3IDY1LjE4NTdMMTk2LjY4NSA3MS40MzE2QzE5NS43NzcgNzQuNjAwMiAxOTIuNDYyIDc3LjE3NjEgMTg5LjI5NSA3Ny4xNzYxSDE3OS43MkMxNzguMjE0IDc3LjE3NjEgMTc2Ljk5OSA3Ni42MjA4IDE3Ni4yMDcgNzUuNTY5N0MxNzUuMzkzIDc0LjQ4OTcgMTc1LjE4IDcyLjk4MTkgMTc1LjYyNCA3MS40MzQxTDE4My42NDkgNDMuNTk2NkMxODMuNjU5IDQzLjU2NzcgMTgzLjY3OCA0My41MTAxIDE4My42ODcgNDMuNDgwNEwxODQuMjQ3IDQxLjUyODhDMTg0LjM5IDQxLjAzMzcgMTg0LjkyIDQwLjUwMDEgMTgzLjk4MiA0MC4wODgzQzE4My42NzEgMzkuNjc2NCAxODMuMTg2IDM5LjQzNDUgMTgyLjY3MSAzOS40MzQ1SDE4MC45NDdDMTgwLjIxNiAzOS40MzQ1IDE3OS41NzQgMzkuOTE4MyAxNzkuMzcxIDQwLjYyMDJMMTc1Ljg3OCA1Mi43MjUxQzE3NS44NjcgNTIuNzU0NyAxNzUuODUyIDUyLjgwMjggMTc1Ljg0NCA1Mi44MzI1TDE3NC41MjUgNTcuNDAxNkMxNzMuNjE3IDYwLjU3MSAxNzAuMzAyIDYzLjE0NzYgMTY3LjEzNiA2My4xNDc2SDE1Ny41NkMxNTYuMDU0IDYzLjE0NzYgMTU0LjgzOSA2Mi41OTIzIDE1NC4wNDcgNjEuNTQxMkMxNTMuMjMzIDYwLjQ1OTYgMTUzLjAyIDU4Ljk1MTkgMTUzLjQ2NCA1Ny40MDQ4TDE1OS4yODMgMzcuMjAyNEMxNTkuNDI2IDM2LjcwNzMgMTU5LjMyNyAzNi4xNzM3IDE1OS4wMTcgMzUuNzYxMULDMTU4LjcwNyAzNS4zNDkzIDE1OC4yMjIgMzUuMTA3NCAxNTcuNzA2IDM1LjEwNzRIMTU1Ljk3M0MxNTUuMjQxIDM1LjEwNzQgMTU0LjU5NyAzNS41OTIxIDE1NC4zOTUgMzYuMjk1NUwxNTEuOTE1IDQ0LjkzNDVDMTUxLjkwNSA0NC45NjM0IDE1MS44ODkgNDUuMDEwNiAxNTEuODgxIDQ1LjA0MDNMMTQ4LjMxMSA1Ny40MDE2QzE0Ny40MDEgNjAuNTcxIDE0NC4wODYgNjMuMTQ3NiAxNDAuOTIgNjMuMTQ3NkgxMzEuMzQ1QzEyOS44MzkgNjMuMTQ3NiAxMjguNjI0IDYyLjU5MjMgMTI3LjgzMiA2MS41NDEyQzEyNy4wMTcgNjAuNDYwNCAxMjYuODA1IDU4Ljk1MjYgMTI3LjI0OSA1Ny40MDQ4TDEyOS55OTUgNDcuODI5MUMxMzAuMDA1IDQ3LjgwMDMgMTMwLjAyIDQ3Ljc1NDYgMTMwLjAyOCA0Ny43MjQ5TDEzMy4wNzIgMzcuMTg1NUMxMzMuMjE1IDM2LjY5MDQgMTMzLjExNyAzNi4xNTYgMTMyLjgwNyAzNS43NDQyQzE3Mi40OTcgMzUuMzMyNCAxMzIuMDEyIDM1LjA4OTYgMTMxLjQ5NiAzNS4wODk2SDEyOS43NzlDMTI5LjA0OCAzNS4wODk2IDEyOC40MDUgMzUuNTczNSAxMjguMjAyIDM2LjI3NTRMMTI3Ljk1NCAzNy4xMzc0QzEyNy45NDUgMzcuMTYyMyAxMjcuOTI2IDM3LjIyMDggMTI3LjkxOSAzNy4yNDU2TDEyMi4zMiA1Ni42MjE5QzEyMi4zMTIgNTYuNjQ3NSAxMjIuMjkxIDU2LjcxIDEyMi4yODQgNTYuNzM1N0wxMjIuMDkzIDU3LjQwNzlDMTIxLjE4NiA2MC41NzA5IDExNy44NzEgNjMuMTQ3NSAxMTQuNzA1IDYzLjE0NzVIMTA1LjEyOUMxMDMuNjEzIDYzLjE0NzUgMTAyLjQwOCA2Mi41OTIyIDEwMS42MTYgNjEuNTQxMUMxMDAuODAyIDYwLjQ2MDMgMTAwLjU5IDU4Ljk1MjUgMTAxLjAzMyA1Ny40MDQ3TDEwMS41NjEgNTUuNTYzNkMxMDEuNzAzIDU1LjA2ODUgMTAxLjYwNCA1NC41MzU3IDEwMS4yOTQgNTQuMTIzOUMxMDAuOTg0IDUzLjcxMjkgMTAwLjQ5OSA1My40NzA5IDk5Ljk4MzkgNTMuNDNlOUg5OC4yNDg2Qzk3LjUxOCA1My40NzA5IDk2Ljg3NSA1My45NTQxIDk2LjY3MjMgNTQuNjU2N0w5NS44Nzk1IDU3LjQwMTVBOTQuOTcwNiA2MC41NzA5IDkxLjY1NTggNjMuMTQ3NSA4OC40ODkxIDYzLjE0NzVIODIuMjgzZDZDODEuNTUxOCA2My4xNDc1IDgwLjkwOCA2My42MzIyIDgwLjcwNjUgNjQuMzM1Nkw3OC42NzExIDcxLjQzMzJDNzcuNzYzNyA3NC42MDAyIDc0LjQ0ODkgNzcuMTc2IDcxLjI4MTkgNzcuMTc2SDYxLjcwNjdDNjAuMjAwMSA3Ny4xNzYgNTguODUxMSA3Ni42MjA3IDU4LjE5MzIgNzUuNTY5NkM1Ny4zNzg4IDc0LjQ4ODggNTcuMTY2NCA3Mi45ODEgNTcuNjEwNyA3MS40MzMyTDU5LjM4NjEgNjUuMjQwMUM1OS41MjgzIDY0Ljc0NSA1OS40MjkzIDY0LjIxMjMgNTkuMTE5MyA2My44MDA0QzU4LjgwOTMgNjMuMzg5NCA1OC4zMjQyIDYzLjE0NzUgNTcuODA5IDYzLjE0NzVINTIuNjk4N0M1MS4xOTI5IDYzLjE0NzUgNDkuNzc3OSA2Mi41OTIyIDQ5LjE4NSIgNjEuNTQxMUM0OC4zNzEyIDYwLjQ2MDMgNDguMTU4NSA1OC45NTI1IDQ4LjYwMjQgNTcuNDA1NUw0OS4xMzA0IDU1LjU2MzZDNDkuNzIyIDU1LjA2ODUgNDkuMTczMiA1NC41MzU3IDQ4Ljg2MzIgNTQuMTIzOUM0OC41NTMyIDUzLjcxMjkgNDguMDY4IDUzLjQ3MDkgNDcuNTUyOCA1My40NzA5SDQ1LjgxMjNDNDUuMDgwNCA1My40NzA5IDQ0LjQzNyA1My45NTU2IDQ0LjIzNTEgNTQuNjU5TDQzLjQ0NzYgNTcuNDA0N0M0Mi41Mzk5IDYwLjV7MTcgMzkuMjI1IDYzLjE0NzUgMzYuMDU4NCA2My4xNDc1TDIyLjk0MjggNjMuMTcwN0wxMy4zMzk1IDYzLjE3MTVDMTEuODMzNCA2My4xNzE1IDEwLjYxODQgNjIuNjE2MyA5LjgyNjQzIDYxLjU2NTJDOS4wMTI0MyA2MC40ODQ0IDguNzk5NyA1OC45NzY2IDkuMjQzNjIgNTcuNDI5NUwxMS4wMjY2IDUxLjIxMTZDMTEuMTY4OCA1MC43MTU3IDExLjA2OTggNTAuMTgyOSAxMC43NTk4IDQ5Ljc3MTFDMTAuNDQ5OKA0OS4zNjAxIDkuOTY0NjcgNDkuMTE4MSA5LjQ0OTQ2IDQ5LjExODFINC4zMjMxOUMyLjgxNzoyIDQ5LjExODEgMS42MDIwMSA0OC41NjI5IDAuODEwMDc2IDQ3LjUxMThDLTAuMDAzOTI0IDQ2LjQzMSAtMC4yMTY2NTQgNDQuOTIzMiAwLjIyNzI2NSA0My4zNzYyTDUuMDAwNjggMjYuNzg3OUM1LjAwOTQ2IDI2Ljc2MzEgNS4wMjkxNSAyNi43MDA2IDUuMDM2NjkgMjYuNjc1OEw2LjU3NzczIDIxLjMzMjhDNi41OTE3NiAyMS4yOTQzIDYuNjAzNzQgMjEuMjU2NiA2LjYxNDk4IDIxLjIxNzRMNy4wMjkyIDE5Ljc3MTNDNy45MzY4OSAxNi42MDUxIDExLjI1MTQgMTQuMDI4NSAxNC40MTggMTQuMDI4NUgyMC41NTE0QzIxLjE4MzIgMTQuMDI4NSAyMS45MjcgMTMuNTQzOCAyMi4xMjg1IDEyLjg0MDRMMjQuMTU2NyA1Ljc2NzY2QzI1LjA2NDUgMi42MDA1OCAyOC4zNzkzIDAuMDI0ODU4MSAzMS41NDYzIDAuMDI0ODU4MUw0NC42OTE1IDBINTQuMjY0N0M1NS43NzA5IDAgNTYuOTg1OSAwLjU1NTI0NiA1Ny43Nzc4IDEuNjA2MzRDNTguNTkyMiAyLjY4NzE0IDU4LjgwNDUgNC4xOTQ5NSA1OC4zNjA2IDUuNzQyOEw1NS42MTUgMTUuMzE5MkM1NC43MDY5IDE4LjQ4NTUgNTEuMzg0MiAyMS4wNjEyNDgyMjU0IDIxLjA2MTJMMzUuMDgwMiAyMS4wODUzSDI4Ljk0OTNDMjguMjE3OCAyMS4wODUzIDI3LjU3NDUgMjEuNTY5OSAyNy4zNzIyIDIyLjI3MjZMMjQuNjY3OCAzMS42ODRDMjQuNjU3OCAzMS43MTM2IDI0LjQ0MjUgMzEuNzYwOSAyNC42MzQyIDMxLjc5MDZMMjIuMjYxNSA0MC4wMDc0QzIyLjExODEgNDAuNTAzMyAyMi4yMTY2IDQxLjAzNzcgMjIuNTI3NCA0MS40NTAzQzIyLjgzNzUgNDEuODYxMyAyMy4zMjI2IDQyLjEwMzIgMjMuODM3OCA0Mi4xMDMyQzIzLjgzOSA0Mi4xMDcyIDMyLjUyNDEgNDIuMDg2NCAzMi41MjQxIDQyLjA4NjRINDIuMDk3M0M0My42MDM0IDQyLjA4NjQgNDQuODE4NCA0Mi42NDE3IDQ1LjYxMDQgNDMuNjkyOEM0Ni40MjQ4IDQ0Ljc3MzYgNDYuNjM3MSA0Ni4yODE0IDQ2LjE5MzIgNDcuODI5Mkw0NS42NDY0IDQ5LjczNzZDNDUuNTA0MiA1MC4yMzI3IDQ1LjYwMzIgNTAuNzY1NSA0NS45MTMyIDUxLjE3NzNDNDYuMjIzMiA1MS41ODg0IDQ2LjcwODMgNTEuODMwMyA0Ny4yMjM1IDUxLgzMDNINDguOTY0MUM0OS42OTU2IDUxLgzMDMgNTAuMzM4OSA1MS4zNDU2IDUwLjU0MTIgNTAuNjQyOUw1MS41NzM1IDQ3LjA1MTNDNTEuNTgzNSA0Ny4wMjE3IDU1LjQwMzIgMzMuODAzMiA1NS40MDMyIDMzLjgwMzJDNTYuMzExNyAzMC42MzM3IDU5LjYyNjUgMjguMDU4IDYyLjc5MzYgMjguMDU4SDY4LjkwNTdDNjkuNjM3NiAyOC4wNTggNzAuMjgxMyAyNy41NzMzIDcwLjQ4MjggMjYuODY5MUw3Mi41MTI2IDE5Ljc4OTFDNzMuNDIxMSAxNi42MjI5IDc2LjczNjMgMTQuMDQ2MyA3OS45MDI2IDE0LjA0NjNINDkuNDc3NEM5MC45ODM2IDE0LjA0NjMgOTIuMTk4NiAxNC42MDE2IDkyLjk5MDUgMTUuNjUyNkM5My44MDQ5IDE2LjczMzQgOTQuMDE3NiAxOC4yNDEzIDkzLjU3MzcgMTkuNzg4M0w5MS44MDI3IDI1Ljk2NTNDOTEuNjYwNiAyNi40NjA0IDkxLjc1OTUgMjYuOTkzMiA5Mi4wNjk1IDI3LjQwNUNDOTIuMzc5NSAyNy44MTYxIDkyLjg2NDcgMjguMDU4IDkzLjM3OTkgMjguMDU4SDk4LjU4NDNDMTAwLjA5IDI4LjA1OCAxMDEuMzA1IDI4LjYxMzIgMTAyLjA5NyAyOS42NjQzQzEwMi45MTIgMzAuNzQ1MSAxMDMuMTI0IDMyLjI1MjkgMTAyLjY4IDMzLjgwMDhMOTguMDk0NCA0OS43MzYxQzk3Ljk1MTggNTAuMjMxMiA5OC4wNTAzIDUwLjc2NDggOTguMzYwMyA1MS4xNzY2Qzk4LjY3IDUxLjU4ODQgOTkuMTU1NSA1MS44MzA0IDk5LjY3MTEgNTEuODMwNEgxMDEuMzk1QzEwMi4xMjYgNTEuODMwNCAxMDIuNzY5IDUxLjM0NjUgMTAyLjk3MiA1MC42NDM4TDEwNS4xMzEgNDMuMTU0NUMxMDUuMTM4IDQzLjEzMjEgMTA1LjE1OCA0My4wNzIgMTA1LjE2NCA0My4wNDk2TDExMC43NjMgMjMuNjcyNUMxMTAuNzc0IDIzLjY0MiAxMTAuNzg5IDIzLjU5MzEgMTA1Ljc5OCAyMy41NjI3TDExMS44OSAxOS43NzM5QzExMi43OTkgMTYuNjA1MyAxMTYuMTE0IDE0LjAyODggMTE5LjI4IDE0LjAyODhIMTQxLjk1N0MxNDMuNDYzIDE0LjAyODggMTQ0LjY3OCAxNC41ODQ3IDE0NS40NyAxNS42MzU5QzE0Ni4yODUgMTYuNzE1OSAxNDYuNDk4IDE4LjIyMzcgMTQ2LjA1NCAxOS43NzA3TDE0NC4yNzggMjUuOTY0NkMxNDQuMTM1IDI2LjQ2MDUgMTQ0LjIzNCAyNi5OTkzMyAxNDQuNTQgMjcuNDA0M0MxNDQuODU0IDI3LjgxNjEgMTQ1LjMzOSAyOC4wNTgxIDE0NS44NTUgMjguMDU4MUgxNDcuNjQzQzE0OC4zNzUgMjguMDU4MSAxNDkuMDE5IDI3LjU3MzQgMTQ5LjIyIDI2Ljg3TDE1MS4yNTEgMTkuNzg5MkMxNTIuMTU5IDE2LjYyMjkgMTU1LjQ3NCAxNC4wNDY0IDE1OC42NCAxNC4wNDY0SDE2OC4yMTVDMTY5LjcyMiAxNC4wNDY0IDE3MC45MzczIDE0LjYwMTYgMTcxLjcyOSAxNS42NTI3QzE3Mi41NDMgMTYuNzMzNSAxNzIuNzU1IDE4LjI0MTMgMTcyLjMxMSAxOS43ODkyTDE3MC41NDEgMjUuOTY1NEMxNzAuMzk5IDI2LjQ2MDUgMTcwLjQ5OCAyNi45OTMzIDE3MC44MDggMjcuNDA1MUMxNzEuMTE4IDI3LjgxNjEgMTcxLjYwMyAyOC4wNTgxIDE3Mi4xMTggMjguMDU4MUgxNzcuMjMxQzE3OC43MzcgMjguMDU4MSAxNzkuNTcyIDI4LjYxMzMgMTgwLjc0NCAyOS42NjQ0QzE4MS41NTggMzAuNzQ1MiAxODEuNzcwIDMyLjI1MyAxODEuMzI2IDMzLjgwMDFMMTgwLjc4MiAzNS43MDEyQzE4MC42MzkgMzYuMTk2NCAxODAuNzM4IDM2LjcyOTEgMTgxLjA0OCAzNy4xNDEEMTgxLjM1OCAzNy41NTIwIDE4MS44NDQgMzcuNzkzOSAxODIuMzU5IDM3Ljc5MzlIMTg0LjA5M3JDMTg0LjgyNCAzNy43OTM5IDE4NS40NjcgMzcu3MTA4IDE4NS42NyAzNi42MDlMMTkwLjUzNiAxOS43NzQ4QzE5MS40NDUgMTYuNjA0NiAxOTQuNzYgMTQuMDI4OCAxOTcuOTI2IDE0LjAyODhIMjA0LjA4MkMyMDQuODE0IDE0LjAyODggMjA1LjQ1NyAxMy41NDQxIDIwNS42NTkgMTIuODQwN0wyMDcuNjk1IDUuNzQzMTNDMjA4LjYwMyAyLjU3NjA1IDIxMS45MTggMC4wMDAzMjgxNiAyMTUuMDg0IDAuMDAwMzI4MTZIMjI0LjY1OUMyMjYuMTY2IDAuMDAwMzI4MTZIMjI3LjM4MSAwLjU1NTU3NSAyMjguMTczIDEuNjA2NjdDMjI4Ljk4NyAyLjY4NzQ2IDIyOS4yIDQuMTk1MjggMjI4Ljc1NSA1Ljc0MzEzTDIyNi4wMSAxNS4zMTk2QzIyNS4xMDIgMTguNDg1OCAyMjEuNzg3IDIxLjA2MTUgMjE4LjYyMSAyMS4wNjE1SDIxMi40NjRDMjExLzczMyAyMS4wNjE1IDIxMS4wODkgMjEuNTQ2MiAyMTAuODg3IDIyLjI1MDVMMjA5LjgyMiAyNS45NjU1QzIwOS42OCAyNi40NjA2IDIwOS43NzkgMjYuOTkzNCAyMTAuMDg5IDI3LjQwNTJDMjEwLjM5OSAyNy44MTYyIDIxMC44ODQgMjguMDU4MiAyMTEuMzk5IDIyLjA1ODJIMjE2LjU0OEMyMTguMDU0IDI4LjA1ODIgMjE5LjI2OSAyOC42MTM0IDIyMC4wNjEgMjkuNjY0NUMyMjAuODc2IDMwLjc0NDUgMjIxLjA4OCAzMi4yNTIzIDIyMC42NDQgMzMuODAwMUwyMjAuMDk5IDM1LjcwMTNDMjE5Ljk1NyAzNi4xOTY0IDIyMC4wNTYgMzYuNzI5MiAyMjAuMzY2IDM3Ljc0MUMyMjAuNjc2IDM3Ljc1MjEgMjIxLjE2MSAzNy43OTQgMjIxLjY3NiAzNy43OTRIMjIzLjQxN0MyMjQuMTQ4IDM3Ljc5NCAyMjQuNzkxIDM3LjMxMDkgMjI0Ljk5NCAzNi42MDgzTDIyOS44NTMgMTkuNzc0OCAyMzAuNzYzIDE2LjYwNTQgMjM0LjA3OCAxNC4wMjg4IDIzNy4yNDQgMTQuMDI4OEgyNDYuODE5QzI0OC4zMjYgMTQuMDI4OCAyNDkuNTQxIDE0LjU4NDEgMjUwLjMzMyAxNS42MzUyQzE1MS4xNDcgMTYuNzE2IDI1MS4zNTkgMTguMjIzOCAyNTAuOTE1IDE5Ljc3MTZMMjQ2LjU5MyAzNC44MDE2QzI0Ni41ODIgMzQuODMxMiAyNDYuNTY3IDM0Ljg3ODYgMjQ2LjU1OCAzNC45MDlMMjQ2LjA5NSAzNi41MTdDMjQ1Ljk1MyAzNy4wMTI5IDI0Ni4wNTEgMzcuNTQ2NSAyNDYuMzYxIDM3Ljk1ODJDMjQ2LjY3MSAzOC4zNyAyNDcuMTU2IDM4LjYxMiAyNDcuNjcyIDM4LjYxMkgyNDkuMzg1QzI1MC4xMTYgMzguNjEyIDI1MC43NTkgMzguMTI4MiAyNTAuOTYxIDM3LjQyNjNMMjU2LjA1NCAxOS43NzQ5QzI1Ni45NjMgMTYuNjA1NSAyNjAuMjc3IDE0LjAyODkgMjYzLjQ0NCAxNC4wMjg5SDI3My4wMTlDMjc0LjUyNSAxNC4wMjg5IDI3NS43NCAxNC41ODQ5IDI3Ni41MzIgMTUuNjM2MUMyNzcuMzQ3IDE2LjcxNjEgMjc3LjU1OSAxOC4yMjM5IDI3Ny4xMTUgMTkuNzcxN0wyNjYuMjc0IDU3LjQwMzVDMjY1LjM2NSA2MC41NzIyIDI2Mi4wNSA2My4xNDggMjU4Ljg4NCA2My4xNDhIMjUyLjcyOEMyNTEuOTk2IDYzLjE0OCAyNTEuMzUyIDYzLjYzMjcgMjUxLjE1MSA2NC4zMzYxTDI0OS4xMTUgNzEuNDMzN0MyNDguMjA4IDc0LjYwMDcgMjQ0Ljg5MyA3Ny4xNzY1IDI0MS43MjYgNzcuMTc2NUwyMzIuMTUxIDc3LjE3NjFaTTc3LjIzNjQgMzUuMTA3NEM3Ni41OTU0IDM1LjEwNzQgNzUuOTUyNCAzNS41OTEyIDc1Ljc0OTcgMzYuMjkzOUw3MC42NDEgNTQuMDIwNkM3MC40OTg0IDU4LjUxNTcgNzAuNTk2NSA1NS4wNDgzIDcwLjkwNjYgNTUuNDYxMUM3MS4yMTY2IDU1Ljg3MzcgNzEuNzAyMSA1Ni4xMTU3IDcyLjIxNzcgNTYuMTE1N0g3My45NTFDNzQuNjgyMSA1Ni4xMTU3IDc1LjMyNSA1NS42MzE5IDc1LjUyNzcgNTQuOTI5Mkw4MC42MzY0IDM3LjIwMjVDODAuNzc5IDM2LjcwNzQgODAuNjgwOSAzNi4xNzM4IDgwLjM3MDkgMzUuNzYyQzgwLjA2MDggMzUuMzQ5NCA3OS41NzUzIDM1LjEwNzQgNzkuMDU5OCAzNS4xMDc0SDc3LjIzNjZaIiBmaWxsPSIjRjBGRjQxIj48L3BhdGg+Cjwvc3ZnPg==";

    function ensureBrandMark(imgEl) {
      if (!imgEl) return;
      const applyFallback = () => {
        if (imgEl.dataset.fallbackApplied === '1') return;
        imgEl.dataset.fallbackApplied = '1';
        imgEl.onerror = null;
        imgEl.src = BRAND_MARK_FALLBACK;
      };
      imgEl.onerror = applyFallback;
      if (imgEl.complete && imgEl.naturalWidth === 0) {
        applyFallback();
      }
    }
    ensureBrandMark(brandMark);

    let currentState = null;

    function renderState(state) {
      currentState = state || {};
      const showButton = !!state.show_button;
      const switching = !!state.switching;
      const sourceLabel = state.source_label || 'Current app';
      const targetLabel = state.target_label || 'Target app';
      progressTrack.classList.toggle('is-visible', switching);
      switchBtn.hidden = !showButton;
      switchBtn.disabled = switching;
      switchBtn.textContent = state.button_label || `Turn off ${sourceLabel} and start ${targetLabel}`;

      if (switching) {
        stageTitle.textContent = `Starting ${targetLabel}`;
        stageNote.textContent = state.detail || `Stopping ${sourceLabel}, clearing GPU memory, and starting ${targetLabel}.`;
        statusLine.textContent = 'Waiting for the target app to take this port...';
      } else {
        stageTitle.textContent = targetLabel;
        stageNote.textContent = 'Only one GPU app runs at a time on this pod.';
        statusLine.textContent = state.detail || `${sourceLabel} is active right now.`;
      }

      if (state.phase) {
        stateChip.textContent = state.phase.replace(/[-_]/g, ' ');
      }
    }

    async function requestSwitch() {
      switchBtn.disabled = true;
      statusLine.textContent = 'Requesting app switch...';
      try {
        const res = await fetch('/switch?ts=' + Date.now(), {
          method: 'POST',
          cache: 'no-store',
        });
        if (!res.ok) {
          throw new Error('bad status');
        }
        statusLine.textContent = 'Switch requested. Waiting for the target app...';
      } catch (err) {
        statusLine.textContent = 'Failed to request the app switch. Retry in a moment.';
        switchBtn.disabled = false;
      }
    }

    async function fetchState() {
      try {
        const res = await fetch('/state?ts=' + Date.now(), { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          renderState(data);
        }
      } catch (err) {
        // ignore transient errors during handoff
      } finally {
        window.setTimeout(fetchState, 2000);
      }
    }

    async function probeReplacement() {
      try {
        const res = await fetch('/status?ts=' + Date.now(), { cache: 'no-store' });
        if (res.ok) {
          const text = (await res.text()).trim();
          if (text !== 'switch-placeholder') {
            window.location.replace('/');
            return;
          }
        } else if (res.status === 404) {
          window.location.replace('/');
          return;
        }
      } catch (err) {
        // keep polling during the brief handoff gap
      }
      window.setTimeout(probeReplacement, 1500);
    }

    switchBtn.addEventListener('click', requestSwitch);
    fetchState();
    probeReplacement();
  </script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    state_dir = Path("/tmp/app_state")
    target_app = "ai-toolkit"
    source_app = "comfyui"

    def _send_text(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, html: str, status: int = 200) -> None:
        encoded = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, file_path: Path) -> None:
        try:
            payload = file_path.read_bytes()
        except OSError:
            self._send_text(b"not found", status=404)
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

    def _request_switch(self) -> None:
        _write_text(self.state_dir / "desired_app", self.target_app)
        payload = build_state_payload(self.state_dir, self.target_app, self.source_app)
        self._send_json({"ok": True, "state": payload})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path or "/")
        if path.startswith("/switch"):
            self._request_switch()
            return
        asset_file = _resolve_asset_file(path)
        if asset_file:
            self._send_file(asset_file)
            return
        if path.startswith("/assets/"):
            self._send_text(b"not found", status=404)
            return
        if path.startswith("/healthz"):
            self._send_text(b"ok")
            return
        if path.startswith("/status"):
            self._send_text(b"switch-placeholder")
            return
        if path.startswith("/state"):
            payload = build_state_payload(self.state_dir, self.target_app, self.source_app)
            self._send_json(payload)
            return
        self._send_html(HTML_PAGE)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path or "/")
        if path.startswith("/switch"):
            self._request_switch()
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def log_message(self, *_):  # noqa: A003
        return


class ReuseTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", default="/tmp/app_state")
    parser.add_argument("--target-app", choices=sorted(VALID_APPS), required=True)
    parser.add_argument("--source-app", choices=sorted(VALID_APPS), required=True)
    args = parser.parse_args()

    Handler.state_dir = Path(args.state_dir)
    Handler.target_app = args.target_app
    Handler.source_app = args.source_app

    with ReuseTCPServer(("", args.port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
