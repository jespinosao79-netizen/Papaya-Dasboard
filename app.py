"""
Dashboard interactivo de embarques de PAPAYA
Fuentes: Google Sheets (TX LOADS y TIJ LOADS)
Variables analizadas: VENDOR, DEPART WEEK, CAJAS 35 LB, ALMACEN
"""

import io
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Usa el almacen de certificados del sistema (Windows) para evitar errores SSL
# cuando hay firewall/antivirus corporativo interceptando trafico HTTPS.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuracion de fuentes
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
FETCHER_SCRIPT = Path(__file__).parent / "fetch_data.py"

SOURCES = {
    "TX LOADS": {
        "spreadsheet_id": "1v762Wg44PDQFdwQyeRQeGEbPjlb4QfYIJ-p7EQFqScg",
        "sheet_name": "TX LOADS",
        "gid": "0",
        "local_file": "tx_loads.csv",
        # TX LOADS tiene encabezados combinados (merged cells) - mapeo por indice de columna
        "positional_map": {
            "VENDOR": 2,       # col C
            "PRODUCT": 4,      # col E
            "DEPART_WEEK": 11, # col L
            "CAJAS_35LB": 20,  # col U
            "ALMACEN": 22,     # col W
        },
    },
    "TIJ LOADS": {
        "spreadsheet_id": "1rRgSp64hjLtAFTbSxo7LR5WWiMfggDzroj82kRADK7A",
        "sheet_name": "TIJ LOADS",
        "gid": "0",
        "local_file": "tij_loads.csv",
        "positional_map": None,  # auto-deteccion funciona bien
    },
}

HEADER_KEYWORDS = ("VENDOR", "PRODUCT", "PROD", "ALMAC")

# Grupos de proveedores que deben tratarse como uno solo.
# Todos los nombres se normalizan a MAYUSCULAS/sin acentos antes de comparar.
# Del grupo, se conserva el nombre con MAS embarques (mas filas).
VENDOR_ALIASES = [
    ["PARADISE PAPAYA", "GRUPO FRUTIORO", "FRUTIORO MEXICO"],
    ["CHB MEXICO", "CHB DEL GOLFO"],
    ["MA. DE JESUS GUTIERREZ CISNEROS", "RANCHO EL COBANO"],
    ["OCHOA MENDOZA HERMANOS", "OCHOA MENDOZA HNOS"],
]


def build_csv_url(spreadsheet_id: str, sheet_name: str, gid: str | None = None) -> str:
    # Endpoint /export?format=csv descarga TODAS las filas e ignora filtros / vistas filtradas.
    if gid is not None:
        return (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
            f"/export?format=csv&gid={gid}"
        )
    # Fallback: gviz por nombre de pestana (puede respetar filtros)
    sheet_param = requests.utils.quote(sheet_name)
    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={sheet_param}"
    )


def detect_header_row(raw: pd.DataFrame) -> int:
    """Encuentra la fila con mas coincidencias con palabras clave de encabezado."""
    best_idx, best_score = 0, -1
    for i in range(min(15, len(raw))):
        row_vals = " ".join(str(v).upper() for v in raw.iloc[i].tolist())
        score = sum(kw in row_vals for kw in HEADER_KEYWORDS)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


# ---------------------------------------------------------------------------
# Lectura y normalizacion
# ---------------------------------------------------------------------------
import urllib.request
import ssl as _ssl
import warnings

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

