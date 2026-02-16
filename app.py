#!/usr/bin/env python3
"""
Evaluación de Calidad – Dashboard (Flask for Vercel)

Update:
- Data source can be a Google Apps Script exec endpoint (Google Sheets -> JSON).
- Keeps Vercel-safe /api/data?id=### (numeric plantel_id).
- Adds a reload endpoint /api/reload to clear cache and refresh data.
- Fixes chart type switching robustness (avoids late chart renders when switching quickly).
"""

import os
import json
import time
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from flask import Flask, jsonify, render_template_string, request


# ── Config ──────────────────────────────────────────────────────────────────
EXCEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset.xlsx")

DATA_ENDPOINT_URL = os.environ.get("DATA_ENDPOINT_URL", "https://script.google.com/macros/s/AKfycbyWSeFiAu3DnCYanPMdOUuNj6YvEYw7-1VMbRJu6MmmJL1vXE7oLGFS83Tg5gmGVnulHA/exec").strip()
DATA_ENDPOINT_API_KEY = os.environ.get("DATA_ENDPOINT_API_KEY", "").strip()
DATA_CACHE_TTL_SECONDS = int(os.environ.get("DATA_CACHE_TTL_SECONDS", "300"))

LIKERT5_ORDER = ["Muy satisfecho", "Satisfecho", "Neutral", "Insatisfecho", "Muy insatisfecho"]
YESNO_ORDER = ["Sí", "No"]

_df: Optional[pd.DataFrame] = None
_likert5_cols: Optional[List[str]] = None
_yesno_cols: Optional[List[str]] = None
_all_question_cols: Optional[List[str]] = None
_plantel_names: Optional[List[str]] = None  # list[str] sorted, index = plantel_id
_loaded_at_epoch: Optional[float] = None
_loaded_source: str = "unknown"


def _clean_text_series(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.replace("\u00A0", " ", regex=False)  # NBSP
        .str.replace("\u2013", "–", regex=False)  # normalize en dash
        .str.replace("\u2014", "–", regex=False)  # normalize em dash to en dash
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )


def _normalize_df_strings(df: pd.DataFrame) -> pd.DataFrame:
    # Normalize typical “blank” strings into NaN for consistent dropna/unique behavior
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].apply(lambda x: None if (isinstance(x, str) and x.strip() == "") else x)
    return df


def _build_endpoint_url(base_url: str) -> str:
    """
    Ensures mode=records and key=... are present (if configured).
    """
    parsed = urllib.parse.urlparse(base_url)
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    # Enforce mode=records (unless user already set a mode)
    if "mode" not in q:
        q["mode"] = ["records"]

    # Optional key
    if DATA_ENDPOINT_API_KEY and "key" not in q:
        q["key"] = [DATA_ENDPOINT_API_KEY]

    new_query = urllib.parse.urlencode(q, doseq=True)
    rebuilt = parsed._replace(query=new_query)
    return urllib.parse.urlunparse(rebuilt)


def _http_get_json(url: str, timeout_sec: int = 25) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "eval-calidad-dashboard/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def load_data_from_endpoint() -> pd.DataFrame:
    if not DATA_ENDPOINT_URL:
        raise ValueError("DATA_ENDPOINT_URL is empty")

    url = _build_endpoint_url(DATA_ENDPOINT_URL)
    payload = _http_get_json(url)

    # Accept multiple shapes for flexibility
    records: Optional[List[Dict[str, Any]]] = None

    if isinstance(payload, list):
        # list of dicts
        records = payload if payload and isinstance(payload[0], dict) else []
    elif isinstance(payload, dict):
        if payload.get("ok") is False:
            raise RuntimeError(payload.get("error") or "Endpoint returned ok:false")
        if isinstance(payload.get("records"), list):
            records = payload["records"]
        elif isinstance(payload.get("data"), list):
            records = payload["data"]
        elif isinstance(payload.get("rows"), list) and isinstance(payload.get("columns"), list):
            cols = payload["columns"]
            rows = payload["rows"]
            df = pd.DataFrame(rows, columns=cols)
            return _normalize_df_strings(df)
        else:
            # If dict but unknown structure, try to interpret as records if values look like rows
            raise RuntimeError("Endpoint JSON structure not recognized. Expected {records:[...]} or list of objects.")
    else:
        raise RuntimeError("Endpoint did not return valid JSON (dict or list).")

    df = pd.DataFrame(records or [])
    df = _normalize_df_strings(df)
    return df


