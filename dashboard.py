"""Dashboard TDS real-time: ESP32 -> USB Serial -> Streamlit + Plotly.

Jalankan:  python -m streamlit run dashboard.py
Format data dari ESP32 (1 baris per pembacaan):  voltage,tds,ec
Baris yang diawali '#' atau formatnya salah akan diabaikan.
Tema warna ada di .streamlit/config.toml (letakkan di folder yang sama).
"""
import collections
import html
import threading
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import serial
import serial.tools.list_ports
import streamlit as st
from plotly.subplots import make_subplots

st.set_page_config(page_title="Kualitas air", page_icon="💧", layout="wide")

MAX_POINTS = 5000     # batas data yang disimpan di memori
STALE_AFTER = 5.0     # detik tanpa data -> status "menunggu data"

# Palet: air jernih -> air keruh, tembaga untuk konduktivitas
INK, PAPER = "#132B2A", "#E8EFEE"
TEAL, BLUE, COPPER = "#0F6E66", "#3B5B92", "#B5651D"
RUST, AMBER = "#A8452A", "#C08A2B"

# Skala rasa air minum (acuan WHO), dari jernih sampai keruh
ZONES = [
    (0, 300, "Sangat baik", "di bawah 300", "#BFE5E3"),
    (300, 600, "Baik", "300 sampai 600", "#8CCDC4"),
    (600, 900, "Cukup", "600 sampai 900", "#CFC888"),
    (900, 1200, "Buruk", "900 sampai 1200", "#B98F55"),
    (1200, 1800, "Tidak layak", "di atas 1200", "#80583A"),
]

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;800&family=Hanken+Grotesk:wght@400;500;700&display=swap');
:root{--ink:#132B2A;--muted:#4A6462;--teal:#0F6E66;--blue:#3B5B92;--copper:#B5651D}
.stApp,.stApp p,.stApp label,.stApp button,.stApp input,.stApp li{font-family:'Hanken Grotesk','Segoe UI',sans-serif}
div.block-container{max-width:1120px;padding-top:2.2rem}
.hd{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;margin-bottom:1.8rem}
.ttl{font:700 2.4rem/1.05 'Big Shoulders Display','Arial Narrow',sans-serif;letter-spacing:.01em;color:var(--ink)}
.sub{color:var(--muted);margin-top:.3rem;font-size:.95rem;font-family:'Hanken Grotesk',sans-serif}
.st{display:flex;align-items:center;gap:.55rem;font:500 .95rem 'Hanken Grotesk',sans-serif;color:var(--ink);padding:.35rem .85rem;border:1.5px solid var(--ink);border-radius:999px;white-space:nowrap}
.dot{width:.65rem;height:.65rem;border-radius:50%}
.err{max-width:34ch;margin-top:.5rem;font:400 .85rem/1.4 'Hanken Grotesk',sans-serif;color:#A8452A;text-align:right}
.grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:2.5rem;align-items:start;font-family:'Hanken Grotesk',sans-serif;color:var(--ink)}
.grid.old{opacity:.5}
.hero{display:flex;align-items:baseline;gap:.9rem;flex-wrap:wrap}
.num{font:800 clamp(5.5rem,13vw,9.5rem)/.85 'Big Shoulders Display','Arial Narrow',sans-serif;color:var(--teal)}
.unit{font-size:1.5rem;font-weight:500}
.verdict{display:flex;align-items:center;gap:.55rem;margin:1rem 0 1.5rem;font-size:1.05rem}
.sw{width:.9rem;height:.9rem;border-radius:2px}
.strip{position:relative;display:grid;grid-template-columns:repeat(5,1fr);gap:3px;padding-top:.5rem}
.pad{height:1.1rem}
.mk{position:absolute;top:0;width:3px;height:1.9rem;background:var(--ink);border-radius:2px;transform:translateX(-50%)}
.labels{display:grid;grid-template-columns:repeat(5,1fr);gap:3px;margin-top:.45rem;font-size:.85rem;line-height:1.3}
.labels b{display:block;font-weight:700}
.labels span{display:block;color:var(--muted)}
.note{margin-top:.9rem;font-size:.85rem;color:var(--muted);max-width:60ch}
.side{display:flex;flex-direction:column;gap:1.7rem;padding-top:.6rem}
.rd{padding-left:1rem;border-left:5px solid var(--c)}
.rd .v{font:700 3.2rem/1 'Big Shoulders Display','Arial Narrow',sans-serif}
.rd .u{font-size:1.1rem;margin-left:.35rem}
.rd .l{font-size:.95rem;color:var(--muted);margin-top:.25rem}
.meta{margin:1.4rem 0 .6rem;font:400 .9rem 'Hanken Grotesk',sans-serif;color:var(--muted)}
@media (max-width:760px){.grid{grid-template-columns:1fr;gap:1.5rem}.hd{flex-direction:column}.err{text-align:left}}
"""
st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)


class SerialReader:
    """Membaca Serial di thread terpisah supaya tidak terganggu rerun Streamlit."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = collections.deque(maxlen=MAX_POINTS)
        self.ser = None
        self.stop_evt = None
        self.thread = None
        self.connected = False
        self.port = ""
        self.error = ""
        self.last_rx = 0.0
        self.bad_lines = 0

    def connect(self, port, baud):
        self.disconnect()
        self.error = ""
        try:
            ser = serial.Serial(port, baud, timeout=1)
        except (serial.SerialException, OSError) as e:
            self.error = (
                f"Port {port} tidak bisa dibuka ({e}). "
                "Tutup Serial Monitor atau program lain yang memakai port ini, lalu coba lagi."
            )
            return False
        self.ser = ser
        self.port = port
        self.last_rx = 0.0
        self.stop_evt = threading.Event()
        self.connected = True
        self.thread = threading.Thread(
            target=self._run, args=(ser, self.stop_evt), daemon=True
        )
        self.thread.start()
        return True

    def disconnect(self):
        if self.stop_evt:
            self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.thread = None
        self.stop_evt = None
        self.ser = None
        self.connected = False

    def clear(self):
        with self.lock:
            self.data.clear()
        self.bad_lines = 0

    def snapshot(self):
        with self.lock:
            return list(self.data)

    def _run(self, ser, stop_evt):
        try:
            while not stop_evt.is_set():
                raw = ser.readline()
                if raw:
                    self._handle(raw)
        except (serial.SerialException, OSError):
            if not stop_evt.is_set():
                self.error = "Koneksi terputus. Periksa kabel USB, lalu klik Sambungkan."
        finally:
            try:
                ser.close()
            except Exception:
                pass
            if self.ser is ser:
                self.connected = False

    def _handle(self, raw):
        line = raw.decode("utf-8", errors="ignore").strip()
        if not line or line.startswith("#"):
            return
        try:
            parts = line.split(",")
            if len(parts) != 3:
                raise ValueError
            voltage, tds, ec = (float(p) for p in parts)
        except ValueError:
            self.bad_lines += 1
            return
        with self.lock:
            self.data.append((datetime.now(), voltage, tds, ec))
        self.last_rx = time.time()


@st.cache_resource
def get_reader():
    return SerialReader()


reader = get_reader()


def list_ports():
    return {
        f"{p.device} — {p.description}": p.device
        for p in serial.tools.list_ports.comports()
    }


def zone_position(tds):
    """Indeks zona dan posisi penanda (0-100%) pada strip berpetak sama lebar."""
    last = len(ZONES) - 1
    for i, (lo, hi, *_rest) in enumerate(ZONES):
        if tds < hi or i == last:
            frac = min(max((tds - lo) / (hi - lo), 0.0), 1.0)
            return i, (i + frac) / len(ZONES) * 100


def make_chart(view):
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05,
        row_heights=[0.5, 0.25, 0.25],
    )
    # Latar grafik TDS memakai warna zona yang sama dengan strip
    for lo, hi, _name, _rng, color in ZONES:
        fig.add_hrect(y0=lo, y1=hi, fillcolor=color, opacity=0.35,
                      line_width=0, layer="below", row=1, col=1)

    series = [
        (1, "TDS", "TDS (ppm)", TEAL, "ppm", ".0f"),
        (2, "Voltage", "Tegangan (V)", BLUE, "V", ".3f"),
        (3, "EC", "CE (µS/cm)", COPPER, "µS/cm", ".0f"),
    ]
    for row, col, title, color, unit, fmt in series:
        fig.add_trace(
            go.Scatter(x=view["Waktu"], y=view[col], mode="lines",
                       line=dict(color=color, width=2.4),
                       hovertemplate=f"%{{y:{fmt}}} {unit}<extra></extra>"),
            row=row, col=1,
        )
        fig.add_trace(
            go.Scatter(x=[view["Waktu"].iloc[-1]], y=[view[col].iloc[-1]],
                       mode="markers", hoverinfo="skip",
                       marker=dict(size=9, color=color,
                                   line=dict(color=PAPER, width=2))),
            row=row, col=1,
        )
        fig.update_yaxes(title_text=title, row=row, col=1)

    fig.update_yaxes(rangemode="tozero", row=1, col=1)
    fig.update_yaxes(rangemode="tozero", row=3, col=1)
    fig.update_yaxes(gridcolor="rgba(19,43,42,0.12)", zeroline=False,
                     title_standoff=8)
    fig.update_xaxes(showgrid=False, tickformat="%H:%M:%S", linecolor=INK)
    fig.update_layout(
        height=640, showlegend=False, hovermode="x",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Hanken Grotesk, Segoe UI, sans-serif", color=INK, size=13),
        margin=dict(l=10, r=20, t=10, b=10), uirevision="keep",
    )
    return fig