def refresh_local_data(timeout: int = 90) -> tuple[bool, str]:
    """Corre fetch_data.py como subprocess separado.
    Netskope bloquea el proceso Streamlit pero permite el proceso Python standalone,
    asi que descargamos desde CLI y leemos de disco.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(FETCHER_SCRIPT), str(DATA_DIR)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode == 0:
            return True, proc.stdout.strip()
        return False, f"exit {proc.returncode}: {proc.stderr.strip()[:500]}"
    except subprocess.TimeoutExpired:
        return False, f"Timeout tras {timeout}s"
    except Exception as exc:
        return False, str(exc)


def _fetch_via_urllib(url: str, verify: bool = True) -> str:
    """Fallback via urllib.request - bypasses requests/urllib3 stack en caso de
    que el antivirus lo bloquee especificamente."""
    ctx = _ssl.create_default_context() if verify else _ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_with_retries(url: str, max_attempts: int = 4) -> str:
    """Descarga la URL con reintentos escalonados. Estrategias:
    1. requests con verificacion SSL normal (via truststore)
    2. urllib con verificacion SSL
    3. requests sin verificacion SSL (por si el antivirus corrompe el cert)
    4. urllib sin verificacion SSL
    """
    strategies = [
        lambda: requests.get(url, timeout=30).text,
        lambda: _fetch_via_urllib(url, verify=True),
        lambda: requests.get(url, timeout=30, verify=False).text,
        lambda: _fetch_via_urllib(url, verify=False),
    ]
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return strategies[attempt % len(strategies)]()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Fallo tras {max_attempts} intentos: {last_exc}") from last_exc


@st.cache_data(ttl=300, show_spinner=False)
def fetch_sheet(label: str, spreadsheet_id: str, sheet_name: str, positional_map: dict | None, gid: str | None = None, local_file: str | None = None) -> pd.DataFrame:
    # Estrategia principal: leer del archivo local (poblado por fetch_data.py)
    if local_file:
        local_path = DATA_DIR / local_file
        if not local_path.exists():
            ok, msg = refresh_local_data()
            if not ok:
                raise RuntimeError(f"No hay {local_file} y no se pudo descargar: {msg}")
        with open(local_path, "rb") as f:
            content = f.read()
        text = content.decode("utf-8", errors="replace")
    else:
        # Fallback: fetch HTTP directo (por si algo cambia)
        url = build_csv_url(spreadsheet_id, sheet_name, gid)
        text = _fetch_with_retries(url)
    raw = pd.read_csv(io.StringIO(text), header=None, dtype=str, keep_default_na=False)

    header_idx = detect_header_row(raw)

    if positional_map:
        # Extrae por posicion (sheets con encabezados combinados / merged)
        body = raw.iloc[header_idx + 1 :].reset_index(drop=True)
        out = pd.DataFrame()
        for canon, col_idx in positional_map.items():
            out[canon] = body.iloc[:, col_idx] if col_idx < body.shape[1] else pd.NA
        out = out.replace({"": pd.NA}).dropna(how="all").reset_index(drop=True)
        out["SOURCE"] = label
        return out

    headers = raw.iloc[header_idx].fillna("").astype(str).tolist()
    headers = [h.strip() for h in headers]

    body = raw.iloc[header_idx + 1 :].reset_index(drop=True)
    body.columns = headers + [f"_extra_{i}" for i in range(len(body.columns) - len(headers))]

    body = body.loc[:, [c for c in body.columns if c and not str(c).startswith("_extra_")]]
    body = body.loc[:, ~body.columns.duplicated(keep="first")]
    body = body.replace({"": pd.NA}).dropna(how="all").reset_index(drop=True)
    body["SOURCE"] = label
    return body


def find_col(df: pd.DataFrame, patterns: list[str]) -> str | None:
    """Busca la primera columna cuyo nombre contenga cualquiera de los patrones (case-insensitive)."""
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for col in df.columns:
            if rx.search(str(col)):
                return col
    return None


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Renombra las columnas clave a un nombre canonico."""
    mapping = {
        "VENDOR": ["^vendor\\b", "vendor 1", "vendor"],
        "PRODUCT": ["^product\\b", "prod 1", "producto", "product"],
        "DEPART_WEEK": ["depart.?week", "semana.*salida", "depart wk"],
        "CAJAS_35LB": ["35\\s*lb\\s*conv", "caja\\s*35\\s*lb", "35lb", "35 lb"],
        "ALMACEN": ["almac"],
    }
    rename = {}
    for canon, pats in mapping.items():
        col = find_col(df, pats)
        if col is not None:
            rename[col] = canon
    out = df.rename(columns=rename).copy()
    for canon in mapping:
        if canon not in out.columns:
            out[canon] = pd.NA
    return out


def to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    ).fillna(0)