def load_data_from_excel() -> pd.DataFrame:
    if not os.path.exists(EXCEL_PATH):
        raise FileNotFoundError(f"dataset.xlsx not found at: {EXCEL_PATH}")
    df = pd.read_excel(EXCEL_PATH)
    df = _normalize_df_strings(df)
    return df


def classify_and_prepare(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str], List[str]]:
    # Normalize Likert values (fix casing inconsistencies)
    likert_map = {
        "muy satisfecho": "Muy satisfecho",
        "satisfecho": "Satisfecho",
        "neutral": "Neutral",
        "insatisfecho": "Insatisfecho",
        "muy insatisfecho": "Muy insatisfecho",
    }

    # Identify question columns (skip metadata, open-text, and null columns)
    skip_keywords = [
        "marca temporal",
        "nombre del alumno",
        "campus",
        "nivel educativo",
        "grado",
        "¿por qué",
        "comentarios",
        "sugerencias",
    ]

    question_cols: List[str] = []
    for c in df.columns:
        if c is None or str(c) == "None":
            continue
        if any(kw in str(c).lower() for kw in skip_keywords):
            continue
        vals = df[c].dropna().unique()
        if len(vals) <= 10:
            question_cols.append(c)

    # Classify questions
    likert5_cols: List[str] = []
    yesno_cols: List[str] = []

    for c in question_cols:
        vals = set(str(v).strip().lower() for v in df[c].dropna().unique())

        if vals <= {"muy satisfecho", "satisfecho", "neutral", "insatisfecho", "muy insatisfecho"}:
            likert5_cols.append(c)
            df[c] = df[c].apply(
                lambda x: likert_map.get(str(x).strip().lower(), x) if pd.notna(x) else x
            )

        elif vals <= {"sí", "si", "no"}:
            yesno_cols.append(c)
            df[c] = df[c].apply(
                lambda x: (
                    "Sí" if str(x).strip().lower() in {"sí", "si"}
                    else ("No" if str(x).strip().lower() == "no" else x)
                )
                if pd.notna(x)
                else x
            )

    # Build plantel column: "Nivel Educativo – Campus"
    if "Nivel Educativo" in df.columns and "Campus" in df.columns:
        ne = _clean_text_series(df["Nivel Educativo"])
        ca = _clean_text_series(df["Campus"])
        df["plantel"] = ne + " – " + ca
    else:
        df["plantel"] = "Plantel"

    # Clean plantel itself (extra safety)
    df["plantel"] = _clean_text_series(df["plantel"])

    return df, likert5_cols, yesno_cols


def load_data() -> Tuple[pd.DataFrame, List[str], List[str], str]:
    """
    Returns df, likert5_cols, yesno_cols, source_label
    """
    if DATA_ENDPOINT_URL:
        df = load_data_from_endpoint()
        df, likert5_cols, yesno_cols = classify_and_prepare(df)
        return df, likert5_cols, yesno_cols, "endpoint"
    else:
        df = load_data_from_excel()
        df, likert5_cols, yesno_cols = classify_and_prepare(df)
        return df, likert5_cols, yesno_cols, "excel"


def clear_cache():
    global _df, _likert5_cols, _yesno_cols, _all_question_cols, _plantel_names, _loaded_at_epoch, _loaded_source
    _df = None
    _likert5_cols = None
    _yesno_cols = None
    _all_question_cols = None
    _plantel_names = None
    _loaded_at_epoch = None
    _loaded_source = "unknown"


def ensure_loaded(force: bool = False):
    global _df, _likert5_cols, _yesno_cols, _all_question_cols, _plantel_names, _loaded_at_epoch, _loaded_source

    now = time.time()
    if not force and _df is not None and _loaded_at_epoch is not None:
        if DATA_CACHE_TTL_SECONDS <= 0:
            return
        age = now - _loaded_at_epoch
        if age < DATA_CACHE_TTL_SECONDS:
            return

    df, likert5_cols, yesno_cols, source_label = load_data()

    _df = df
    _likert5_cols = likert5_cols
    _yesno_cols = yesno_cols
    _all_question_cols = likert5_cols + yesno_cols

    planteles = sorted(df["plantel"].dropna().unique().tolist()) if "plantel" in df.columns else []
    _plantel_names = planteles

    _loaded_at_epoch = now
    _loaded_source = source_label


