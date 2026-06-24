"""
streamlit_app.py - Modulo de Tesoreria Integral (Grupo Supre)
App Streamlit para cargar, validar y gestionar Cuentas por Pagar.
Despliegue: GitHub + https://share.streamlit.io
"""
import io
import datetime as dt
import pandas as pd
import streamlit as st

import core
import planos

st.set_page_config(page_title="Tesoreria Integral - Supre", page_icon=":moneybag:", layout="wide")

NAVY = "#1f3864"
BLUE = "#2e75b6"


@st.cache_resource
def conexion():
    con = core.get_conn()
    core.init_db(con)
    planos.init_planos(con)
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
                medio = cc[0].selectbox("Medio de pago *", core.MEDIOS_PAGO)
                comprobante = cc[1].text_input("N comprobante *")
                notas_p = st.text_input("Notas")
                if st.form_submit_button("Registrar pago", type="primary"):
                    datos = dict(fecha_pago=fecha_pago.isoformat(), valor_pagado=valor, banco="",
                                 cuenta_bancaria="", medio_pago=medio,
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
        medio = cc[1].selectbox("Medio de pago *", core.MEDIOS_PAGO)
        comprobante = cc[0].text_input("N comprobante *")
        notas = cc[1].text_input("Notas")
        if st.form_submit_button("Registrar abono por proveedor", type="primary"):
            datos = dict(fecha_pago=fecha_pago.isoformat(), banco="", cuenta_bancaria="",
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
    categoria = st.radio("Categoria",
                         ["Seguimiento y control de cuentas por pagar", "Archivos planos bancos"],
                         label_visibility="collapsed", key="categoria")
    st.write("")
    if categoria == "Seguimiento y control de cuentas por pagar":
        st.markdown(
            "<div style='color:#1f3864;font-weight:700;font-size:12px;"
            "text-transform:uppercase;letter-spacing:.5px'>Seguimiento y control de cuentas por pagar</div>",
            unsafe_allow_html=True)
        pagina = st.radio("Seccion",
                          ["Carga masiva", "Dashboard", "Facturas", "Pagos", "Anulaciones"],
                          label_visibility="collapsed", key="pg_cxp")
    else:
        st.markdown(
            "<div style='color:#1f3864;font-weight:700;font-size:12px;"
            "text-transform:uppercase;letter-spacing:.5px'>Archivos planos bancos</div>",
            unsafe_allow_html=True)
        pagina = st.radio("Seccion",
                          ["Parametros Bancarios", "Dispersion de Nomina", "Historial",
                           "Dashboard Planos", "Empresas"],
                          label_visibility="collapsed", key="pg_ap")
    st.write("---")
    st.caption(f"Usuario: **{USUARIO}**")
    n = con.execute("SELECT COUNT(*) n FROM factura WHERE activo=1").fetchone()["n"]
    st.caption(f"Facturas en sistema: **{n}**")
    with st.expander("Opciones"):
        if st.button("Limpiar Tesoreria Integral (CxP)", use_container_width=True):
            core.reset_db(con)
            st.success("Datos de Tesoreria Integral reiniciados.")
            st.rerun()
        if st.button("Limpiar Archivos Planos (bancos)", use_container_width=True):
            planos.reset_planos(con)
            st.session_state.pop("ap_eid", None)
            st.session_state.pop("ap_err", None)
            st.success("Datos de Archivos Planos reiniciados.")
            st.rerun()


# ===============================================================================
# CATEGORIA: ARCHIVOS PLANOS BANCOS
# ===============================================================================
if categoria == "Archivos planos bancos":
    import zipfile
    XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    if pagina == "Dispersion de Nomina":
        st.title("Dispersion de Nomina")
        st.caption("Carga el archivo de Nomina y el de Informacion Bancaria, elige la fecha de "
                   "aplicacion y ejecuta. El sistema une por cedula, valida, clasifica el banco y "
                   "genera los archivos por Empresa + Banco.")
        c1, c2 = st.columns(2)
        f_nom = c1.file_uploader("Nomina (cedula, nombre, valor_pagar, empresa)",
                                 type=["csv", "xlsx", "xls"], key="ap_nom")
        f_ban = c2.file_uploader("Informacion bancaria (cedula, nombre, tipo_cuenta, numero_cuenta, entidad_bancaria)",
                                 type=["csv", "xlsx", "xls"], key="ap_ban")
        fecha_ap = st.date_input("Fecha de aplicacion", value=dt.date.today(), format="YYYY-MM-DD")

        seleccion = {}
        nrows = brows = None
        err_lectura = None
        if f_nom is not None and f_ban is not None:
            nrows, _, e1 = planos.leer_tabla(f_nom.name, f_nom.getvalue())
            brows, _, e2 = planos.leer_tabla(f_ban.name, f_ban.getvalue())
            err_lectura = e1 or e2
            if not err_lectura:
                dups = planos.cedulas_duplicadas_nomina(nrows)
                if dups:
                    st.warning("Novedad: %d cedula(s) aparecen repetidas en el archivo de "
                               "nomina. Revisa antes de ejecutar (en el proceso solo se toma "
                               "el primer registro de cada cedula)." % len(dups))
                    st.dataframe(pd.DataFrame([{
                        "Cedula": d["cedula"], "Nombre": d["nombre"], "Veces": d["veces"],
                        "Empresas": ", ".join(d["empresas"])} for d in dups]),
                        use_container_width=True, hide_index=True)
                conflictos = planos.conflictos_bancarios(brows)
                if conflictos:
                    st.warning("Se encontraron %d cedula(s) con cuentas diferentes en el archivo "
                               "bancario. Selecciona cual usar para cada una antes de ejecutar."
                               % len(conflictos))
                    for ced, ops in conflictos.items():
                        labels = {o["sig"]: "%s - %s - cuenta %s" % (o["entidad"], o["tipo_cuenta"], o["numero_cuenta"])
                                  for o in ops}
                        nombre = ops[0].get("nombre", "")
                        sig = st.radio("Cedula %s  %s" % (ced, ("- " + nombre) if nombre else ""),
                                       list(labels.keys()), format_func=lambda x: labels[x],
                                       key="conf_%s" % ced)
                        seleccion[ced] = sig

        if st.button("Ejecutar proceso", type="primary"):
            if f_nom is None or f_ban is None:
                st.error("Debes cargar los dos archivos.")
            elif err_lectura:
                st.error(err_lectura)
            else:
                proc = planos.procesar(con, nrows, brows, fecha_ap.isoformat(), seleccion=seleccion)
                eid, _arch = planos.guardar_ejecucion(con, proc, USUARIO)
                st.session_state["ap_eid"] = eid
                st.session_state["ap_err"] = proc["errores"]
                st.session_state["ap_sin_cuenta"] = planos.empresas_sin_cuenta(con, proc)
                st.rerun()

        eid = st.session_state.get("ap_eid")
        if eid:
            arch = planos.archivos_de(con, eid)
            errs = st.session_state.get("ap_err", [])
            st.success("Proceso ejecutado. Ejecucion #%d." % eid)
            kk = st.columns(4)
            kk[0].metric("Archivos generados", len(arch))
            kk[1].metric("Empleados", sum(a["n_empleados"] for a in arch))
            kk[2].metric("Valor dispersado", money(sum(a["valor_total"] for a in arch)))
            kk[3].metric("Inconsistencias", len(errs))
            sin_cuenta = st.session_state.get("ap_sin_cuenta", [])
            if sin_cuenta:
                st.warning("Validacion de empresas pagadoras: %d empresa(s)+banco no tienen "
                           "cuenta pagadora configurada. Los archivos se generaron, pero el "
                           "encabezado (NIT pagador, cuenta a debitar) puede salir incompleto. "
                           "Crea la cuenta en Parametros Bancarios > Cuentas Pagadoras."
                           % len(sin_cuenta))
                st.dataframe(pd.DataFrame(sin_cuenta), use_container_width=True, hide_index=True)
            if arch:
                st.markdown("**Archivos generados (Empresa + Banco)**")
                st.dataframe(pd.DataFrame([{
                    "Empresa": a["empresa"], "Banco": a["banco"], "Empleados": a["n_empleados"],
                    "Valor total": money(a["valor_total"]), "Secuencia": a["secuencia"] or "-",
                    "Archivo": a["nombre_archivo"]} for a in arch]),
                    use_container_width=True, hide_index=True)
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w") as zf:
                    for a in arch:
                        if a["estructura"]:
                            zf.writestr(a["nombre_archivo"], planos.archivo_xlsx_bytes(a["estructura"]))
                st.download_button("Descargar todos (ZIP)", data=zbuf.getvalue(),
                                   file_name="archivos_planos_%d.zip" % eid, mime="application/zip")
                for a in arch:
                    if a["estructura"]:
                        st.download_button("Descargar " + a["nombre_archivo"],
                                           data=planos.archivo_xlsx_bytes(a["estructura"]),
                                           file_name=a["nombre_archivo"], mime=XLSX_MIME,
                                           key="dl_%d" % a["id"])
            if errs:
                st.markdown("**Inconsistencias**")
                st.dataframe(pd.DataFrame(errs), use_container_width=True, hide_index=True)
                st.download_button("Descargar inconsistencias (Excel)",
                                   data=planos.errores_xlsx_bytes(errs),
                                   file_name="inconsistencias_%d.xlsx" % eid, mime=XLSX_MIME)

    elif pagina == "Historial":
        st.title("Historial de ejecuciones")
        ejs = planos.ejecuciones(con)
        if not ejs:
            st.info("Aun no hay ejecuciones.")
        else:
            st.dataframe(pd.DataFrame([{
                "#": e["id"], "Fecha": e["fecha"], "Usuario": e["usuario"],
                "Fecha aplicacion": e["fecha_aplicacion"], "Empleados": e["total_empleados"],
                "Valor": money(e["total_valor"]), "Archivos": e["n_archivos"],
                "Errores": e["n_errores"]} for e in ejs]),
                use_container_width=True, hide_index=True)
            sel = st.selectbox("Ver archivos de la ejecucion #", [e["id"] for e in ejs])
            for a in planos.archivos_de(con, sel):
                if a["estructura"]:
                    st.download_button(
                        "%s  (%d empleados, %s)" % (a["nombre_archivo"], a["n_empleados"], money(a["valor_total"])),
                        data=planos.archivo_xlsx_bytes(a["estructura"]), file_name=a["nombre_archivo"],
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="h_%d" % a["id"])

    elif pagina == "Dashboard Planos":
        st.title("Dashboard - Archivos Planos")
        d = planos.dashboard_planos(con)
        kk = st.columns(4)
        kk[0].metric("Total empleados procesados", d["tot_emp"])
        kk[1].metric("Total valor dispersado", money(d["tot_val"]))
        kk[2].metric("Total archivos generados", d["n_arch"])
        kk[3].metric("Total errores", d["n_err"])
        if d["resumen"]:
            st.markdown("**Resumen por Empresa + Banco**")
            st.dataframe(pd.DataFrame([{
                "Empresa": r["empresa"], "Banco": r["banco"], "Empleados": r["emp"],
                "Valor Total": money(r["val"])} for r in d["resumen"]]),
                use_container_width=True, hide_index=True)
        else:
            st.info("Aun no hay datos. Ejecuta una dispersion.")

    elif pagina == "Empresas":
        st.title("Empresas")
        with st.form("ap_emp_new", clear_on_submit=True):
            cc = st.columns([3, 2, 1])
            nom = cc[0].text_input("Nombre empresa")
            nit = cc[1].text_input("NIT")
            cc[2].markdown("&nbsp;")
            if st.form_submit_button("Crear empresa", type="primary"):
                if nom.strip() and nit.strip():
                    planos.crear_empresa(con, nom, nit); st.success("Empresa creada."); st.rerun()
                else:
                    st.error("Nombre y NIT son obligatorios.")
        for e in planos.empresas(con):
            cols = st.columns([3, 2, 1, 1])
            cols[0].write(e["nombre"]); cols[1].write("NIT " + e["nit"])
            cols[2].write("Activa" if e["activo"] else "Inactiva")
            if cols[3].button("Inactivar" if e["activo"] else "Activar", key="em_%d" % e["id"]):
                planos.editar_empresa(con, e["id"], e["nombre"], e["nit"], not e["activo"]); st.rerun()

    elif pagina == "Parametros Bancarios":
        st.title("Parametros Bancarios")
        st.caption("Configura los campos fijos, codigos de banco y tipos de producto, y adjunta el "
                   "archivo modelo de cada banco. Si el banco cambia el formato o los codigos, lo "
                   "actualizas aqui sin tocar el codigo.")
        tabB, tabD, tabC = st.tabs(["Bancolombia", "Davivienda", "Cuentas Pagadoras"])

        with tabB:
            pb = planos.get_param(con, "Bancolombia")
            with st.form("par_banc"):
                st.markdown("**Campos fijos**")
                cc = st.columns(2)
                td = cc[0].text_input("Tipo Documento Beneficiario", value=pb.get("tipo_doc_beneficiario", "1"))
                tt = cc[1].text_input("Tipo Transaccion", value=pb.get("tipo_transaccion", "37"))
                st.markdown("**Codigos de banco** (una linea por entidad, formato ENTIDAD=CODIGO)")
                cod_txt = "\n".join("%s=%s" % (k, v) for k, v in pb.get("codigos", {}).items())
                cod = st.text_area("Codigos", value=cod_txt, height=110,
                                   help="Las entidades que aparezcan aqui se enrutan al archivo Bancolombia con su codigo.")
                st.markdown("**Tipo de transaccion por codigo** (una linea por codigo, formato CODIGO=TRANSACCION)")
                tr_txt = "\n".join("%s=%s" % (k, v) for k, v in pb.get("transacciones", {}).items())
                tr = st.text_area("Transacciones", value=tr_txt, height=90,
                                  help="Ej: 1007=37 (Bancolombia), 1507=52 (Nequi). Se usa segun el codigo de banco de cada empleado.")
                if st.form_submit_button("Guardar parametros Bancolombia", type="primary"):
                    codigos = {}
                    for ln in cod.splitlines():
                        if "=" in ln:
                            k, v = ln.split("=", 1)
                            if k.strip():
                                codigos[k.strip().upper()] = v.strip()
                    transacciones = {}
                    for ln in tr.splitlines():
                        if "=" in ln:
                            k, v = ln.split("=", 1)
                            if k.strip():
                                transacciones[k.strip()] = v.strip()
                    planos.set_param(con, "Bancolombia", {
                        "tipo_doc_beneficiario": td.strip(), "tipo_transaccion": tt.strip(),
                        "codigos": codigos, "transacciones": transacciones})
                    st.success("Parametros de Bancolombia guardados."); st.rerun()
            st.markdown("**Archivo modelo**")
            nom, blob = planos.modelo_de(con, "Bancolombia")
            if nom:
                st.caption("Modelo actual: " + nom)
                st.download_button("Descargar modelo", data=blob, file_name=nom, key="dl_mod_b")
            else:
                st.caption("Aun no hay archivo modelo adjunto.")
            mfb = st.file_uploader("Adjuntar / actualizar modelo Bancolombia",
                                   type=["xls", "xlsx", "csv", "txt"], key="up_mod_b")
            if mfb is not None and st.button("Guardar modelo Bancolombia", key="btn_mod_b"):
                planos.guardar_modelo(con, "Bancolombia", mfb.name, mfb.getvalue())
                st.success("Modelo guardado."); st.rerun()
            st.caption("Estructura - Encabezado: " + ", ".join(planos.BANCOLOMBIA_HEADER))
            st.caption("Estructura - Detalle: " + ", ".join(planos.BANCOLOMBIA_DETALLE))

        with tabD:
            pdv = planos.get_param(con, "Davivienda")
            with st.form("par_davi"):
                st.markdown("**Campos fijos**")
                cc = st.columns(2)
                ti = cc[0].text_input("Tipo Identificacion", value=pdv.get("tipo_identificacion", "1"))
                cb = cc[1].text_input("Codigo del Banco", value=pdv.get("codigo_banco", "51"))
                st.markdown("**Tipos de producto** (una linea por entidad, formato ENTIDAD=PRODUCTO)")
                pr_txt = "\n".join("%s=%s" % (k, v) for k, v in pdv.get("productos", {}).items())
                pr = st.text_area("Productos", value=pr_txt, height=120,
                                  help="Ej: DAVIPLATA=DP, DAVIVIENDA=CA. Estas entidades se enrutan al archivo Davivienda.")
                if st.form_submit_button("Guardar parametros Davivienda", type="primary"):
                    productos = {}
                    for ln in pr.splitlines():
                        if "=" in ln:
                            k, v = ln.split("=", 1)
                            if k.strip():
                                productos[k.strip().upper()] = v.strip()
                    planos.set_param(con, "Davivienda", {
                        "tipo_identificacion": ti.strip(), "codigo_banco": cb.strip(),
                        "productos": productos})
                    st.success("Parametros de Davivienda guardados."); st.rerun()
            st.markdown("**Archivo modelo**")
            nom, blob = planos.modelo_de(con, "Davivienda")
            if nom:
                st.caption("Modelo actual: " + nom)
                st.download_button("Descargar modelo", data=blob, file_name=nom, key="dl_mod_d")
            else:
                st.caption("Aun no hay archivo modelo adjunto.")
            mfd = st.file_uploader("Adjuntar / actualizar modelo Davivienda",
                                   type=["xls", "xlsx", "csv", "txt"], key="up_mod_d")
            if mfd is not None and st.button("Guardar modelo Davivienda", key="btn_mod_d"):
                planos.guardar_modelo(con, "Davivienda", mfd.name, mfd.getvalue())
                st.success("Modelo guardado."); st.rerun()
            st.caption("Estructura - Columnas: " + ", ".join(planos.DAVIVIENDA_COLS))

        with tabC:
            st.markdown("**Cuentas pagadoras** (por Empresa + Banco)")
            emps = planos.empresas(con, solo_activas=True)
            if not emps:
                st.info("Crea primero una empresa en la seccion Empresas.")
            else:
                with st.form("ap_cta_new", clear_on_submit=True):
                    cc = st.columns(3)
                    emp = cc[0].selectbox("Empresa", emps, format_func=lambda e: e["nombre"])
                    banco = cc[1].selectbox("Banco", ["Bancolombia", "Davivienda"])
                    numero = cc[2].text_input("Numero de cuenta")
                    cc2 = st.columns(3)
                    tipo = cc2[0].selectbox("Tipo de cuenta", ["S", "D"])
                    nitp = cc2[1].text_input("NIT pagador")
                    desc = cc2[2].text_input("Descripcion del pago", value="Pago nomina")
                    cc3 = st.columns(2)
                    tpago = cc3[0].text_input("Tipo de pago (Bancolombia)", value="220")
                    aplic = cc3[1].text_input("Aplicacion (Bancolombia)", value="I")
                    if st.form_submit_button("Crear cuenta", type="primary"):
                        planos.crear_cuenta(con, emp["id"], banco, numero, tipo, nitp, desc, tpago, aplic)
                        st.success("Cuenta creada."); st.rerun()
                st.markdown("**Cuentas registradas**")
                cs = planos.cuentas(con)
                if cs:
                    for c in cs:
                        cols = st.columns([2.6, 1.3, 2.2, 1, 1.1, 1])
                        cols[0].write(c["empresa_nombre"])
                        cols[1].write(c["banco"])
                        cols[2].write("Cta %s (%s) - NIT %s" % (c["numero_cuenta"], c["tipo_cuenta"], c["nit_pagador"]))
                        cols[3].write("Activa" if c["activa"] else "Inactiva")
                        if cols[4].button("Inactivar" if c["activa"] else "Activar", key="tg_cta_%d" % c["id"]):
                            planos.toggle_cuenta(con, c["id"], not c["activa"]); st.rerun()
                        if cols[5].button("Eliminar", key="del_cta_%d" % c["id"]):
                            planos.eliminar_cuenta(con, c["id"]); st.success("Cuenta eliminada."); st.rerun()
                else:
                    st.caption("Sin cuentas.")

    st.stop()



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
    marcar = st.checkbox(
        "Dar por pagadas las facturas que ya no vienen en el archivo (Actualizada por archivo plano)",
        value=True,
        help="Solo afecta a las empresas presentes en el archivo cargado. Las facturas activas de "
             "esas empresas que no aparezcan se marcan como Pagadas y quedan en Pagos.")
    if archivo is not None:
        if st.button("Validar y cargar", type="primary"):
            try:
                resumen, errores = core.procesar_carga(con, archivo.name, archivo.getvalue(),
                                                       USUARIO, marcar_faltantes=marcar)
                msg = (f"Carga procesada: {resumen['leidas']} leidas - {resumen['nuevas']} nuevas - "
                       f"{resumen['actualizadas']} actualizadas - {resumen['sin_cambios']} sin cambios - "
                       f"{resumen['rechazadas']} rechazadas.")
                (st.success if resumen["rechazadas"] == 0 else st.warning)(msg)
                cc = st.columns(6)
                cc[0].metric("Leidas", resumen["leidas"])
                cc[1].metric("Nuevas", resumen["nuevas"])
                cc[2].metric("Actualizadas", resumen["actualizadas"])
                cc[3].metric("Sin cambios", resumen["sin_cambios"])
                cc[4].metric("Rechazadas", resumen["rechazadas"])
                cc[5].metric("Pagadas x archivo", resumen.get("pagadas_archivo", 0))
                if resumen.get("pagadas_archivo", 0):
                    st.info("%d factura(s) ya no venian en el archivo: se marcaron como Pagadas "
                            "(Actualizada por archivo plano) y quedan en Pagos."
                            % resumen["pagadas_archivo"])
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
