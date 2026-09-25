"""
Dashboard interactivo de embarques de PAPAYA
Fuentes: Google Sheets (TX LOADS y TIJ LOADS)
Variables analizadas: VENDOR, DEPART WEEK, CAJAS 35 LB, ALMACEN
"""

import hmac
import io
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Usa el almacen de certificados del sistema (solo Windows/macOS) para evitar
# errores SSL con firewall/antivirus corporativo. En Linux (Streamlit Cloud)
# no aplica y puede fallar - lo silenciamos.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuracion de fuentes
# ---------------------------------------------------------------------------
def _pick_data_dir() -> Path:
    """Directorio de datos. Usa ./data si es escribible; si no (contenedor con
    filesystem de solo lectura) cae a un temporal."""
    candidate = Path(__file__).parent / "data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except Exception:
        import tempfile
        fallback = Path(tempfile.gettempdir()) / "papaya_dashboard_data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _pick_data_dir()
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
            "DEPART_DATE": 7,  # col H  (DEPART DATE FROM ORIGIN - REAL)
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
        err = f"exit {proc.returncode}: {proc.stderr.strip()[:300]}"
    except subprocess.TimeoutExpired:
        err = f"Timeout tras {timeout}s"
    except Exception as exc:
        err = str(exc)

    # Fallback: descarga directa desde este proceso (Streamlit Cloud / Linux)
    try:
        for cfg in SOURCES.values():
            if not cfg.get("local_file"):
                continue
            url = build_csv_url(cfg["spreadsheet_id"], cfg["sheet_name"], cfg.get("gid"))
            text = _fetch_with_retries(url, max_attempts=2)
            (DATA_DIR / cfg["local_file"]).write_text(text, encoding="utf-8")
        return True, "Descarga directa OK"
    except Exception as exc:
        return False, f"subprocess: {err} | directo: {exc}"


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
            # 1) subprocess (necesario en Windows con proxy Netskope)
            ok, msg = refresh_local_data()
            if not ok:
                # 2) descarga directa (funciona en Streamlit Cloud / Linux)
                try:
                    url = build_csv_url(spreadsheet_id, sheet_name, gid)
                    text_direct = _fetch_with_retries(url, max_attempts=2)
                    local_path.write_text(text_direct, encoding="utf-8")
                except Exception as exc:
                    raise RuntimeError(
                        f"No hay {local_file}. Subprocess: {msg} | Directo: {exc}"
                    ) from exc
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
        # Fecha de salida REAL (el encabezado trae saltos de linea, por eso [\s\S])
        "DEPART_DATE": ["depart[\\s\\S]*date[\\s\\S]*real", "fecha[\\s\\S]*salida[\\s\\S]*real"],
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

    # Fecha de salida REAL. El formato de los sheets es 02-Jan-26; lo que no
    # cuadre con ese formato se intenta con el parser generico.
    fecha_txt = df["DEPART_DATE"].astype(str).str.strip()
    fechas = pd.to_datetime(fecha_txt, format="%d-%b-%y", errors="coerce")
    faltantes = fechas.isna() & fecha_txt.ne("") & fecha_txt.ne("NAN") & fecha_txt.ne("nan")
    if faltantes.any():
        fechas.loc[faltantes] = pd.to_datetime(
            fecha_txt[faltantes], errors="coerce", dayfirst=True
        )
    df["DEPART_DATE"] = fechas

    return df, diagnostics


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Dashboard Papaya", page_icon="🍈", layout="wide")


# ---------------------------------------------------------------------------
# Control de acceso (usuario + contrasena)
# Las credenciales viven en los Secrets de Streamlit, NUNCA en el repositorio:
#   [passwords]
#   usuario = "clave"
# Local: archivo .streamlit/secrets.toml (ignorado por git)
# Nube:  Streamlit Cloud > Settings > Secrets
# ---------------------------------------------------------------------------
def _get_users() -> dict:
    try:
        return dict(st.secrets.get("passwords", {}))
    except Exception:
        return {}


