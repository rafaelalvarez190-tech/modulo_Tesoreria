# Módulo de Tesorería Integral — Grupo Supre (Streamlit)

App **Streamlit** para cargar, validar y gestionar las cuentas por pagar (CxP) a partir del
reporte diario en Excel/CSV. Pensada para desplegarse gratis en **Streamlit Community Cloud**.

## Funcionalidades

- **Carga masiva** `.xlsx`/`.csv` con validación de estructura y **log de errores**.
- **Deduplicación (upsert)** por llave única `empresa | NIT | factura`: no duplica, incorpora
  nuevas y actualiza solo lo permitido.
- **Saldo Contable** (del archivo) vs **Saldo de Tesorería** (valor − abonos) + conciliación.
- **Gestión de facturas**: estados con máquina de transiciones, edición y trazabilidad.
- **Pagos/abonos** con validación de sobre-pago y recálculo automático de estado/saldo.
- **Dashboard** con KPIs y gráficos (antigüedad, estados, flujo de pagos, saldo por empresa).

---

## Opción A — Desplegar en Streamlit Cloud (recomendado, sin instalar nada)

### 1. Sube el proyecto a GitHub
- Crea una cuenta en https://github.com si no la tienes.
- Crea un repositorio nuevo, por ejemplo `tesoreria-supre` (puede ser privado).
- Sube **todos** estos archivos manteniendo la estructura:

```
tesoreria-supre/
├── streamlit_app.py
├── core.py
├── requirements.txt
├── cuentas_por_pagar_ejemplo.csv
├── .gitignore
└── .streamlit/
    └── config.toml
```

> La forma más simple sin usar la terminal: en tu repo de GitHub haz clic en
> **Add file → Upload files**, arrastra los archivos y la carpeta `.streamlit`, y confirma
> con **Commit changes**.

### 2. Conecta Streamlit Cloud
- Entra a https://share.streamlit.io e inicia sesión con tu cuenta de GitHub.
- Clic en **Create app → Deploy a public app from GitHub**.
- Selecciona tu repositorio, la rama `main` y, en **Main file path**, escribe:
  `streamlit_app.py`
- Clic en **Deploy**. En 1–2 minutos tendrás una URL pública tipo
  `https://tesoreria-supre.streamlit.app` para abrir desde cualquier navegador.

### 3. Prueba el flujo
1. **📤 Carga masiva** → sube `cuentas_por_pagar_ejemplo.csv` → revisa el resumen.
2. **📊 Dashboard** → KPIs y gráficos.
3. **📄 Facturas** → abre una, registra un abono parcial, observa el cambio de estado/saldo.
4. Sube el mismo archivo otra vez → verás `0 nuevas / 28 sin cambios` (no duplica).

---

## Opción B — Ejecutar en tu computador

Requisitos: Python 3.10+

```bash
cd tesoreria_streamlit
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Se abre solo en `http://localhost:8501`.

---

## ⚠️ Importante sobre la persistencia de datos

Streamlit Community Cloud usa un **disco efímero**: la base `tesoreria.db` se guarda mientras
el contenedor está activo, pero **se reinicia** cuando la app "duerme" por inactividad o cuando
actualizas el código. Es perfecto para **demostrar y probar**, no para datos definitivos.

Para producción con datos persistentes y multiusuario, conecta una base externa (no requiere
cambiar la UI, solo `core.py`):

- **Supabase / Neon / Postgres** (gratis para empezar) — recomendado.
- **Google Sheets** vía `st.connection` para algo muy ligero.
- **Streamlit secrets** (`.streamlit/secrets.toml`) para guardar las credenciales de forma segura.

> Siguiente paso sugerido: añadir login con roles y segmentación por empresa, y migrar el
> almacenamiento a Postgres. La lógica de negocio (RN-01 a RN-11) ya está aislada en `core.py`,
> así que el cambio se concentra en las funciones de conexión y consulta.

## Estructura del código

- **`core.py`** — toda la lógica (sin Streamlit): base de datos, validación, upsert,
  pagos, estados, datos del dashboard. Fácil de testear y de migrar a otra base.
- **`streamlit_app.py`** — interfaz de usuario (navegación, formularios, tablas, gráficos).
