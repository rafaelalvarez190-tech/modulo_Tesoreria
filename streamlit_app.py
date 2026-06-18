"""
streamlit_app.py — Módulo de Tesorería Integral (Grupo Supre)
App Streamlit para cargar, validar y gestionar Cuentas por Pagar.
Despliegue: GitHub + https://share.streamlit.io
"""
import os
import datetime as dt
import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="Tesorería Integral · Supre", page_icon="💰", layout="wide")

NAVY = "#1f3864"
BLUE = "#2e75b6"

# --- conexión única (cacheada) -------------------------------------------------
@st.cache_resource
def conexion():
    con = core.get_conn()
    core.init_db(con)
    return con

con = conexion()
USUARIO = "ralvarez@supre.com.co"


def money(v):
    try:
        return "$" + f"{float(v):,.0f}".replace(",", ".")
    except Exception:
        return v


def _detalle_factura(con, fid):
    """Ficha de la factura: saldos, edición, estado, pago, historial."""
    f = core.get_factura(con, fid)
    if not f:
        st.error("Factura no encontrada.")
        return
    st.markdown(f"### Factura {f['numero_factura']} — {f['proveedor']}")
    st.caption(f"NIT {f['identificacion']} · {f['empresa']} · estado actual: **{f['estado']}**")

    c1, c2, c3 = st.columns(3)
    c1.metric("Saldo contable (archivo)", money(f["saldo_contable"]))
    c2.metric("Saldo de tesorería", money(f["saldo_tesoreria"]), "Valor original − abonos")
    c3.metric("Diferencia (conciliación)", money(f["diferencia"]),
              "Revisar" if f["diferencia"] != 0 else "Conciliado",
              delta_color="inverse" if f["diferencia"] != 0 else "off")

    col_a, col_b = st.columns(2)

    # ---- columna izquierda: datos + edición + estado ----
    with col_a:
        st.markdown("**Datos de la factura**")
        st.write({
            "Valor original": money(f["valor_original"]),
            "Total abonado": money(f["total_abonado"]),
            "Cuenta contable": f"{f['codigo_cuenta']} {f['nombre_cuenta']}",
            "Periodo": f["periodo"] or "—",
            "Vencimiento": f"{f['fecha_vencimiento'] or '—'} ({f['cubeta']})",
            "Última actualización": f["fecha_ultima_actualizacion"],
        })

        with st.form("editar"):
            st.markdown("**Editar campos autorizados**")
            fep_val = None
            if f["fecha_estimada_pago"]:
                try:
                    fep_val = dt.date.fromisoformat(f["fecha_estimada_pago"][:10])
                except ValueError:
                    fep_val = None
            fep = st.date_input("Fecha estimada de pago", value=fep_val, format="YYYY-MM-DD")
            notas = st.text_area("Notas", value=f["notas"] or "", height=70)
            if st.form_submit_button("Guardar cambios"):
                ok, m = core.editar_factura(con, fid, fep.isoformat() if fep else "", notas, USUARIO)
                (st.success if ok else st.error)(m)
                st.rerun()

        with st.form("estado"):
            st.markdown("**Cambiar estado**")
            permitidas = core.TRANSICIONES.get(f["estado"], [])
            if permitidas:
                nuevo = st.selectbox("Nuevo estado", permitidas)
                motivo = st.text_input("Motivo (obligatorio para anular)")
                if st.form_submit_button("Aplicar cambio"):
                    ok, m = core.cambiar_estado(con, fid, nuevo, motivo, USUARIO)
                    (st.success if ok else st.error)(m)
                    st.rerun()
            else:
                st.caption("No hay transiciones disponibles desde el estado actual.")
                st.form_submit_button("Aplicar cambio", disabled=True)

    # ---- columna derecha: registrar pago ----
    with col_b:
        st.markdown("**Registrar pago / abono**")
        if f["estado"] == "Anulada":
            st.caption("La factura está anulada; no admite pagos.")
        elif f["saldo_tesoreria"] <= 0:
            st.caption("La factura ya está pagada (saldo 0).")
        else:
            with st.form("pago"):
                cc = st.columns(2)
                fecha_pago = cc[0].date_input("Fecha de pago *", value=dt.date.today(), format="YYYY-MM-DD")
                valor = cc[1].number_input(f"Valor pagado * (máx {f['saldo_tesoreria']:,.0f})",
                                           min_value=0.0, max_value=float(f["saldo_tesoreria"]), step=1000.0)
                banco = cc[0].text_input("Banco *", placeholder="Bancolombia, Davivienda…")
                cuenta = cc[1].text_input("Cuenta bancaria *")
                medio = cc[0].selectbox("Medio de pago *", core.MEDIOS_PAGO)
                comprobante = cc[1].text_input("N° comprobante *")
                notas_p = st.text_input("Notas")
                if st.form_submit_button("Registrar pago", type="primary"):
                    datos = dict(fecha_pago=fecha_pago.isoformat(), valor_pagado=valor, banco=banco,
                                 cuenta_bancaria=cuenta, medio_pago=medio,
                                 numero_comprobante=comprobante, notas=notas_p)
                    ok, m = core.registrar_pago(con, fid, datos, USUARIO)
                    (st.success if ok else st.error)(m)
                    if ok:
                        st.rerun()

    # ---- historial ----
    h1, h2 = st.columns(2)
    with h1:
        st.markdown("**Historial de pagos**")
        pagos = core.pagos_de(con, fid)
        if pagos:
            st.dataframe(pd.DataFrame([{
                "Fecha": p["fecha_pago"], "Medio": p["medio_pago"],
                "Comprobante": p["numero_comprobante"], "Valor": p["valor_pagado"]
            } for p in pagos]), use_container_width=True, hide_index=True,
                column_config={"Valor": st.column_config.NumberColumn(format="$ %d")})
        else:
            st.caption("Sin pagos.")
    with h2:
        st.markdown("**Trazabilidad / auditoría**")
        hist = core.historial_de(con, fid)
        if hist:
            st.dataframe(pd.DataFrame([{
                "Fecha": h["fecha"], "Campo": h["campo"],
                "Antes → Después": f"{h['valor_anterior']} → {h['valor_nuevo']}",
                "Motivo": h["motivo"]
            } for h in hist]), use_container_width=True, hide_index=True)
        else:
            st.caption("Sin cambios registrados.")