def check_password() -> bool:
    """Devuelve True solo si el usuario ya se autentico."""
    if st.session_state.get("auth_ok"):
        return True

    users = _get_users()
    if not users:
        st.error(
            "No hay credenciales configuradas. Agrega una seccion [passwords] "
            "en los Secrets de la app (o en .streamlit/secrets.toml si corres local)."
        )
        return False

    def _validate():
        user = st.session_state.get("login_user", "").strip()
        pwd = st.session_state.get("login_pass", "")
        # El usuario no distingue mayusculas; la contrasena si.
        lookup = {str(k).strip().lower(): v for k, v in users.items()}
        expected = lookup.get(user.lower())
        if expected is not None and hmac.compare_digest(str(pwd), str(expected)):
            st.session_state["auth_ok"] = True
            st.session_state["auth_user"] = user
        else:
            st.session_state["auth_ok"] = False
            st.session_state["auth_failed"] = True
        st.session_state["login_pass"] = ""

    st.title("Dashboard de Embarques de Papaya")
    st.caption("Acceso restringido")
    with st.form("login_form"):
        st.text_input("Usuario", key="login_user")
        st.text_input("Contrasena", type="password", key="login_pass")
        st.form_submit_button("Entrar", on_click=_validate)
    if st.session_state.get("auth_failed"):
        st.error("Usuario o contrasena incorrectos.")
    return False


if not check_password():
    st.stop()

with st.sidebar:
    st.caption(f"Conectado como **{st.session_state.get('auth_user', '')}**")
    if st.button("Cerrar sesion"):
        for k in ("auth_ok", "auth_user", "auth_failed"):
            st.session_state.pop(k, None)
        st.rerun()

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

# Mascara base: todo menos el rango de semanas (el calendario la reutiliza para
# poder mostrar siempre la semana actual aunque el slider este acotado).
mask_base = df["SOURCE"].isin(sources_sel)
if vendors_sel:
    mask_base &= df["VENDOR"].isin(vendors_sel)
if almacenes_sel:
    mask_base &= df["ALMACEN"].isin(almacenes_sel)

mask = mask_base.copy()
if week_range:
    mask &= df["DEPART_WEEK_NUM"].between(week_range[0], week_range[1]) | df["DEPART_WEEK_NUM"].isna()

fdf = df.loc[mask].copy()

LOAD_SIZE = 1150  # 1 camion / 1 load = 1150 cajas 35 LB

# KPIs combinados (todos los destinos juntos)
st.markdown("#### Total general")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Total cajas 35 LB", f"{int(fdf['CAJAS_35LB'].sum()):,}")
k2.metric("Embarques", f"{len(fdf):,}")
k3.metric("Vendors distintos", fdf["VENDOR"].nunique())
k4.metric("Almacenes distintos", fdf["ALMACEN"].nunique())

# --- Comparativo TX vs TIJ ---
st.markdown("#### Comparativo por destino")
DESTINOS = [("TX LOADS", "TX", "tx"), ("TIJ LOADS", "TIJ", "tij")]
destinos_activos = [(s, c, p) for s, c, p in DESTINOS if s in sources_sel]

total_cajas_all = float(fdf["CAJAS_35LB"].sum())
cmp_cols = st.columns(len(destinos_activos) + 1) if destinos_activos else [st]
for i, (src_d, corto, _p) in enumerate(destinos_activos):
    sub = fdf[fdf["SOURCE"] == src_d]
    cajas = int(sub["CAJAS_35LB"].sum())
    share = (cajas / total_cajas_all * 100) if total_cajas_all else 0
    with cmp_cols[i]:
        with st.container(border=True):
            st.markdown(f"**{corto}**")
            st.markdown(f"### {cajas:,}")
            st.caption(f"cajas · {cajas / LOAD_SIZE:,.1f} camiones")
            st.caption(f"{len(sub):,} embarques · {share:.1f}% del total")

if destinos_activos:
    with cmp_cols[-1]:
        comp = (
            fdf.groupby("SOURCE", as_index=False)["CAJAS_35LB"].sum()
            .sort_values("CAJAS_35LB", ascending=False)
        )
        fig_cmp = px.pie(comp, names="SOURCE", values="CAJAS_35LB", hole=0.5)
        fig_cmp.update_layout(height=200, margin=dict(l=0, r=0, t=10, b=10))
        st.plotly_chart(fig_cmp, use_container_width=True, key="pie_destinos")