# ---------------------------------------------------------------- Sidebar
with st.sidebar:
    ports = list_ports()
    label = st.selectbox(
        "Port",
        list(ports.keys()) or ["(tidak ada port terdeteksi)"],
        disabled=not ports,
    )
    port = ports.get(label)
    baud = st.selectbox("Baud rate", [9600, 57600, 115200, 230400], index=2)
    window = st.slider("Titik di grafik", 20, 1000, 120, step=10)

    c1, c2 = st.columns(2)
    if c1.button(
        "Sambungkan", type="primary", width="stretch",
        disabled=reader.connected or port is None,
    ):
        reader.connect(port, baud)
        st.rerun()
    if c2.button("Putuskan", width="stretch", disabled=not reader.connected):
        reader.disconnect()
        st.rerun()

    st.button("Muat ulang port", width="stretch")
    if st.button("Hapus data", width="stretch"):
        reader.clear()

    rows = reader.snapshot()
    if rows:
        export = pd.DataFrame(
            rows, columns=["waktu", "voltage_V", "tds_ppm", "ec_uScm"]
        )
        st.download_button(
            "Unduh CSV",
            export.to_csv(index=False).encode("utf-8"),
            file_name=f"tds_log_{datetime.now():%Y%m%d_%H%M%S}.csv",
            mime="text/csv",
            width="stretch",
        )