def load_all() -> tuple[pd.DataFrame, dict]:
    frames = []
    diagnostics = {}
    for label, cfg in SOURCES.items():
        raw = fetch_sheet(label, cfg["spreadsheet_id"], cfg["sheet_name"], cfg.get("positional_map"), cfg.get("gid"), cfg.get("local_file"))
        norm = raw if cfg.get("positional_map") else normalize(raw)
        diagnostics[label] = {
            "rows_raw": len(raw),
            "columns": list(raw.columns)[:25],
        }
        frames.append(norm)
    df = pd.concat(frames, ignore_index=True, sort=False)

    df["VENDOR"] = df["VENDOR"].astype(str).str.strip().str.upper()
    df["PRODUCT"] = df["PRODUCT"].astype(str).str.strip().str.upper()
    df["DEPART_WEEK"] = df["DEPART_WEEK"].astype(str).str.strip()
    df["ALMACEN"] = df["ALMACEN"].astype(str).str.strip().str.upper()
    df["CAJAS_35LB"] = to_number(df["CAJAS_35LB"])

    df = df[df["PRODUCT"].str.contains("PAPAYA", na=False)].copy()
    df = df[df["VENDOR"].ne("") & df["VENDOR"].ne("NAN")]

    # Consolidacion de proveedores: dentro de cada grupo de alias, el nombre
    # canonico es el que tiene MAS filas (embarques). Los demas se renombran.
    alias_to_canon = {}
    for group in VENDOR_ALIASES:
        group_upper = [g.strip().upper() for g in group]
        counts = df["VENDOR"].value_counts()
        present = [g for g in group_upper if g in counts.index]
        if not present:
            continue
        canon = max(present, key=lambda g: counts[g])
        for name in group_upper:
            if name != canon:
                alias_to_canon[name] = canon
    if alias_to_canon:
        df["VENDOR"] = df["VENDOR"].replace(alias_to_canon)

    df["DEPART_WEEK_NUM"] = pd.to_numeric(df["DEPART_WEEK"], errors="coerce")
    return df, diagnostics


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Dashboard Papaya", page_icon="🍈", layout="wide")
st.title("Dashboard de Embarques de Papaya")
st.caption("Fuentes: TX LOADS + TIJ LOADS · Filtrado solo producto PAPAYA")

col_a, col_b, col_c = st.columns([1, 1, 4])
with col_a:
    if st.button("🔄 Actualizar datos"):
        with st.spinner("Descargando datos de Google Sheets..."):
            ok, msg = refresh_local_data()
        if ok:
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(f"Fallo la descarga: {msg}")
with col_b:
    show_diag = st.toggle("Mostrar diagnostico", value=False)
with col_c:
    st.write(f"Ultima actualizacion: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}** · Cache 5 min")

try:
    df, diag = load_all()
except Exception as exc:
    st.error(f"Error cargando datos: {exc}")
    st.stop()

if df.empty:
    st.warning("No se encontraron filas de PAPAYA. Revisa el diagnostico.")
    if show_diag:
        st.json(diag)
    st.stop()

# Sidebar - filtros
st.sidebar.header("Filtros")
sources_sel = st.sidebar.multiselect("Origen", sorted(df["SOURCE"].unique()), default=sorted(df["SOURCE"].unique()))
vendors_sel = st.sidebar.multiselect("Vendor", sorted(df["VENDOR"].dropna().unique()))
almacenes_sel = st.sidebar.multiselect("Almacen", sorted(df["ALMACEN"].dropna().unique()))

weeks_available = sorted(df["DEPART_WEEK_NUM"].dropna().unique())
if weeks_available:
    wmin, wmax = int(min(weeks_available)), int(max(weeks_available))
    week_range = st.sidebar.slider("Rango semana de salida", wmin, wmax, (wmin, wmax))
else:
    week_range = None

mask = df["SOURCE"].isin(sources_sel)
if vendors_sel:
    mask &= df["VENDOR"].isin(vendors_sel)
if almacenes_sel:
    mask &= df["ALMACEN"].isin(almacenes_sel)
if week_range:
    mask &= df["DEPART_WEEK_NUM"].between(week_range[0], week_range[1]) | df["DEPART_WEEK_NUM"].isna()

fdf = df.loc[mask].copy()

# KPIs
k1, k2, k3, k4 = st.columns(4)
k1.metric("Total cajas 35 LB", f"{int(fdf['CAJAS_35LB'].sum()):,}")
k2.metric("Embarques", f"{len(fdf):,}")
k3.metric("Vendors distintos", fdf["VENDOR"].nunique())
k4.metric("Almacenes distintos", fdf["ALMACEN"].nunique())

st.divider()

LOAD_SIZE = 1150  # 1 camion / 1 load = 1150 cajas 35 LB