# --- encabezado / navegación ---------------------------------------------------
with st.sidebar:
    st.markdown(f"<h1 style='color:{NAVY};margin-bottom:0'>SUPRE</h1>"
                "<div style='color:#666;font-size:13px;margin-top:-6px'>Tesorería Integral · CxP</div>",
                unsafe_allow_html=True)
    st.write("")
    pagina = st.radio("Navegación", ["📊 Dashboard", "📤 Carga masiva", "📄 Facturas", "💳 Pagos"],
                      label_visibility="collapsed")
    st.write("---")
    st.caption(f"Usuario: **{USUARIO}**")
    n = con.execute("SELECT COUNT(*) n FROM factura WHERE activo=1").fetchone()["n"]
    st.caption(f"Facturas en sistema: **{n}**")
    with st.expander("⚙️ Opciones"):
        if st.button("🗑️ Reiniciar base de datos", use_container_width=True):
            core.reset_db(con)
            st.success("Base de datos reiniciada.")
            st.rerun()


# ===============================================================================
# DASHBOARD
# ===============================================================================
if pagina.startswith("📊"):
    st.title("Dashboard de Tesorería")
    st.caption("Cuentas por pagar del grupo Supre · saldo de tesorería en tiempo real")
    d = core.dashboard_data(con)
    k = d["kpis"]

    if k["n_facturas"] == 0:
        st.info("No hay datos todavía. Ve a **📤 Carga masiva** y sube tu archivo de cuentas por pagar "
                "(o el `cuentas_por_pagar_ejemplo.csv` incluido en el repositorio).")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total cuentas por pagar", money(k["total_cxp"]), f"{k['n_facturas']} facturas")
        c2.metric("Total pendiente", money(k["total_pendiente"]))
        c3.metric("Total pagado", money(k["total_pagado"]))
        c4.metric("Total abonado (parcial)", money(k["total_abonado"]))
        c5, c6 = st.columns(2)
        c5.metric("⚠️ Facturas vencidas", k["n_vencidas"], f"{money(k['monto_vencido'])} en mora", delta_color="inverse")
        c6.metric("Por vencer (corriente)", k["n_por_vencer"], money(k["monto_por_vencer"]))

        st.write("")
        g1, g2 = st.columns(2)
        with g1:
            st.subheader("Antigüedad de saldos")
            df = pd.DataFrame({"Rango": list(d["aging"].keys()), "Saldo": list(d["aging"].values())}).set_index("Rango")
            st.bar_chart(df, color=BLUE, height=260)
        with g2:
            st.subheader("Facturas por estado")
            de = pd.DataFrame({"Estado": list(d["estados"].keys()), "Cantidad": list(d["estados"].values())}).set_index("Estado")
            st.bar_chart(de, color="#7fb3e0", height=260)

        g3, g4 = st.columns(2)
        with g3:
            st.subheader("Flujo de pagos diario")
            if d["flujo"]:
                dfl = pd.DataFrame(d["flujo"], columns=["Fecha", "Pagos"]).set_index("Fecha")
                st.line_chart(dfl, color=BLUE, height=260)
            else:
                st.caption("Aún no hay pagos registrados.")
        with g4:
            st.subheader("Saldo por empresa")
            if d["empresas"]:
                dem = pd.DataFrame({"Empresa": list(d["empresas"].keys()), "Saldo": list(d["empresas"].values())}).set_index("Empresa")
                st.bar_chart(dem, color=NAVY, height=260, horizontal=True)


