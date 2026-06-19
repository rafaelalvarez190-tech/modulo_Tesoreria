"""
streamlit_app.py - Modulo de Tesoreria Integral (Grupo Supre)
App Streamlit para cargar, validar y gestionar Cuentas por Pagar.
Despliegue: GitHub + https://share.streamlit.io
"""
import datetime as dt
import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="Tesoreria Integral - Supre", page_icon=":moneybag:", layout="wide")

NAVY = "#1f3864"
BLUE = "#2e75b6"


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
    """Ficha de la factura: saldos, edicion, estado, pago, historial."""
    f = core.get_factura(con, fid)
    if not f:
        st.error("Factura no encontrada.")
        return
    st.markdown(f"### Factura {f['numero_factura']} - {f['proveedor']}")
    st.caption(f"NIT {f['identificacion']} - {f['empresa']} - estado actual: **{f['estado']}**")

    c1, c2, c3 = st.columns(3)
    c1.metric("Saldo contable (archivo)", money(f["saldo_contable"]))
    c2.metric("Saldo de tesoreria", money(f["saldo_tesoreria"]), "Valor original - abonos")
    c3.metric("Diferencia (conciliacion)", money(f["diferencia"]),
              "Revisar" if f["diferencia"] != 0 else "Conciliado",
              delta_color="inverse" if f["diferencia"] != 0 else "off")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Datos de la factura**")
        st.write({
            "Valor original": money(f["valor_original"]),
            "Total abonado": money(f["total_abonado"]),
            "Cuenta contable": f"{f['codigo_cuenta']} {f['nombre_cuenta']}",
            "Periodo": f["periodo"] or "-",
            "Vencimiento": f"{f['fecha_vencimiento'] or '-'} ({f['cubeta']})",
            "Ultima actualizacion": f["fecha_ultima_actualizacion"],
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

    with col_b:
        st.markdown("**Registrar pago / abono**")
        if f["estado"] == "Anulada":
            st.caption("La factura esta anulada; no admite pagos.")
        elif f["saldo_tesoreria"] <= 0:
            st.caption("La factura ya esta pagada (saldo 0).")
        else:
            with st.form("pago", clear_on_submit=True):
                cc = st.columns(2)
                fecha_pago = cc[0].date_input("Fecha de pago *", value=dt.date.today(), format="YYYY-MM-DD")
                valor = cc[1].number_input(
                    f"Valor pagado * (saldo {money(f['saldo_tesoreria'])}; el excedente va a anticipo)",
                    min_value=0.0, step=1000.0)
                banco = cc[0].text_input("Banco *", placeholder="Bancolombia, Davivienda...")
                cuenta = cc[1].text_input("Cuenta bancaria *")
                medio = cc[0].selectbox("Medio de pago *", core.MEDIOS_PAGO)
                comprobante = cc[1].text_input("N comprobante *")
                notas_p = st.text_input("Notas")
                if st.form_submit_button("Registrar pago", type="primary"):
                    datos = dict(fecha_pago=fecha_pago.isoformat(), valor_pagado=valor, banco=banco,
                                 cuenta_bancaria=cuenta, medio_pago=medio,
                                 numero_comprobante=comprobante, notas=notas_p)
                    ok, m = core.registrar_pago(con, fid, datos, USUARIO)
                    (st.success if ok else st.error)(m)
                    if ok:
                        st.rerun()

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
        st.markdown("**Trazabilidad / auditoria**")
        hist = core.historial_de(con, fid)
        if hist:
            st.dataframe(pd.DataFrame([{
                "Fecha": h["fecha"], "Campo": h["campo"],
                "Antes -> Despues": f"{h['valor_anterior']} -> {h['valor_nuevo']}",
                "Motivo": h["motivo"]
            } for h in hist]), use_container_width=True, hide_index=True)
        else:
            st.caption("Sin cambios registrados.")


def _abono_por_proveedor(con):
    """Abono a nivel de proveedor: se reparte entre sus facturas mas vencidas primero."""
    # limpiar campos de monto tras un registro exitoso
    if st.session_state.pop("abp_clear", False):
        st.session_state["abp_monto"] = ""
        st.session_state["cruce_monto"] = ""
    st.markdown("Aplica un abono a un **proveedor**; el sistema lo distribuye automaticamente "
                "entre sus facturas **de la mas vencida a la menos vencida**, sin exceder el saldo total.")
    empresa = st.selectbox("Empresa (opcional)", [""] + core.empresas_distintas(con),
                           format_func=lambda x: "Todas" if x == "" else x, key="abp_emp")
    provs = core.proveedores_con_saldo(con, empresa)
    if not provs:
        st.info("No hay proveedores con saldo pendiente.")
        return
    opt = st.selectbox(
        "Proveedor", provs,
        format_func=lambda p: f"{p['proveedor']} - NIT {p['nit']} - saldo {money(p['saldo'])} - {p['n']} fact.",
        key="abp_prov")
    nit, saldo_total = opt["nit"], opt["saldo"]

    vencido_total = opt.get("vencido", 0.0)
    anticipo_total = core.saldo_anticipo_proveedor(con, nit)
    c = st.columns(4)
    c[0].metric("Saldo total del proveedor", money(saldo_total))
    c[1].metric("Saldo total vencido", money(vencido_total),
                f"{(vencido_total / saldo_total * 100):.0f}% del saldo" if saldo_total else None,
                delta_color="inverse")
    c[2].metric("Saldo anticipo a proveedor", money(anticipo_total))
    c[3].metric("Facturas con saldo", opt["n"])
    monto_str = st.text_input("Monto a abonar (si excede el saldo total, el excedente va a anticipo)",
                              placeholder="Ej: 1.000.000", key="abp_monto")
    monto = core.to_float(monto_str)
    if monto_str.strip():
        st.caption("Monto a aplicar: " + money(monto))

    facs = core.facturas_pagables_proveedor(con, nit, empresa)
    plan, aplicado, remanente, _ = core.distribuir_abono(facs, monto)
    abono_by = {f["llave_unica"]: ap for f, ap in plan}
    st.markdown("**Facturas del proveedor y distribucion del abono** (mas vencidas primero)")
    if facs:
        prev = pd.DataFrame([{
            "N factura": f["numero_factura"], "Vencimiento": f["fecha_vencimiento"] or "-",
            "Antiguedad": f["cubeta"], "Saldo": money(f["saldo_tesoreria"]),
            "Saldo vencido": money(f["saldo_tesoreria"] if f["vencida"] else 0),
            "Abono": money(abono_by.get(f["llave_unica"], 0)),
            "Saldo restante": money(round(f["saldo_tesoreria"] - abono_by.get(f["llave_unica"], 0), 2)),
        } for f in facs])
        st.dataframe(prev, use_container_width=True, hide_index=True)
        if monto > 0:
            cap = f"Se aplicaran **{money(aplicado)}** a {len(plan)} factura(s)."
            if remanente > 0:
                cap += f" Excedente {money(remanente)} -> se registrara como anticipo del proveedor."
            st.caption(cap)
        else:
            st.caption("Ingresa un monto mayor a cero para ver la distribucion del abono.")
    else:
        st.caption("El proveedor no tiene facturas con saldo pendiente.")

    with st.form("abono_prov", clear_on_submit=True):
        st.markdown("**Datos del pago**")
        cc = st.columns(2)
        fecha_pago = cc[0].date_input("Fecha de pago *", value=dt.date.today(), format="YYYY-MM-DD")
        banco = cc[1].text_input("Banco *", placeholder="Bancolombia, Davivienda...")
        cuenta = cc[0].text_input("Cuenta bancaria *")
        medio = cc[1].selectbox("Medio de pago *", core.MEDIOS_PAGO)
        comprobante = cc[0].text_input("N comprobante *")
        notas = cc[1].text_input("Notas")
        if st.form_submit_button("Registrar abono por proveedor", type="primary"):
            datos = dict(fecha_pago=fecha_pago.isoformat(), banco=banco, cuenta_bancaria=cuenta,
                         medio_pago=medio, numero_comprobante=comprobante, notas=notas)
            ok, m, res = core.abono_por_proveedor(con, nit, monto, datos, empresa, USUARIO)
            (st.success if ok else st.error)(m)
            if ok:
                st.session_state["abp_clear"] = True
                st.rerun()

    # ---- Cruce de anticipo ----
    st.write("---")
    st.markdown("**Cruce de anticipo**")
    st.caption("Aplica el saldo de anticipo disponible del proveedor contra sus facturas "
               "(de la mas vencida a la menos vencida), sin movimiento bancario.")
    if anticipo_total <= 0:
        st.caption("Este proveedor no tiene saldo de anticipo disponible para cruzar.")
    elif not facs:
        st.caption("No hay facturas con saldo para cruzar.")
    else:
        st.info(f"Anticipo disponible: {money(anticipo_total)}")
        cruce_str = st.text_input("Monto a cruzar (vacio = todo el anticipo disponible)",
                                  placeholder="Ej: 500.000", key="cruce_monto")
        cruce_monto = core.to_float(cruce_str) if cruce_str.strip() else anticipo_total
        excede = cruce_monto > anticipo_total + 0.001
        if cruce_str.strip():
            st.caption("Monto a cruzar: " + money(cruce_monto))
        if excede:
            st.error(f"El monto a cruzar ({money(cruce_monto)}) no puede ser mayor al anticipo "
                     f"disponible ({money(anticipo_total)}). Debe ser menor o igual.")
        tope = round(min(cruce_monto, anticipo_total, saldo_total), 2)
        plan_c, aplic_c, _, _ = core.distribuir_abono(facs, tope)
        cruce_by = {f["llave_unica"]: ap for f, ap in plan_c}
        prevc = pd.DataFrame([{
            "N factura": f["numero_factura"], "Vencimiento": f["fecha_vencimiento"] or "-",
            "Antiguedad": f["cubeta"], "Saldo": money(f["saldo_tesoreria"]),
            "Cruce": money(cruce_by.get(f["llave_unica"], 0)),
            "Saldo restante": money(round(f["saldo_tesoreria"] - cruce_by.get(f["llave_unica"], 0), 2)),
        } for f in facs])
        st.dataframe(prevc, use_container_width=True, hide_index=True)
        st.caption(f"Se cruzaran **{money(aplic_c)}** del anticipo; quedaran "
                   f"{money(round(anticipo_total - aplic_c, 2))} de anticipo.")
        with st.form("form_cruce"):
            if st.form_submit_button("Aplicar cruce de anticipo", type="primary", disabled=excede):
                ok, m, res = core.cruzar_anticipo(con, nit, cruce_monto, empresa, USUARIO)
                (st.success if ok else st.error)(m)
                if ok:
                    st.session_state["abp_clear"] = True
                    st.rerun()


def _estado_proveedor(con):
    """Estado actual por proveedor: cartera total y total vencido."""
    st.markdown("Estado actual de cada proveedor: **saldo total de cartera** y **total vencido**. "
                "Un proveedor esta *En mora* si tiene saldo vencido, o *Al dia* si no.")
    empresa = st.selectbox("Empresa (opcional)", [""] + core.empresas_distintas(con),
                           format_func=lambda x: "Todas" if x == "" else x, key="ep_emp")
    provs = core.proveedores_con_saldo(con, empresa)
    if not provs:
        st.info("No hay proveedores con saldo pendiente.")
        return
    tot_cartera = round(sum(p["saldo"] for p in provs), 2)
    tot_vencido = round(sum(p.get("vencido", 0) for p in provs), 2)
    en_mora = sum(1 for p in provs if p.get("vencido", 0) > 0)
    c = st.columns(4)
    c[0].metric("Proveedores con cartera", len(provs))
    c[1].metric("Cartera total", money(tot_cartera))
    c[2].metric("Total vencido", money(tot_vencido),
                f"{(tot_vencido / tot_cartera * 100):.0f}% de la cartera" if tot_cartera else None,
                delta_color="inverse")
    c[3].metric("Proveedores en mora", en_mora)

    df = pd.DataFrame([{
        "Proveedor": p["proveedor"], "NIT": p["nit"], "Facturas": p["n"],
        "Cartera total": money(p["saldo"]), "Vencido": money(p.get("vencido", 0)),
        "% vencido": round(p.get("vencido", 0) / p["saldo"] * 100, 1) if p["saldo"] else 0.0,
        "Estado": "En mora" if p.get("vencido", 0) > 0 else "Al dia",
    } for p in provs])
    st.dataframe(df, use_container_width=True, hide_index=True, column_config={
        "% vencido": st.column_config.NumberColumn(format="%.1f%%"),
    })


def _anticipos(con):
    """Saldo de anticipo (saldo a favor) por proveedor."""
    st.markdown("Saldos **a favor** (anticipos) generados cuando un pago o abono **excede** lo adeudado "
                "al proveedor. El excedente queda aqui como saldo del proveedor.")
    empresa = st.selectbox("Empresa (opcional)", [""] + core.empresas_distintas(con),
                           format_func=lambda x: "Todas" if x == "" else x, key="ant_emp")
    resumen = core.anticipos_resumen(con, empresa)
    total = round(sum(a["saldo"] for a in resumen), 2)
    c = st.columns(2)
    c[0].metric("Proveedores con anticipo", len(resumen))
    c[1].metric("Total anticipos", money(total))
    if resumen:
        df = pd.DataFrame([{
            "Proveedor": a["proveedor"], "NIT": a["nit"],
            "Saldo anticipo": money(a["saldo"]), "Movimientos": a["movimientos"],
        } for a in resumen])
        st.dataframe(df, use_container_width=True, hide_index=True)
        opts = {f"{a['proveedor']} (NIT {a['nit']})": a["nit"] for a in resumen}
        sel = st.selectbox("Ver movimientos de:", list(opts.keys()), key="ant_sel")
        movs = core.anticipos_movimientos(con, opts[sel])
        if movs:
            st.markdown("**Movimientos**")
            st.dataframe(pd.DataFrame([{
                "Fecha": m["fecha"], "Valor": money(m["valor"]), "Origen": m["origen"],
                "Comprobante": m["numero_comprobante"], "Usuario": m["usuario"],
            } for m in movs]), use_container_width=True, hide_index=True)
    else:
        st.info("Aun no hay anticipos. Se generan automaticamente al pagar o abonar mas del total adeudado.")


# ---- sidebar / navegacion ----
with st.sidebar:
    st.markdown(f"<h1 style='color:{NAVY};margin-bottom:0'>SUPRE</h1>"
                "<div style='color:#666;font-size:13px;margin-top:-6px'>Tesoreria Integral - CxP</div>",
                unsafe_allow_html=True)
    st.write("")
    st.markdown(
        "<div style='color:#1f3864;font-weight:700;font-size:12px;"
        "text-transform:uppercase;letter-spacing:.5px'>Seguimiento y control de cuentas por pagar</div>",
        unsafe_allow_html=True)
    pagina = st.radio("Navegacion",
                      ["Dashboard", "Carga masiva", "Facturas", "Pagos", "Anulaciones"],
                      label_visibility="collapsed")
    st.write("")
    st.markdown(
        "<div style='color:#9aa5b1;font-weight:700;font-size:12px;"
        "text-transform:uppercase;letter-spacing:.5px'>Archivos planos bancos</div>",
        unsafe_allow_html=True)
    st.caption("Proximamente (otro proyecto)")
    st.write("---")
    st.caption(f"Usuario: **{USUARIO}**")
    n = con.execute("SELECT COUNT(*) n FROM factura WHERE activo=1").fetchone()["n"]
    st.caption(f"Facturas en sistema: **{n}**")
    with st.expander("Opciones"):
        if st.button("Reiniciar base de datos", use_container_width=True):
            core.reset_db(con)
            st.success("Base de datos reiniciada.")
            st.rerun()


# ===============================================================================
# DASHBOARD
# ===============================================================================
if pagina == "Dashboard":
    st.title("Dashboard de Tesoreria")
    st.caption("Cuentas por pagar del grupo Supre - saldo de tesoreria en tiempo real")
    d = core.dashboard_data(con)
    k = d["kpis"]
    if k["n_facturas"] == 0:
        st.info("No hay datos todavia. Ve a **Carga masiva** y sube tu archivo de cuentas por pagar "
                "(o el cuentas_por_pagar_ejemplo.csv incluido en el repositorio).")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total cuentas por pagar", money(k["total_cxp"]), f"{k['n_facturas']} facturas")
        c2.metric("Total pendiente", money(k["total_pendiente"]))
        c3.metric("Total pagado a cartera", money(k["total_pagado"]))
        c4.metric("Total pagado a anticipo disponible", money(k["total_anticipo"]))
        c5, c6 = st.columns(2)
        c5.metric("Facturas vencidas", k["n_vencidas"], f"{money(k['monto_vencido'])} en mora",
                  delta_color="inverse")
        c6.metric("Por vencer (corriente)", k["n_por_vencer"], money(k["monto_por_vencer"]))

        st.write("")
        g1, g2 = st.columns(2)
        with g1:
            st.subheader("Antiguedad de saldos")
            df = pd.DataFrame({"Rango": list(d["aging"].keys()),
                               "Saldo": list(d["aging"].values())}).set_index("Rango")
            st.bar_chart(df, color=BLUE, height=260)
        with g2:
            st.subheader("Facturas por estado")
            de = pd.DataFrame({"Estado": list(d["estados"].keys()),
                               "Cantidad": list(d["estados"].values())}).set_index("Estado")
            st.bar_chart(de, color="#7fb3e0", height=260)
        g3, g4 = st.columns(2)
        with g3:
            st.subheader("Flujo de pagos diario")
            if d["flujo"]:
                dfl = pd.DataFrame(d["flujo"], columns=["Fecha", "Pagos"]).set_index("Fecha")
                st.line_chart(dfl, color=BLUE, height=260)
            else:
                st.caption("Aun no hay pagos registrados.")
        with g4:
            st.subheader("Saldo por empresa")
            if d["empresas"]:
                dem = pd.DataFrame({"Empresa": list(d["empresas"].keys()),
                                    "Saldo": list(d["empresas"].values())}).set_index("Empresa")
                st.bar_chart(dem, color=NAVY, height=260, horizontal=True)


# ===============================================================================
# CARGA MASIVA
# ===============================================================================
elif pagina == "Carga masiva":
    st.title("Carga masiva de cuentas por pagar")
    st.caption("Sube el reporte (Excel .xlsx o CSV). El sistema valida estructura, evita duplicados "
               "y consolida contra el historico mediante la llave unica empresa|NIT|factura.")
    archivo = st.file_uploader("Selecciona tu archivo", type=["csv", "xlsx", "xls"])
    if archivo is not None:
        if st.button("Validar y cargar", type="primary"):
            try:
                resumen, errores = core.procesar_carga(con, archivo.name, archivo.getvalue(), USUARIO)
                msg = (f"Carga procesada: {resumen['leidas']} leidas - {resumen['nuevas']} nuevas - "
                       f"{resumen['actualizadas']} actualizadas - {resumen['sin_cambios']} sin cambios - "
                       f"{resumen['rechazadas']} rechazadas.")
                (st.success if resumen["rechazadas"] == 0 else st.warning)(msg)
                cc = st.columns(5)
                cc[0].metric("Leidas", resumen["leidas"])
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

    with st.expander("Estructura esperada del archivo"):
        st.write("**Columnas obligatorias:** " + ", ".join("`" + c + "`" for c in core.REQUIRED_COLS))
        st.write("**Recomendadas (reporte de antiguedad):** "
                 + ", ".join("`" + c + "`" for c in core.EXPECTED_COLS))

    st.subheader("Cargas recientes")
    cargas = core.cargas_recientes(con)
    if cargas:
        df = pd.DataFrame(cargas)[["id", "nombre_archivo", "fecha", "total_leidas",
                                   "nuevas", "actualizadas", "sin_cambios", "rechazadas"]]
        df.columns = ["#", "Archivo", "Fecha", "Leidas", "Nuevas", "Actualizadas",
                      "Sin cambios", "Rechazadas"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("Aun no hay cargas.")


# ===============================================================================
# FACTURAS
# ===============================================================================
elif pagina == "Facturas":
    st.title("Facturas (Cuentas por pagar)")
    tab_estado, tab_list, tab_abono, tab_ant = st.tabs(
        ["Estado del proveedor", "Listado y gestion", "Abono por proveedor", "Saldo anticipo proveedor"])

    with tab_estado:
        _estado_proveedor(con)

    with tab_list:
        f1, f2, f3, f4 = st.columns([3, 2, 2, 1])
        q = f1.text_input("Buscar", placeholder="Proveedor, NIT o N factura")
        empresa = f2.selectbox("Empresa", [""] + core.empresas_distintas(con),
                               format_func=lambda x: "Todas" if x == "" else x)
        estado = f3.selectbox("Estado", [""] + core.ESTADOS,
                              format_func=lambda x: "Todos" if x == "" else x)
        solo_v = f4.checkbox("Vencidas")

        items = core.list_facturas(con, q=q, estado=estado, empresa=empresa, solo_vencidas=solo_v)
        total = sum(i["saldo_tesoreria"] for i in items)
        st.caption(f"{len(items)} facturas - saldo de tesoreria total: **{money(total)}**")

        if items:
            df = pd.DataFrame([{
                "ID": i["id"], "Empresa": i["empresa"], "Proveedor": i["proveedor"],
                "N factura": i["numero_factura"], "Vencimiento": i["fecha_vencimiento"] or "-",
                "Antiguedad": i["cubeta"], "Saldo contable": money(i["saldo_contable"]),
                "Saldo tesoreria": money(i["saldo_tesoreria"]), "Estado": i["estado"],
            } for i in items])
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.write("---")
            ids = [i["id"] for i in items]
            fid = st.selectbox("Abrir factura (ID)", ids,
                               format_func=lambda x: f"#{x} - " + next(i["proveedor"] for i in items if i["id"] == x))
            _detalle_factura(con, fid)
        else:
            st.info("No hay facturas con estos filtros.")

    with tab_abono:
        _abono_por_proveedor(con)

    with tab_ant:
        _anticipos(con)


# ===============================================================================
# PAGOS
# ===============================================================================
elif pagina == "Pagos":
    st.title("Pagos registrados")
    st.caption("Registros agrupados por **numero de comprobante** (un comprobante = una operacion). "
               "Incluye lo aplicado a facturas y lo que se fue a anticipo. Puedes **anular** un "
               "comprobante mal registrado: se restablecen los saldos de cartera, anticipo y cruce.")
    grupos = core.comprobantes_resumen(con)
    total_caja = core.total_caja_pagada(con)
    c1, c2 = st.columns(2)
    c1.metric("Total pagado (caja)", money(total_caja))
    c2.metric("Comprobantes", len(grupos))
    if grupos:
        df = pd.DataFrame([{
            "Comprobante": g["comprobante"], "Fecha": g["fecha"], "Empresa": g["empresa"],
            "Proveedor": g["proveedor"], "Facturas": g["n_facturas"],
            "Aplicado a facturas": money(g["valor_facturas"]),
            "A anticipo": money(g["valor_anticipo"]),
            "Tipo": "Cruce de anticipo" if g["es_cruce"] else "Pago",
            "Total caja": money(g["total"]),
        } for g in grupos])
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.write("---")
        st.markdown("**Anular un comprobante**")
        st.caption("Selecciona el comprobante mal registrado. Al anular, las facturas vuelven a su "
                   "saldo anterior y el anticipo/cruce se revierte.")
        comps = [g["comprobante"] for g in grupos]
        col1, col2 = st.columns([2, 3])
        sel = col1.selectbox("Comprobante", comps, key="anular_sel")
        motivo = col2.text_input("Motivo de la anulacion", key="anular_motivo")
        gsel = next((g for g in grupos if g["comprobante"] == sel), None)
        if gsel:
            st.caption(f"Proveedor: {gsel['proveedor']} - aplicado a facturas: "
                       f"{money(gsel['valor_facturas'])} - a anticipo: {money(gsel['valor_anticipo'])}")
        if st.button("Anular comprobante", type="primary"):
            ok, m = core.anular_comprobante(con, sel, USUARIO, motivo or "Anulacion de pago")
            (st.success if ok else st.error)(m)
            if ok:
                st.rerun()
    else:
        st.info("Aun no hay pagos registrados.")


elif pagina == "Anulaciones":
    st.title("Historico de anulaciones")
    st.caption("Seguimiento de los comprobantes anulados: que se anulo, cuanto, quien y por que. "
               "Los movimientos anulados ya no afectan saldos ni reportes.")
    hist = core.anulaciones_historico(con)
    total = round(sum((h["valor_facturas"] or 0) for h in hist), 2)
    c1, c2 = st.columns(2)
    c1.metric("Comprobantes anulados", len(hist))
    c2.metric("Valor anulado (aplicado a facturas)", money(total))
    if hist:
        df = pd.DataFrame([{
            "Fecha anulacion": h["fecha"], "Comprobante": h["comprobante"], "Tipo": h["tipo"],
            "Empresa": h["empresa"], "Proveedor": h["proveedor"],
            "Valor facturas": money(h["valor_facturas"]), "Valor anticipo": money(h["valor_anticipo"]),
            "Pagos": h["n_pagos"], "Mov. anticipo": h["n_anticipos"],
            "Usuario": h["usuario"], "Motivo": h["motivo"],
        } for h in hist])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Aun no se ha anulado ningun comprobante.")