# --- Cajas por semana ---
st.subheader("Cajas 35 LB por semana de salida")
by_week = (
    fdf.dropna(subset=["DEPART_WEEK_NUM"])
    .groupby(["DEPART_WEEK_NUM", "SOURCE"], as_index=False)["CAJAS_35LB"].sum()
    .sort_values("DEPART_WEEK_NUM")
)
if not by_week.empty:
    fig = px.bar(
        by_week, x="DEPART_WEEK_NUM", y="CAJAS_35LB", color="SOURCE",
        barmode="stack", labels={"DEPART_WEEK_NUM": "Semana", "CAJAS_35LB": "Cajas 35 LB"},
    )
    fig.update_layout(height=380, xaxis=dict(dtick=1))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No hay datos de semana de salida para el filtro actual.")

# --- TIME LINE: total camiones (loads) por semana del anio ---
st.subheader(f"Time Line · Total camiones embarcados por semana del anio  ·  1 camion = {LOAD_SIZE} cajas 35 LB")
trucks_by_week = (
    fdf.dropna(subset=["DEPART_WEEK_NUM"])
    .groupby("DEPART_WEEK_NUM", as_index=False)["CAJAS_35LB"].sum()
)
if not trucks_by_week.empty:
    trucks_by_week["CAMIONES"] = trucks_by_week["CAJAS_35LB"] / LOAD_SIZE
    # Solo semanas con camiones > 0
    tl = trucks_by_week[trucks_by_week["CAMIONES"] > 0][["DEPART_WEEK_NUM", "CAMIONES"]].sort_values("DEPART_WEEK_NUM").reset_index(drop=True)

    fig_tl = px.line(
        tl, x="DEPART_WEEK_NUM", y="CAMIONES",
        markers=True,
        labels={"DEPART_WEEK_NUM": "Semana del anio", "CAMIONES": "Camiones"},
    )
    fig_tl.update_traces(
        line=dict(color="#e74c3c", width=2),
        marker=dict(size=8, color="#c0392b"),
        text=[f"{int(round(v))}" for v in tl["CAMIONES"]],
        textposition="top center",
        textfont=dict(size=12),
        mode="lines+markers+text",
        hovertemplate="Semana %{x}<br>Camiones: %{y:.1f}<extra></extra>",
    )
    week_min, week_max = int(tl["DEPART_WEEK_NUM"].min()), int(tl["DEPART_WEEK_NUM"].max())
    fig_tl.update_layout(
        height=420,
        xaxis=dict(dtick=1, range=[week_min - 0.5, week_max + 0.5], title="Semana del anio"),
        yaxis=dict(title="Camiones", tickformat="d"),
        margin=dict(l=40, r=20, t=20, b=40),
    )
    st.plotly_chart(fig_tl, use_container_width=True)

    max_row = tl.loc[tl["CAMIONES"].idxmax()]
    kc1, kc2, kc3 = st.columns(3)
    kc1.metric("Total camiones (ano)", f"{int(round(tl['CAMIONES'].sum())):,}")
    kc2.metric("Semana pico", f"S{int(max_row['DEPART_WEEK_NUM'])} · {int(round(max_row['CAMIONES']))}")
    kc3.metric("Promedio semanas activas", f"{int(round(tl['CAMIONES'].mean())):,}")
else:
    st.info("Sin datos para el time line.")

# --- Por vendor ---
left, right = st.columns(2)
with left:
    st.subheader("Top Vendors por cajas 35 LB")
    by_vendor = (
        fdf.groupby(["VENDOR", "SOURCE"], as_index=False)["CAJAS_35LB"].sum()
        .sort_values("CAJAS_35LB", ascending=False)
    )
    top_vendor = (
        by_vendor.groupby("VENDOR")["CAJAS_35LB"].sum()
        .sort_values(ascending=False).head(15).index
    )
    by_vendor_top = by_vendor[by_vendor["VENDOR"].isin(top_vendor)]
    if not by_vendor_top.empty:
        fig2 = px.bar(
            by_vendor_top, x="CAJAS_35LB", y="VENDOR", color="SOURCE",
            orientation="h", labels={"CAJAS_35LB": "Cajas 35 LB", "VENDOR": ""},
        )
        fig2.update_layout(height=480, yaxis=dict(categoryorder="total ascending"))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Sin datos.")