st.divider()

# ---------------------------------------------------------------------------
# Calendario semanal
# Por defecto muestra la semana en curso, derivada de la fecha del sistema
# (semana ISO), y permite desplegar semanas anteriores. Al recargar la pagina
# siempre vuelve a la semana actual.
# ---------------------------------------------------------------------------
HOY = date.today()
SEMANA_ACTUAL = HOY.isocalendar().week
ANIO_ISO = HOY.isocalendar().year
DIAS_ES = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

st.subheader("Calendario semanal")

semanas_datos = {int(w) for w in df.loc[mask_base, "DEPART_WEEK_NUM"].dropna().unique()}
opciones_sem = sorted(semanas_datos | {SEMANA_ACTUAL}, reverse=True)


def _fmt_semana(w: int) -> str:
    if w == SEMANA_ACTUAL:
        return f"Semana {w} · en curso"
    if w == SEMANA_ACTUAL - 1:
        return f"Semana {w} · anterior"
    return f"Semana {w}"


# index= solo aplica la primera vez; despues manda lo que el usuario eligio en
# session_state. Al recargar la pagina la sesion es nueva y vuelve a la actual.
SEMANA_SEL = st.selectbox(
    "Semana a revisar",
    opciones_sem,
    index=opciones_sem.index(SEMANA_ACTUAL),
    format_func=_fmt_semana,
    key="cal_semana",
)

try:
    LUNES = date.fromisocalendar(ANIO_ISO, SEMANA_SEL, 1)
except ValueError:
    LUNES = HOY - timedelta(days=HOY.weekday())

es_semana_actual = SEMANA_SEL == SEMANA_ACTUAL
_dom = LUNES + timedelta(days=6)
st.caption(
    f"Embarques con DEPART WEEK = {SEMANA_SEL} "
    f"({LUNES.day}-{MESES_ES[LUNES.month - 1]} al {_dom.day}-{MESES_ES[_dom.month - 1]}), "
    "colocados por su fecha de salida REAL. "
    + (
        f"Hoy es {DIAS_ES[HOY.weekday()]} {HOY.day}-{MESES_ES[HOY.month - 1]}-{HOY.year}."
        if es_semana_actual
        else "Estas viendo una semana pasada."
    )
)

sem = df.loc[mask_base & (df["DEPART_WEEK_NUM"] == SEMANA_SEL)].copy()

if sem.empty:
    st.info(f"No hay embarques registrados en la semana {SEMANA_SEL} con los filtros actuales.")
