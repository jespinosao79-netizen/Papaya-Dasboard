# Dashboard Papaya

Dashboard interactivo de embarques de papaya. Lee dos hojas de Google Sheets en tiempo real:
- **TX LOADS** (spreadsheet publico)
- **TIJ LOADS** (spreadsheet publico)

Filtra automaticamente el producto PAPAYA y consolida alias de proveedores.

## Variables analizadas
- VENDOR
- DEPART WEEK
- CAJAS 35 LB (columna U en TX LOADS)
- ALMACEN

## Correr localmente
```bash
pip install -r requirements.txt
python fetch_data.py data
streamlit run app.py
```

## Deploy en Streamlit Community Cloud
1. Fork/clone este repo a GitHub
2. En https://share.streamlit.io conecta el repo
3. Archivo principal: `app.py`
4. Deploy — no requiere configuracion adicional