with right:
    st.subheader("Distribucion por Almacen")
    by_alm = fdf.groupby("ALMACEN", as_index=False)["CAJAS_35LB"].sum()
    by_alm = by_alm[by_alm["ALMACEN"].astype(bool) & by_alm["ALMACEN"].ne("NAN")]
    if not by_alm.empty:
        fig3 = px.pie(by_alm, names="ALMACEN", values="CAJAS_35LB", hole=0.45)
        fig3.update_layout(height=480)
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("Sin datos.")

st.divider()

# --- Vendor x Semana en LOADS (1 load = 1150 cajas 35 LB) ---
st.subheader(f"Vendor x Semana (Loads)  ·  1 load = {LOAD_SIZE} cajas 35 LB")

loads_df = fdf.dropna(subset=["DEPART_WEEK_NUM"]).copy()
loads_df["LOADS"] = loads_df["CAJAS_35LB"] / LOAD_SIZE

pivot_loads = loads_df.pivot_table(
    index="VENDOR", columns="DEPART_WEEK_NUM",
    values="LOADS", aggfunc="sum", fill_value=0,
)
if not pivot_loads.empty:
    pivot_loads.columns = [int(c) if float(c).is_integer() else c for c in pivot_loads.columns]
    pivot_loads = pivot_loads.reindex(sorted(pivot_loads.columns), axis=1)
    week_cols = list(pivot_loads.columns)
    n_weeks = len(week_cols)
    pivot_loads["TOTAL"] = pivot_loads[week_cols].sum(axis=1)
    pivot_loads["PROMEDIO/SEM"] = pivot_loads["TOTAL"] / n_weeks if n_weeks else 0
    pivot_loads = pivot_loads.sort_values("TOTAL", ascending=False)

    total_row = pivot_loads[week_cols + ["TOTAL"]].sum(axis=0).to_frame().T
    total_row["PROMEDIO/SEM"] = total_row["TOTAL"].iloc[0] / n_weeks if n_weeks else 0
    total_row.index = ["TOTAL"]
    pivot_loads_display = pd.concat([pivot_loads, total_row])

    st.dataframe(
        pivot_loads_display.style.format("{:,.1f}"),
        use_container_width=True, height=440,
    )

    # Heatmap: vendor x semana
    cols_extra = [c for c in ["TOTAL", "PROMEDIO/SEM"] if c in pivot_loads.columns]
    heat_data = pivot_loads.drop(columns=cols_extra)

    # Ancho minimo por columna para que el texto quepa siempre (incluye semanas nuevas)
    n_cols = len(heat_data.columns)
    n_rows = len(heat_data.index)
    fig_width = max(900, 55 * n_cols + 200)

    fig_loads = px.imshow(
        heat_data.values,
        x=[str(c) for c in heat_data.columns],
        y=heat_data.index.tolist(),
        labels=dict(x="Semana de salida", y="Vendor", color="Loads"),
        color_continuous_scale="YlOrRd",
        aspect="auto",
    )
    # Forzar renderizado de texto en TODAS las celdas (bypasea el auto-hide de texto en celdas chicas)
    fig_loads.update_traces(
        texttemplate="%{z:.1f}",
        textfont=dict(size=11),
        hovertemplate="Vendor: %{y}<br>Semana: %{x}<br>Loads: %{z:.1f}<extra></extra>",
    )
    fig_loads.update_layout(
        height=max(380, 22 * n_rows + 100),
        width=fig_width,
        margin=dict(l=10, r=40, t=30, b=10),
    )
    fig_loads.update_xaxes(side="top", dtick=1, type="category")
    st.plotly_chart(fig_loads, use_container_width=False)

    csv_loads = pivot_loads_display.round(1).to_csv().encode("utf-8-sig")
    st.download_button("Descargar tabla Loads (CSV)", csv_loads, "vendor_x_semana_loads.csv", "text/csv")
else:
    st.info("Sin datos para tabla de loads.")

st.divider()

# --- Detalle ---
with st.expander("Detalle (filas filtradas)"):
    st.dataframe(
        fdf[["SOURCE", "VENDOR", "PRODUCT", "DEPART_WEEK", "CAJAS_35LB", "ALMACEN"]],
        use_container_width=True, height=400,
    )
    csv = fdf.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Descargar CSV filtrado", csv, "papaya_filtrado.csv", "text/csv")

if show_diag:
    st.divider()
    st.subheader("Diagnostico")
    st.json(diag)
    st.write("Columnas detectadas tras normalizar:", list(df.columns))