def compute_results(sub_df: pd.DataFrame):
    total = int(len(sub_df))
    results = []

    # Defensive: if columns were not set for some reason
    if not _all_question_cols or not _likert5_cols or not _yesno_cols:
        return []

    for col in _all_question_cols:
        is_likert5 = col in _likert5_cols
        order = LIKERT5_ORDER if is_likert5 else YESNO_ORDER
        counts = sub_df[col].value_counts() if col in sub_df.columns else pd.Series(dtype=int)

        data = []
        for label in order:
            c = int(counts.get(label, 0))
            data.append(
                {
                    "label": label,
                    "count": c,
                    "pct": round(c / total * 100, 1) if total > 0 else 0,
                }
            )

        results.append(
            {
                "question": col,
                "type": "likert5" if is_likert5 else "yesno",
                "total": total,
                "data": data,
            }
        )

    return results


# ── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/health")
def api_health():
    try:
        ensure_loaded()
        return jsonify(
            {
                "ok": True,
                "rows": int(len(_df)) if _df is not None else 0,
                "planteles": int(len(_plantel_names)) if _plantel_names is not None else 0,
                "source": _loaded_source,
                "cache_ttl_seconds": DATA_CACHE_TTL_SECONDS,
                "loaded_at_epoch": _loaded_at_epoch,
                "endpoint_configured": bool(DATA_ENDPOINT_URL),
                "excel_path": EXCEL_PATH,
            }
        )
    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "error": str(e),
                "endpoint_configured": bool(DATA_ENDPOINT_URL),
                "excel_path": EXCEL_PATH,
            }
        ), 500