else:
    con_fecha = sem.dropna(subset=["DEPART_DATE"]).copy()
    con_fecha["DIA"] = con_fecha["DEPART_DATE"].dt.date
    sin_fecha = len(sem) - len(con_fecha)

    # Filas cuya fecha real cae fuera del lunes-domingo de esta semana
    # (errores de captura en la columna DEPART WEEK). Se reportan aparte para
    # que los totales cuadren siempre con la suma de los 7 dias.
    DOMINGO = LUNES + timedelta(days=6)
    en_ventana = con_fecha["DIA"].between(LUNES, DOMINGO)
    fuera_rango = con_fecha.loc[~en_ventana].copy()
    con_fecha = con_fecha.loc[en_ventana].copy()

    cols = st.columns(7)
    for i, col in enumerate(cols):
        dia = LUNES + timedelta(days=i)
        dia_df = con_fecha[con_fecha["DIA"] == dia]
        cajas = int(dia_df["CAJAS_35LB"].sum())
        camiones = cajas / LOAD_SIZE
        es_hoy = dia == HOY
        futuro = dia > HOY
        with col:
            with st.container(border=True):
                etiqueta = f"{DIAS_ES[i]} {dia.day}-{MESES_ES[dia.month - 1]}"
                st.markdown(f"**{'🟢 ' if es_hoy else ''}{etiqueta}**")
                if len(dia_df):
                    st.markdown(f"### {cajas:,}")
                    st.caption(f"cajas · {camiones:.1f} camiones")
                    st.caption(f"{len(dia_df)} embarques")
                    partes = []
                    for _s, _c, _p in DESTINOS:
                        v = int(dia_df.loc[dia_df["SOURCE"] == _s, "CAJAS_35LB"].sum())
                        if v:
                            partes.append(f"{_c} {v:,}")
                    if partes:
                        st.caption(" · ".join(partes))
                elif futuro:
                    st.markdown("### —")
                    st.caption("por salir")
                else:
                    st.markdown("### 0")
                    st.caption("sin embarques")

    tot_cajas = int(con_fecha["CAJAS_35LB"].sum())
    r1, r2, r3, r4 = st.columns(4)
    r1.metric(f"Total semana {SEMANA_SEL}", f"{tot_cajas:,} cajas")
    r2.metric("Camiones", f"{tot_cajas / LOAD_SIZE:.1f}")
    r3.metric("Embarques", f"{len(con_fecha):,}")
    r4.metric("Vendors", con_fecha["VENDOR"].nunique())

    if sin_fecha:
        st.warning(
            f"{sin_fecha} embarque(s) de la semana {SEMANA_SEL} no tienen fecha de salida REAL "
            "capturada, por eso no aparecen en ningun dia del calendario."
        )

    if len(fuera_rango):
        with st.expander(
            f"⚠️ {len(fuera_rango)} embarque(s) marcados semana {SEMANA_SEL} "
            f"con fecha fuera del {LUNES.day}-{MESES_ES[LUNES.month - 1]} al "
            f"{DOMINGO.day}-{MESES_ES[DOMINGO.month - 1]}"
        ):
            st.caption(
                "La columna DEPART WEEK del sheet dice semana "
                f"{SEMANA_SEL}, pero la fecha de salida REAL cae en otra semana. "
                "Suele ser un error de captura. No estan incluidos en los totales de arriba."
            )
            fr = fuera_rango.sort_values("DEPART_DATE")[
                ["SOURCE", "VENDOR", "DEPART_DATE", "CAJAS_35LB", "ALMACEN"]
            ].copy()
            fr["DEPART_DATE"] = fr["DEPART_DATE"].dt.strftime("%d-%b-%Y")
            fr.columns = ["Origen", "Vendor", "Fecha salida REAL", "Cajas 35 LB", "Almacen"]
            st.dataframe(
                fr.style.format({"Cajas 35 LB": "{:,.0f}"}),
                use_container_width=True, hide_index=True,
            )

    with st.expander(f"Detalle de la semana {SEMANA_SEL} ({len(con_fecha)} embarques)", expanded=True):
        det = con_fecha.sort_values(["DEPART_DATE", "VENDOR"]).copy()
        det["Dia"] = det["DEPART_DATE"].apply(lambda d: f"{DIAS_ES[d.weekday()]} {d.day}-{MESES_ES[d.month - 1]}")
        det["Camiones"] = det["CAJAS_35LB"] / LOAD_SIZE
        det = det[["Dia", "SOURCE", "VENDOR", "PRODUCT", "CAJAS_35LB", "Camiones", "ALMACEN"]]
        det.columns = ["Dia", "Origen", "Vendor", "Producto", "Cajas 35 LB", "Camiones", "Almacen"]
        st.dataframe(
            det.style.format({"Cajas 35 LB": "{:,.0f}", "Camiones": "{:,.1f}"}),
            use_container_width=True, height=380, hide_index=True,
        )
        st.download_button(
            "Descargar detalle de la semana (CSV)",
            det.to_csv(index=False).encode("utf-8-sig"),
            f"semana_{SEMANA_SEL}.csv", "text/csv",
        )

st.divider()