# ------------------------------------------------------------------- Main
@st.fragment(run_every=1.0)
def live_view(window_points):
    rows = reader.snapshot()
    fresh = reader.connected and (time.time() - reader.last_rx < STALE_AFTER)

    if fresh:
        dot, status = TEAL, f"Terhubung ke {reader.port}"
    elif reader.connected:
        dot, status = AMBER, f"Terhubung ke {reader.port}, menunggu data"
    else:
        dot, status = RUST, "Tidak terhubung"
    err = ""
    if reader.error and not reader.connected:
        err = f'<div class="err">{html.escape(reader.error)}</div>'

    st.markdown(
        '<div class="hd"><div><div class="ttl">Kualitas air</div>'
        '<div class="sub">Sensor TDS pada ESP32, terhubung lewat USB</div></div>'
        f'<div><div class="st"><span class="dot" style="background:{dot}"></span>'
        f"{html.escape(status)}</div>{err}</div></div>",
        unsafe_allow_html=True,
    )

    if not rows:
        st.markdown(
            '<p class="note">Belum ada data. Pilih port di sidebar, lalu klik Sambungkan.</p>',
            unsafe_allow_html=True,
        )
        return

    df = pd.DataFrame(rows, columns=["Waktu", "Voltage", "TDS", "EC"])
    last = df.iloc[-1]
    zi, pct = zone_position(last.TDS)
    zname, zcolor = ZONES[zi][2], ZONES[zi][4]

    pads = "".join(f'<div class="pad" style="background:{z[4]}"></div>' for z in ZONES)
    labels = "".join(f"<div><b>{z[2]}</b><span>{z[3]}</span></div>" for z in ZONES)

    st.markdown(
        f'<div class="grid{"" if fresh else " old"}">'
        '<div>'
        f'<div class="hero"><span class="num">{last.TDS:.0f}</span><span class="unit">ppm TDS</span></div>'
        f'<div class="verdict"><span class="sw" style="background:{zcolor}"></span>Rasa air: {zname.lower()}</div>'
        f'<div class="strip">{pads}<div class="mk" style="left:{pct:.1f}%"></div></div>'
        f'<div class="labels">{labels}</div>'
        '<div class="note">Skala rasa air minum menurut acuan WHO. Ini bukan uji keamanan air, '
        'dan akurasinya bergantung pada kalibrasi sensor.</div>'
        '</div>'
        '<div class="side">'
        f'<div class="rd" style="--c:{BLUE}"><div><span class="v">{last.Voltage:.3f}</span><span class="u">V</span></div>'
        '<div class="l">Tegangan sensor</div></div>'
        f'<div class="rd" style="--c:{COPPER}"><div><span class="v">{last.EC:.0f}</span><span class="u">µS/cm</span></div>'
        '<div class="l">Konduktivitas listrik (CE)</div></div>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    skipped = f", {reader.bad_lines} baris non-data diabaikan" if reader.bad_lines else ""
    st.markdown(
        f'<div class="meta">Data terakhir masuk pukul {last.Waktu:%H:%M:%S}, '
        f"total {len(df)} sampel{skipped}.</div>",
        unsafe_allow_html=True,
    )

    st.plotly_chart(
        make_chart(df.tail(window_points)),
        theme=None, width="stretch", config={"displayModeBar": False},
    )


live_view(window)