@app.route("/api/reload", methods=["POST"])
def api_reload():
    try:
        clear_cache()
        ensure_loaded(force=True)
        return jsonify(
            {
                "ok": True,
                "rows": int(len(_df)) if _df is not None else 0,
                "planteles": int(len(_plantel_names)) if _plantel_names is not None else 0,
                "source": _loaded_source,
                "loaded_at_epoch": _loaded_at_epoch,
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/planteles")
def api_planteles():
    # Keep EXACTLY the old behavior: list of strings.
    try:
        ensure_loaded()
        return jsonify(_plantel_names or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data")
def api_data_by_id():
    """
    Vercel-safe endpoint:
      /api/data?id=###   where ### is the index in /api/planteles list
    """
    try:
        ensure_loaded()

        raw_id = request.args.get("id", None)
        if raw_id is None:
            return jsonify({"error": "Missing query param ?id=PLANTEL_ID"}), 400

        try:
            pid = int(raw_id)
        except ValueError:
            return jsonify({"error": "Invalid id (must be integer)"}), 400

        if not _plantel_names:
            return jsonify({"error": "No planteles available"}), 404

        if pid < 0 or pid >= len(_plantel_names):
            return jsonify({"error": f"Unknown id {pid}"}), 404

        plantel_name = _plantel_names[pid]
        sub = _df[_df["plantel"] == plantel_name] if _df is not None else pd.DataFrame()
        return jsonify(compute_results(sub))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data_all")
def api_data_all():
    try:
        ensure_loaded()
        return jsonify(compute_results(_df if _df is not None else pd.DataFrame()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Legacy route (kept, but UI no longer uses it)
@app.route("/api/data/<plantel>")
def api_data_legacy(plantel):
    try:
        ensure_loaded()
        plantel_clean = (
            str(plantel)
            .replace("\u00A0", " ")
            .replace("\u2013", "–")
            .replace("\u2014", "–")
            .strip()
        )
        sub = _df[_df["plantel"] == plantel_clean] if _df is not None else pd.DataFrame()
        return jsonify(compute_results(sub))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── HTML Template ───────────────────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Evaluación de Calidad – Dashboard</title>

<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

  :root {
    --bg:        #f4f6f9;
    --surface:   #ffffff;
    --sidebar:   #1e293b;
    --accent:    #6366f1;
    --accent-light: #818cf8;
    --text:      #1e293b;
    --text-muted:#64748b;
    --border:    #e2e8f0;
    --radius:    16px;
  }

  *, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }

  body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    min-height: 100vh;
  }

  .sidebar {
    position: fixed; top:0; left:0; bottom:0;
    width: 310px;
    background: var(--sidebar);
    color: #f1f5f9;
    padding: 32px 24px;
    display: flex; flex-direction: column; gap: 28px;
    z-index: 100;
    overflow-y: auto;
  }
  .sidebar h1 {
    font-size: 1.15rem; font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.4;
    color: #fff;
  }
  .sidebar h1 span { color: var(--accent-light); }
  .sidebar label {
    font-size: 0.72rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: #94a3b8; margin-bottom: 6px; display: block;
  }
  .sidebar select, .sidebar button {
    width: 100%;
    padding: 10px 14px;
    border-radius: 10px;
    border: 1px solid #475569;
    background: #334155;
    color: #f1f5f9;
    font-family: inherit; font-size: 0.85rem;
    cursor: pointer;
    transition: all .15s;
  }
  .sidebar select:hover, .sidebar select:focus { border-color: var(--accent-light); outline:none; }
  .sidebar select option { background: #1e293b; }

  .ctrl-group { display: flex; flex-direction: column; gap: 6px; }

  .btn-primary {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
    font-weight: 600;
  }
  .btn-primary:hover { background: var(--accent-light) !important; }
  .btn-outline {
    background: transparent !important;
    border: 1.5px solid #475569 !important;
    color: #94a3b8 !important;
    font-weight: 500;
  }
  .btn-outline:hover { border-color: #94a3b8 !important; color: #f1f5f9 !important; }

  .sidebar-footer {
    margin-top: auto;
    padding-top: 20px;
    border-top: 1px solid #334155;
    font-size: 0.7rem;
    color: #64748b;
    line-height: 1.5;
  }

  .filter-section { display: flex; flex-direction: column; gap: 16px; }
  .chart-type-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .chart-type-grid button {
    padding: 7px 8px; font-size: 0.78rem; border-radius: 8px;
    background: #334155; border: 1px solid #475569; color: #cbd5e1;
  }
  .chart-type-grid button.active,
  .chart-type-grid button:hover {
    background: var(--accent); border-color: var(--accent); color: #fff;
  }

  .legend-preview {
    display: flex; flex-direction: column; gap: 5px;
    padding: 12px; background: #0f172a; border-radius: 10px;
  }
  .legend-row { display: flex; align-items: center; gap: 8px; font-size: 0.75rem; }
  .legend-dot { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }

  .main { margin-left: 310px; flex: 1; padding: 40px 48px; }

  .header-bar {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 36px;
  }
  .header-bar h2 { font-size: 1.6rem; font-weight: 700; }
  .header-bar .badge {
    background: var(--accent); color: #fff;
    padding: 6px 16px; border-radius: 50px;
    font-size: 0.82rem; font-weight: 600;
  }

  .stats-row {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px; margin-bottom: 36px;
  }
  .stat-card {
    background: var(--surface); border-radius: var(--radius);
    padding: 20px 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }
  .stat-card .stat-label { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; font-weight: 600; }
  .stat-card .stat-value { font-size: 1.8rem; font-weight: 800; margin-top: 4px; }

  .section-label {
    font-size: 0.75rem; text-transform: uppercase; letter-spacing: .08em;
    font-weight: 700; color: var(--text-muted);
    margin: 36px 0 20px; padding-bottom: 10px;
    border-bottom: 2px solid var(--border);
  }

  .charts-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
    gap: 24px;
  }

  .chart-card {
    background: var(--surface);
    border-radius: var(--radius);
    padding: 28px;
    box-shadow: 0 1px 4px rgba(0,0,0,.05);
    break-inside: avoid;
  }
  .chart-card h3 { font-size: 0.88rem; font-weight: 600; line-height: 1.45; margin-bottom: 18px; color: var(--text); }
  .chart-card .chart-wrap { position: relative; width: 100%; min-height: 250px; }
  .chart-card canvas { width: 100% !important; }

  .chart-card .summary-bar { display: flex; margin-top: 14px; border-radius: 8px; overflow: hidden; height: 10px; }
  .chart-card .detail-row { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
  .detail-pill { display: flex; align-items: center; gap: 5px; font-size: 0.72rem; color: var(--text-muted); }
  .detail-pill .dot { width: 8px; height: 8px; border-radius: 2px; }

  .loading-overlay {
    position: fixed; top:0;left:0;right:0;bottom:0;
    background: rgba(244,246,249,.85);
    display: flex; align-items: center; justify-content: center;
    z-index: 999; transition: opacity .3s;
  }
  .loading-overlay.hidden { opacity:0; pointer-events:none; }
  .spinner {
    width: 40px; height: 40px;
    border: 4px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .error-banner {
    margin: 14px 0 0;
    padding: 12px 14px;
    border-radius: 10px;
    background: #fee2e2;
    border: 1px solid #fecaca;
    color: #7f1d1d;
    font-size: 0.9rem;
    line-height: 1.35;
    white-space: pre-wrap;
  }
</style>
</head>

<body>
<aside class="sidebar">
  <div><h1>📊 Evaluación de <span>Calidad</span> del Servicio</h1></div>

  <div class="filter-section">
    <div class="ctrl-group">
      <label>Plantel</label>
      <select id="selPlantel"></select>
    </div>

    <div class="ctrl-group">
      <label>Tipo de gráfica</label>
      <div class="chart-type-grid" id="chartTypes">
        <button data-type="bar" class="active">Barras</button>
        <button data-type="horizontalBar">H-Barras</button>
        <button data-type="doughnut">Dona</button>
        <button data-type="pie">Pastel</button>
        <button data-type="polarArea">Polar</button>
        <button data-type="radar">Radar</button>
      </div>
    </div>

    <div class="ctrl-group">
      <label>Filtrar preguntas</label>
      <select id="selFilter">
        <option value="all">Todas las preguntas</option>
        <option value="likert5">Solo escala de satisfacción (5 pts)</option>
        <option value="yesno">Solo Sí / No</option>
      </select>
    </div>
  </div>

  <div class="ctrl-group">
    <label>Código de colores</label>
    <div class="legend-preview" id="legendPreview"></div>
  </div>

  <div style="display:flex;flex-direction:column;gap:8px;">
    <button class="btn-primary" onclick="handlePrint()">🖨️ Imprimir / Guardar PDF</button>
    <button class="btn-outline" onclick="handleReload()">↻ Recargar datos</button>
  </div>

  <div class="sidebar-footer">
    Dashboard generado a partir de las respuestas de la evaluación institucional.<br>
    Datos cargados desde <strong>Google Sheets (Apps Script endpoint)</strong>
  </div>
</aside>

<div class="main" id="mainContent">
  <div class="loading-overlay" id="loader"><div class="spinner"></div></div>

  <div class="header-bar">
    <h2 id="titlePlantel">Cargando…</h2>
    <span class="badge" id="badgeN">–</span>
  </div>

  <div id="clientError" class="error-banner" style="display:none;"></div>
  <div class="stats-row" id="statsRow"></div>
  <div id="sectionsContainer"></div>
</div>

<script>
(function () {
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.async = true;
      s.onload = () => resolve(src);
      s.onerror = () => reject(new Error('Failed to load: ' + src));
      document.head.appendChild(s);
    });
  }

  async function loadFirstAvailable(sources) {
    let lastErr = null;
    for (const src of sources) {
      try { await loadScript(src); return src; }
      catch (e) { lastErr = e; }
    }
    throw lastErr || new Error('No sources provided');
  }

  function showLoader(show) {
    const loader = document.getElementById('loader');
    if (!loader) return;
    loader.classList.toggle('hidden', !show);
  }

  function showError(msg) {
    const el = document.getElementById('clientError');
    if (!el) return;
    el.style.display = 'block';
    el.textContent = msg;
  }

  async function boot() {
    showLoader(true);

    await loadFirstAvailable([
      'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js',
      'https://unpkg.com/chart.js@4.4.1/dist/chart.umd.min.js'
    ]);
    if (!window.Chart) throw new Error('Chart.js loaded but window.Chart is missing.');

    await loadFirstAvailable([
      'https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js',
      'https://unpkg.com/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js'
    ]);
    if (!window.ChartDataLabels) throw new Error('DataLabels loaded but window.ChartDataLabels is missing.');

    window.Chart.register(window.ChartDataLabels);

    initDashboard();
  }

  boot().catch(err => {
    console.error(err);
    showError('No se pudieron cargar las librerías (Chart.js / DataLabels).\n' + (err?.message || String(err)));
    showLoader(false);
  });

  function initDashboard() {
    const LIKERT5_COLORS = {
      'Muy satisfecho':  { bg: '#4ade80', border: '#22c55e' },
      'Satisfecho':      { bg: '#86efac', border: '#4ade80' },
      'Neutral':         { bg: '#fde047', border: '#facc15' },
      'Insatisfecho':    { bg: '#fca5a5', border: '#f87171' },
      'Muy insatisfecho':{ bg: '#f87171', border: '#ef4444' },
    };
    const YESNO_COLORS = {
      'Sí': { bg: '#4ade80', border: '#22c55e' },
      'No': { bg: '#f87171', border: '#ef4444' },
    };

    function getColors(type, labels) {
      const map = type === 'likert5' ? LIKERT5_COLORS : YESNO_COLORS;
      return {
        bg: labels.map(l => (map[l]||{bg:'#cbd5e1'}).bg),
        border: labels.map(l => (map[l]||{border:'#94a3b8'}).border),
      };
    }

    function showLoader(show) {
      document.getElementById('loader').classList.toggle('hidden', !show);
    }

    function showError(msg) {
      const el = document.getElementById('clientError');
      el.style.display = 'block';
      el.textContent = msg;
    }

    window.handlePrint = function handlePrint() {
      setTimeout(() => window.print(), 300);
    };

    window.handleReload = async function handleReload() {
      showLoader(true);
      try {
        await fetch('/api/reload', { method: 'POST', cache: 'no-store' });
      } catch (e) {
        // If reload fails, still refresh the page
      } finally {
        location.reload();
      }
    };

    function updateLegend(type) {
      const map = type === 'likert5' ? LIKERT5_COLORS : YESNO_COLORS;
      const el = document.getElementById('legendPreview');
      el.innerHTML = '';
      Object.entries(map).forEach(([label, c]) => {
        const row = document.createElement('div');
        row.className = 'legend-row';
        row.innerHTML = `<span class="legend-dot" style="background:${c.bg}"></span><span>${label}</span>`;
        el.appendChild(row);
      });
    }

    let chartInstances = [];
    let currentData = [];
    let currentChartType = 'bar';
    let currentFilter = 'all';

    // Render sequence token to avoid late chart creation when switching chart types quickly
    let renderSeq = 0;

    async function startApp() {
      try {
        const health = await fetch('/api/health', { cache: 'no-store' });
        if (!health.ok) {
          const t = await health.text();
          throw new Error('Backend health failed: ' + t);
        }

        const res = await fetch('/api/planteles', { cache: 'no-store' });
        const planteles = await res.json();
        if (!res.ok || (planteles && planteles.error)) {
          throw new Error(planteles?.error || 'Error cargando /api/planteles');
        }

        const sel = document.getElementById('selPlantel');
        sel.innerHTML = '';

        const optAll = document.createElement('option');
        optAll.value = '__ALL__';
        optAll.textContent = '🏫 Todos los planteles';
        sel.appendChild(optAll);

        planteles.forEach((name, idx) => {
          const o = document.createElement('option');
          o.value = String(idx);
          o.textContent = name;
          sel.appendChild(o);
        });

        sel.value = '__ALL__';
        sel.addEventListener('change', () => loadPlantel(sel.value));

        document.querySelectorAll('#chartTypes button').forEach(btn => {
          btn.addEventListener('click', () => {
            document.querySelectorAll('#chartTypes button').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentChartType = btn.dataset.type;
            renderCharts();
          });
        });

        document.getElementById('selFilter').addEventListener('change', e => {
          currentFilter = e.target.value;
          renderCharts();
        });

        updateLegend('likert5');
        await loadPlantel(sel.value);
      } catch (e) {
        console.error(e);
        showError('Error inicializando:\n' + (e?.message || String(e)));
        showLoader(false);
      }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', startApp);
    } else {
      startApp();
    }

    async function loadPlantel(value) {
      showLoader(true);
      try {
        let url;
        let displayName;

        const sel = document.getElementById('selPlantel');
        displayName = sel.options[sel.selectedIndex]?.textContent || 'Plantel';

        if (value === '__ALL__') {
          url = '/api/data_all';
          displayName = 'Todos los Planteles';
        } else {
          url = `/api/data?id=${encodeURIComponent(value)}`;
        }

        const res = await fetch(url, { cache: 'no-store' });
        const payload = await res.json();

        if (!res.ok || (payload && payload.error)) {
          throw new Error(payload?.error || ('Error cargando ' + url));
        }

        currentData = payload;

        document.getElementById('titlePlantel').textContent = displayName;
        const total = currentData.length > 0 ? currentData[0].total : 0;
        document.getElementById('badgeN').textContent = `${total} respuestas`;

        renderStats();
        renderCharts();
      } catch (e) {
        console.error(e);
        showError('Error cargando datos:\n' + (e?.message || String(e)));
      } finally {
        showLoader(false);
      }
    }

    function renderStats() {
      const row = document.getElementById('statsRow');
      row.innerHTML = '';

      const likert5 = currentData.filter(d => d.type === 'likert5');
      const yesno   = currentData.filter(d => d.type === 'yesno');

      if (likert5.length) {
        let totalPositive = 0, totalAll = 0;
        likert5.forEach(q => {
          const ms = q.data.find(d => d.label === 'Muy satisfecho')?.count || 0;
          const s  = q.data.find(d => d.label === 'Satisfecho')?.count || 0;
          const all = q.data.reduce((a,b) => a + b.count, 0);
          totalPositive += ms + s;
          totalAll += all;
        });
        const pct = totalAll > 0 ? (totalPositive / totalAll * 100).toFixed(1) : 0;
        addStat(row, 'Satisfacción positiva', pct + '%', pct >= 70 ? '#4ade80' : pct >= 50 ? '#fde047' : '#f87171');
      }

      if (yesno.length) {
        let totalSi = 0, totalAll = 0;
        yesno.forEach(q => {
          totalSi  += q.data.find(d => d.label === 'Sí')?.count || 0;
          totalAll += q.data.reduce((a,b) => a + b.count, 0);
        });
        const pct = totalAll > 0 ? (totalSi / totalAll * 100).toFixed(1) : 0;
        addStat(row, 'Respuestas "Sí"', pct + '%', pct >= 70 ? '#4ade80' : pct >= 50 ? '#fde047' : '#f87171');
      }

      addStat(row, 'Preguntas de satisfacción', likert5.length, '#818cf8');
      addStat(row, 'Preguntas Sí/No', yesno.length, '#818cf8');
    }

    function addStat(container, label, value, color) {
      const d = document.createElement('div');
      d.className = 'stat-card';
      d.innerHTML = `<div class="stat-label">${label}</div><div class="stat-value" style="color:${color}">${value}</div>`;
      container.appendChild(d);
    }

    function renderCharts() {
      renderSeq += 1;
      const seq = renderSeq;

      chartInstances.forEach(c => c.destroy());
      chartInstances = [];

      const container = document.getElementById('sectionsContainer');
      container.innerHTML = '';

      const filtered = currentData.filter(d => currentFilter === 'all' ? true : d.type === currentFilter);
      const likert5 = filtered.filter(d => d.type === 'likert5');
      const yesno   = filtered.filter(d => d.type === 'yesno');

      if (likert5.length) {
        container.innerHTML += `<div class="section-label">Preguntas de Satisfacción (Escala Likert 5 puntos)</div>`;
        const grid = document.createElement('div');
        grid.className = 'charts-grid';
        container.appendChild(grid);
        likert5.forEach((q, i) => grid.appendChild(buildCard(q, i, seq)));
        updateLegend('likert5');
      }

      if (yesno.length) {
        container.innerHTML += `<div class="section-label">Preguntas Sí / No</div>`;
        const grid = document.createElement('div');
        grid.className = 'charts-grid';
        container.appendChild(grid);
        yesno.forEach((q, i) => grid.appendChild(buildCard(q, i + likert5.length, seq)));
        if (!likert5.length) updateLegend('yesno');
      }
    }

    function hexToRgba(hex, alpha) {
      const h = (hex || '').replace('#','').trim();
      if (h.length !== 6) return `rgba(148,163,184,${alpha})`;
      const r = parseInt(h.slice(0,2), 16);
      const g = parseInt(h.slice(2,4), 16);
      const b = parseInt(h.slice(4,6), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }

    function buildCard(q, idx, seq) {
      const card = document.createElement('div');
      card.className = 'chart-card';

      const labels = q.data.map(d => d.label);
      const counts = q.data.map(d => d.count);
      const pcts   = q.data.map(d => d.pct);
      const colors = getColors(q.type, labels);

      const h3 = document.createElement('h3');
      h3.textContent = q.question;
      card.appendChild(h3);

      const totalCount = counts.reduce((a,b) => a+b, 0);
      const bar = document.createElement('div');
      bar.className = 'summary-bar';
      q.data.forEach((d, i) => {
        const seg = document.createElement('div');
        const w = totalCount > 0 ? (d.count / totalCount * 100) : 0;
        seg.style.width = w + '%';
        seg.style.background = colors.bg[i];
        seg.title = `${d.label}: ${d.count} (${d.pct}%)`;
        bar.appendChild(seg);
      });
      card.appendChild(bar);

      const detailRow = document.createElement('div');
      detailRow.className = 'detail-row';
      q.data.forEach((d, i) => {
        const pill = document.createElement('span');
        pill.className = 'detail-pill';
        pill.innerHTML = `<span class="dot" style="background:${colors.bg[i]}"></span>${d.label}: <strong>${d.count}</strong> (${d.pct}%)`;
        detailRow.appendChild(pill);
      });
      card.appendChild(detailRow);

      const wrap = document.createElement('div');
      wrap.className = 'chart-wrap';
      const canvas = document.createElement('canvas');
      canvas.id = 'chart_' + idx;
      wrap.appendChild(canvas);
      card.appendChild(wrap);

      requestAnimationFrame(() => {
        // If a newer render started, abort creating this chart
        if (seq !== renderSeq) return;
        if (!canvas.isConnected) return;
        if (!window.Chart) return;

        const originalType = currentChartType;
        const isCartesian = ['bar', 'horizontalBar'].includes(originalType);
        const isRadar = originalType === 'radar';

        const chartType = (originalType === 'horizontalBar') ? 'bar' : originalType;

        // Destroy any existing chart bound to this canvas (safety)
        const existing = window.Chart.getChart(canvas);
        if (existing) existing.destroy();

        let dataset;

        if (chartType === 'radar') {
          // Radar works better with a single fill color + per-point colors
          dataset = {
            data: counts,
            backgroundColor: hexToRgba(colors.bg[0] || '#cbd5e1', 0.25),
            borderColor: colors.border[0] || '#94a3b8',
            borderWidth: 2,
            pointBackgroundColor: colors.bg,
            pointBorderColor: colors.border,
            pointRadius: 4,
            pointHoverRadius: 5,
            fill: true,
          };
        } else if (chartType === 'bar') {
          dataset = {
            data: counts,
            backgroundColor: colors.bg.map(c => c + 'cc'),
            borderColor: colors.border,
            borderWidth: 2,
            borderRadius: 8,
            hoverBackgroundColor: colors.bg,
          };
        } else {
          // pie/doughnut/polarArea
          dataset = {
            data: counts,
            backgroundColor: colors.bg.map(c => c + 'cc'),
            borderColor: colors.border,
            borderWidth: 2,
            hoverBackgroundColor: colors.bg,
          };
        }

        const options = {
          responsive: true,
          maintainAspectRatio: true,
          layout: { padding: { top: 10, bottom: 4 } },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#1e293b',
              cornerRadius: 10,
              padding: 12,
              callbacks: {
                label: ctx => {
                  const i = ctx.dataIndex;
                  return `${labels[i]}: ${counts[i]} (${pcts[i]}%)`;
                }
              }
            },
            datalabels: {
              color: (isCartesian || isRadar) ? '#1e293b' : '#fff',
              font: { family: 'Inter', weight: '700', size: 12 },
              anchor: isCartesian ? 'end' : 'center',
              align: isCartesian ? (originalType === 'horizontalBar' ? 'end' : 'top') : 'center',
              offset: isCartesian ? 4 : 0,
              formatter: (val, ctx) => {
                const p = pcts[ctx.dataIndex];
                return val > 0 ? `${p}%` : '';
              },
            }
          },
          animation: { duration: 600 }
        };

        if (isCartesian) {
          options.indexAxis = (originalType === 'horizontalBar') ? 'y' : 'x';
          options.scales = {
            x: { grid: { display: false }, ticks: { color: '#64748b' } },
            y: { grid: { color: '#f1f5f9' }, ticks: { color: '#64748b' }, beginAtZero: true }
          };
        } else if (isRadar) {
          options.scales = {
            r: { beginAtZero: true, ticks: { display: false }, grid: { color: '#e2e8f0' } }
          };
        }

        const cfg = {
          type: chartType,
          data: { labels, datasets: [dataset] },
          options
        };

        const chart = new window.Chart(canvas.getContext('2d'), cfg);
        chartInstances.push(chart);
      });

      return card;
    }
  }
})();
</script>

</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