# ---------------------------------------------------------------------------
# Bloque de analisis por destino (TX / TIJ), uno debajo del otro.
# Cada seccion repite el mismo juego de vistas pero solo con sus datos.
# ---------------------------------------------------------------------------
def render_destino(dd: pd.DataFrame, titulo: str, pfx: str) -> None:
    st.header(f"Destino: {titulo}")

    if dd.empty:
        st.info(f"No hay embarques de {titulo} con los filtros actuales.")
        return

    cajas_tot = int(dd["CAJAS_35LB"].sum())
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Cajas 35 LB", f"{cajas_tot:,}")
    d2.metric("Camiones", f"{cajas_tot / LOAD_SIZE:,.1f}")
    d3.metric("Embarques", f"{len(dd):,}")
    d4.metric("Vendors", dd["VENDOR"].nunique())

    # --- Cajas por semana ---
    st.subheader("Cajas 35 LB por semana de salida")
    by_week = (
        dd.dropna(subset=["DEPART_WEEK_NUM"])
        .groupby("DEPART_WEEK_NUM", as_index=False)["CAJAS_35LB"].sum()
        .sort_values("DEPART_WEEK_NUM")
    )
    if not by_week.empty:
        fig = px.bar(
            by_week, x="DEPART_WEEK_NUM", y="CAJAS_35LB",
            labels={"DEPART_WEEK_NUM": "Semana", "CAJAS_35LB": "Cajas 35 LB"},
        )
        fig.update_layout(height=380, xaxis=dict(dtick=1))
        st.plotly_chart(fig, use_container_width=True, key=f"bar_sem_{pfx}")
    else:
        st.info("No hay datos de semana de salida para el filtro actual.")

    # --- TIME LINE: camiones por semana ---
    st.subheader(f"Time Line · Camiones por semana  ·  1 camion = {LOAD_SIZE} cajas 35 LB")
    trucks_by_week = (
        dd.dropna(subset=["DEPART_WEEK_NUM"])
        .groupby("DEPART_WEEK_NUM", as_index=False)["CAJAS_35LB"].sum()
    )
    if not trucks_by_week.empty:
        trucks_by_week["CAMIONES"] = trucks_by_week["CAJAS_35LB"] / LOAD_SIZE
        tl = (
            trucks_by_week[trucks_by_week["CAMIONES"] > 0][["DEPART_WEEK_NUM", "CAMIONES"]]
            .sort_values("DEPART_WEEK_NUM").reset_index(drop=True)
        )
    else:
        tl = pd.DataFrame()

    if not tl.empty:
        fig_tl = px.line(
            tl, x="DEPART_WEEK_NUM", y="CAMIONES", markers=True,
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
        st.plotly_chart(fig_tl, use_container_width=True, key=f"tl_{pfx}")

        max_row = tl.loc[tl["CAMIONES"].idxmax()]
        kc1, kc2, kc3 = st.columns(3)
        kc1.metric("Total camiones", f"{int(round(tl['CAMIONES'].sum())):,}")
        kc2.metric("Semana pico", f"S{int(max_row['DEPART_WEEK_NUM'])} · {int(round(max_row['CAMIONES']))}")
        kc3.metric("Promedio semanas activas", f"{int(round(tl['CAMIONES'].mean())):,}")
    else:
        st.info("Sin datos para el time line.")

    # --- Vendors y almacenes ---
    left, right = st.columns(2)
    with left:
        st.subheader("Top Vendors por cajas 35 LB")
        by_vendor = (
            dd.groupby("VENDOR", as_index=False)["CAJAS_35LB"].sum()
            .sort_values("CAJAS_35LB", ascending=False).head(15)
        )
        if not by_vendor.empty:
            fig2 = px.bar(
                by_vendor, x="CAJAS_35LB", y="VENDOR", orientation="h",
                labels={"CAJAS_35LB": "Cajas 35 LB", "VENDOR": ""},
            )
            fig2.update_layout(height=480, yaxis=dict(categoryorder="total ascending"))
            st.plotly_chart(fig2, use_container_width=True, key=f"vend_{pfx}")
        else:
            st.info("Sin datos.")

    with right:
        st.subheader("Distribucion por Almacen")
        by_alm = dd.groupby("ALMACEN", as_index=False)["CAJAS_35LB"].sum()
        by_alm = by_alm[by_alm["ALMACEN"].astype(bool) & by_alm["ALMACEN"].ne("NAN")]
        if not by_alm.empty:
            fig3 = px.pie(by_alm, names="ALMACEN", values="CAJAS_35LB", hole=0.45)
            fig3.update_layout(height=480)
            st.plotly_chart(fig3, use_container_width=True, key=f"alm_{pfx}")
        else:
            st.info("Sin datos.")

    # --- Vendor x Semana en LOADS ---
    st.subheader(f"Vendor x Semana (Loads)  ·  1 load = {LOAD_SIZE} cajas 35 LB")
    loads_df = dd.dropna(subset=["DEPART_WEEK_NUM"]).copy()
    loads_df["LOADS"] = loads_df["CAJAS_35LB"] / LOAD_SIZE
    pivot_loads = loads_df.pivot_table(
        index="VENDOR", columns="DEPART_WEEK_NUM",
        values="LOADS", aggfunc="sum", fill_value=0,
    )
    if not pivot_loads.empty:
        pivot_loads.columns = [int(c) if float(c).is_integer() else c for c in pivot_loads.columns]
        # Semanas en orden DESCENDENTE: la mas reciente queda como primera columna.
        pivot_loads = pivot_loads.reindex(sorted(pivot_loads.columns, reverse=True), axis=1)
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
            use_container_width=True, height=440, key=f"piv_{pfx}",
        )

        cols_extra = [c for c in ["TOTAL", "PROMEDIO/SEM"] if c in pivot_loads.columns]
        heat_data = pivot_loads.drop(columns=cols_extra)
        n_cols = len(heat_data.columns)
        n_rows = len(heat_data.index)
        fig_loads = px.imshow(
            heat_data.values,
            x=[str(c) for c in heat_data.columns],
            y=heat_data.index.tolist(),
            labels=dict(x="Semana de salida", y="Vendor", color="Loads"),
            color_continuous_scale="YlOrRd",
            aspect="auto",
        )
        fig_loads.update_traces(
            texttemplate="%{z:.1f}",
            textfont=dict(size=11),
            hovertemplate="Vendor: %{y}<br>Semana: %{x}<br>Loads: %{z:.1f}<extra></extra>",
        )
        fig_loads.update_layout(
            height=max(380, 22 * n_rows + 100),
            width=max(900, 55 * n_cols + 200),
            margin=dict(l=10, r=40, t=30, b=10),
        )
        fig_loads.update_xaxes(side="top", dtick=1, type="category")
        st.plotly_chart(fig_loads, use_container_width=False, key=f"heat_{pfx}")

        st.download_button(
            "Descargar tabla Loads (CSV)",
            pivot_loads_display.round(1).to_csv().encode("utf-8-sig"),
            f"vendor_x_semana_loads_{pfx}.csv", "text/csv", key=f"dl_loads_{pfx}",
        )
    else:
        st.info("Sin datos para tabla de loads.")

    with st.expander(f"Detalle de {titulo} ({len(dd):,} embarques)"):
        st.dataframe(
            dd[["VENDOR", "PRODUCT", "DEPART_WEEK", "CAJAS_35LB", "ALMACEN"]],
            use_container_width=True, height=400, key=f"det_{pfx}",
        )
        st.download_button(
            "Descargar CSV", dd.to_csv(index=False).encode("utf-8-sig"),
            f"papaya_{pfx}.csv", "text/csv", key=f"dl_det_{pfx}",
        )


for _src, _corto, _pfx in destinos_activos:
    render_destino(fdf[fdf["SOURCE"] == _src].copy(), _src, _pfx)
    st.divider()

# --- Exportacion global ---
with st.expander(f"Detalle completo, todos los destinos ({len(fdf):,} embarques)"):
    st.dataframe(
        fdf[["SOURCE", "VENDOR", "PRODUCT", "DEPART_WEEK", "CAJAS_35LB", "ALMACEN"]],
        use_container_width=True, height=400, key="det_global",
    )
    st.download_button(
        "Descargar CSV filtrado", fdf.to_csv(index=False).encode("utf-8-sig"),
        "papaya_filtrado.csv", "text/csv", key="dl_global",
    )


if show_diag:
    st.divider()
    st.subheader("Diagnostico")
    st.json(diag)
    st.write("Columnas detectadas tras normalizar:", list(df.columns))