# ===============================================================================
# CARGA MASIVA
# ===============================================================================
elif pagina.startswith("📤"):
    st.title("Carga masiva de cuentas por pagar")
    st.caption("Sube el reporte (Excel .xlsx o CSV). El sistema valida estructura, evita duplicados "
               "y consolida contra el histórico mediante la llave única empresa|NIT|factura.")

    archivo = st.file_uploader("Selecciona tu archivo", type=["csv", "xlsx", "xls"])
    if archivo is not None:
        if st.button("✅ Validar y cargar", type="primary"):
            try:
                resumen, errores = core.procesar_carga(con, archivo.name, archivo.getvalue(), USUARIO)
                msg = (f"Carga procesada: {resumen['leidas']} leídas · {resumen['nuevas']} nuevas · "
                       f"{resumen['actualizadas']} actualizadas · {resumen['sin_cambios']} sin cambios · "
                       f"{resumen['rechazadas']} rechazadas.")
                (st.success if resumen["rechazadas"] == 0 else st.warning)(msg)
                cc = st.columns(5)
                cc[0].metric("Leídas", resumen["leidas"])
                cc[1].metric("Nuevas", resumen["nuevas"])
                cc[2].metric("Actualizadas", resumen["actualizadas"])
                cc[3].metric("Sin cambios", resumen["sin_cambios"])
                cc[4].metric("Rechazadas", resumen["rechazadas"])
                if errores:
                    st.subheader("Log de errores")
                    st.dataframe(pd.DataFrame(errores, columns=["Fila", "Motivo", "Dato"]),
                                 use_container_width=True, hide_index=True)
            except ValueError as e:
                st.error(str(e))

    with st.expander("📋 Estructura esperada del archivo"):
        st.write("**Columnas obligatorias:** " + ", ".join(f"`{c}`" for c in core.REQUIRED_COLS))
        st.write("**Recomendadas (reporte de antigüedad):** " + ", ".join(f"`{c}`" for c in core.EXPECTED_COLS))

    st.subheader("Cargas recientes")
    cargas = core.cargas_recientes(con)
    if cargas:
        df = pd.DataFrame(cargas)[["id", "nombre_archivo", "fecha", "total_leidas",
                                   "nuevas", "actualizadas", "sin_cambios", "rechazadas"]]
        df.columns = ["#", "Archivo", "Fecha", "Leídas", "Nuevas", "Actualizadas", "Sin cambios", "Rechazadas"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("Aún no hay cargas.")


# ===============================================================================
# FACTURAS
# ===============================================================================
elif pagina.startswith("📄"):
    st.title("Facturas (Cuentas por pagar)")
    f1, f2, f3, f4 = st.columns([3, 2, 2, 1])
    q = f1.text_input("Buscar", placeholder="Proveedor, NIT o N° factura")
    empresa = f2.selectbox("Empresa", [""] + core.empresas_distintas(con),
                           format_func=lambda x: "Todas" if x == "" else x)
    estado = f3.selectbox("Estado", [""] + core.ESTADOS,
                          format_func=lambda x: "Todos" if x == "" else x)
    solo_v = f4.checkbox("Vencidas")

    items = core.list_facturas(con, q=q, estado=estado, empresa=empresa, solo_vencidas=solo_v)
    total = sum(i["saldo_tesoreria"] for i in items)
    st.caption(f"{len(items)} facturas · saldo de tesorería total: **{money(total)}**")

    if items:
        df = pd.DataFrame([{
            "ID": i["id"], "Empresa": i["empresa"], "Proveedor": i["proveedor"],
            "N° factura": i["numero_factura"], "Vencimiento": i["fecha_vencimiento"] or "—",
            "Antigüedad": i["cubeta"], "Saldo contable": i["saldo_contable"],
            "Saldo tesorería": i["saldo_tesoreria"], "Estado": i["estado"],
        } for i in items])
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "Saldo contable": st.column_config.NumberColumn(format="$ %d"),
                "Saldo tesorería": st.column_config.NumberColumn(format="$ %d"),
            })

        st.write("---")
        ids = [i["id"] for i in items]
        fid = st.selectbox("🔍 Abrir factura (ID)", ids,
                           format_func=lambda x: f"#{x} — " + next(i["proveedor"] for i in items if i["id"] == x))
        _detalle_factura(con, fid)
    else:
        st.info("No hay facturas con estos filtros.")


# ===============================================================================
# PAGOS
# ===============================================================================
elif pagina.startswith("💳"):
    st.title("Pagos registrados")
    pagos = core.todos_los_pagos(con)
    total = sum((p["valor_pagado"] or 0) for p in pagos)
    st.caption(f"{len(pagos)} pagos · total: **{money(total)}**")
    if pagos:
        df = pd.DataFrame([{
            "Fecha": p["fecha_pago"], "Empresa": p["empresa"], "Proveedor": p["proveedor"],
            "N° factura": p["numero_factura"], "Medio": p["medio_pago"], "Banco": p["banco"],
            "Comprobante": p["numero_comprobante"], "Valor": p["valor_pagado"],
        } for p in pagos])
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={"Valor": st.column_config.NumberColumn(format="$ %d")})
    else:
        st.info("Aún no hay pagos registrados.")
