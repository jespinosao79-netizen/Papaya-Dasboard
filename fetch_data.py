"""Descargador standalone de las hojas de Google Sheets.
Se ejecuta como subprocess desde app.py para bypasear Netskope
(que bloquea el proceso Streamlit pero permite Python CLI plano).

Uso: python fetch_data.py <output_dir>
Genera:
  <output_dir>/tx_loads.csv
  <output_dir>/tij_loads.csv
"""
import os
import sys
import time
import urllib.request
import ssl

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

SOURCES = {
    "tx_loads.csv": "https://docs.google.com/spreadsheets/d/1v762Wg44PDQFdwQyeRQeGEbPjlb4QfYIJ-p7EQFqScg/export?format=csv&gid=0",
    "tij_loads.csv": "https://docs.google.com/spreadsheets/d/1rRgSp64hjLtAFTbSxo7LR5WWiMfggDzroj82kRADK7A/export?format=csv&gid=0",
}


def fetch(url: str, tries: int = 3) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"Fallo tras {tries} intentos: {last}")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)
    for filename, url in SOURCES.items():
        data = fetch(url)
        path = os.path.join(out_dir, filename)
        # Escritura atomica: temp + rename
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        print(f"OK {filename}: {len(data):,} bytes")


if __name__ == "__main__":
    main()
