"""
planos.py - Modulo "Archivos Planos" (dispersion de nomina) - Grupo Supre.
Genera archivos bancarios (Bancolombia / Davivienda) por Empresa + Banco a partir
de un archivo de nomina y un archivo de informacion bancaria, unidos por cedula.
Sin dependencias de Streamlit.
"""
import io
import json
import datetime as dt

try:
    import pandas as pd
    import openpyxl
    HAS_LIBS = True
except Exception:
    HAS_LIBS = False

# ---- Estructura de los archivos planos (segun modelos del banco) ----
BANCOLOMBIA_HEADER = ["NIT PAGADOR", "TIPO DE PAGO", "APLICACION", "SECUENCIA DE ENVIO",
                      "NRO CUENTA A DEBITAR", "TIPO DE CUENTA A DEBITAR", "DESCRIPCION DEL PAGO"]
BANCOLOMBIA_DETALLE = ["Tipo Documento Beneficiario", "Nit Beneficiario", "Nombre Beneficiario",
                       "Tipo Transaccion", "Codigo Banco", "No Cuenta Beneficiario", "Email",
                       "Documento Autorizado", "Referencia", "Celular Beneficiario",
                       "ValorTransaccion", "Fecha de aplicacion"]
DAVIVIENDA_COLS = ["Tipo de Identificacion", "Numero de Identificacion", "Nombre", "Apellido",
                   "Codigo del Banco", "Tipo de Producto o Servicio", "Numero de producto o servicio",
                   "Valor del pago o la recarga"]

EMPRESAS_SEED = [
    ("Supremotos SAS", "900768559"),
    ("Suprecredito SAS", "901347233"),
    ("Movicap SAS", "901869465"),
    ("Mañana de Pascua SAS", "901347260"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_empresa (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT, nit TEXT, activo INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ap_cuenta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id INTEGER, banco TEXT, numero_cuenta TEXT, tipo_cuenta TEXT,
    nit_pagador TEXT, descripcion_pago TEXT, tipo_pago TEXT, aplicacion TEXT,
    activa INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ap_consecutivo (
    empresa_id INTEGER, banco TEXT, ultima_secuencia INTEGER DEFAULT 0,
    PRIMARY KEY (empresa_id, banco)
);
CREATE TABLE IF NOT EXISTS ap_ejecucion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT, usuario TEXT, fecha_aplicacion TEXT,
    total_empleados INTEGER, total_valor REAL, n_archivos INTEGER, n_errores INTEGER
);
CREATE TABLE IF NOT EXISTS ap_archivo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ejecucion_id INTEGER, empresa TEXT, banco TEXT, n_empleados INTEGER,
    valor_total REAL, secuencia INTEGER, nombre_archivo TEXT, datos_json TEXT
);
"""


def init_planos(con):
    con.executescript(SCHEMA)
    n = con.execute("SELECT COUNT(*) c FROM ap_empresa").fetchone()[0]
    if n == 0:
        for nombre, nit in EMPRESAS_SEED:
            con.execute("INSERT INTO ap_empresa (nombre, nit, activo) VALUES (?,?,1)", (nombre, nit))
        con.commit()
        # cuenta pagadora de ejemplo (Supremotos / Bancolombia) tomada del modelo
        e = con.execute("SELECT id FROM ap_empresa WHERE nit='900768559'").fetchone()
        if e:
            con.execute(
                "INSERT INTO ap_cuenta (empresa_id, banco, numero_cuenta, tipo_cuenta, nit_pagador,"
                " descripcion_pago, tipo_pago, aplicacion, activa) VALUES (?,?,?,?,?,?,?,?,1)",
                (e["id"], "Bancolombia", "2331597140", "S", "900768559", "Pago nomina", "220", "I"))
        con.commit()


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm(v):
    return str(v if v is not None else "").strip().upper()


def solo_digitos(v):
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def to_float(v):
    if v is None:
        return 0.0
    s = str(v).strip().replace("$", "").replace(" ", "")
    if s == "" or s.lower() == "nan":
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    elif "." in s:
        if s.count(".") > 1 or len(s.split(".")[-1]) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


# ---- Maestros ----
def empresas(con, solo_activas=False):
    sql = "SELECT * FROM ap_empresa"
    if solo_activas:
        sql += " WHERE activo=1"
    sql += " ORDER BY nombre"
    return [dict(r) for r in con.execute(sql).fetchall()]


def crear_empresa(con, nombre, nit):
    con.execute("INSERT INTO ap_empresa (nombre, nit, activo) VALUES (?,?,1)", (nombre.strip(), solo_digitos(nit)))
    con.commit()


def editar_empresa(con, eid, nombre, nit, activo):
    con.execute("UPDATE ap_empresa SET nombre=?, nit=?, activo=? WHERE id=?",
                (nombre.strip(), solo_digitos(nit), 1 if activo else 0, eid))
    con.commit()


def cuentas(con, empresa_id=None):
    sql = ("SELECT c.*, e.nombre empresa_nombre FROM ap_cuenta c "
           "JOIN ap_empresa e ON e.id=c.empresa_id")
    p = []
    if empresa_id:
        sql += " WHERE c.empresa_id=?"
        p.append(empresa_id)
    sql += " ORDER BY e.nombre, c.banco"
    return [dict(r) for r in con.execute(sql, p).fetchall()]


def crear_cuenta(con, empresa_id, banco, numero_cuenta, tipo_cuenta, nit_pagador,
                 descripcion_pago, tipo_pago="220", aplicacion="I"):
    con.execute(
        "INSERT INTO ap_cuenta (empresa_id, banco, numero_cuenta, tipo_cuenta, nit_pagador,"
        " descripcion_pago, tipo_pago, aplicacion, activa) VALUES (?,?,?,?,?,?,?,?,1)",
        (empresa_id, banco, numero_cuenta, tipo_cuenta, solo_digitos(nit_pagador),
         descripcion_pago, tipo_pago, aplicacion))
    con.commit()


def toggle_cuenta(con, cid, activa):
    con.execute("UPDATE ap_cuenta SET activa=? WHERE id=?", (1 if activa else 0, cid))
    con.commit()


def cuenta_de(con, empresa_id, banco):
    return con.execute(
        "SELECT * FROM ap_cuenta WHERE empresa_id=? AND banco=? AND activa=1 LIMIT 1",
        (empresa_id, banco)).fetchone()


# ---- Consecutivos ----
def consecutivo_actual(con, empresa_id, banco):
    r = con.execute("SELECT ultima_secuencia FROM ap_consecutivo WHERE empresa_id=? AND banco=?",
                    (empresa_id, banco)).fetchone()
    return r["ultima_secuencia"] if r else 0


def consecutivo_siguiente(con, empresa_id, banco):
    actual = consecutivo_actual(con, empresa_id, banco)
    nuevo = actual + 1
    con.execute(
        "INSERT INTO ap_consecutivo (empresa_id, banco, ultima_secuencia) VALUES (?,?,?) "
        "ON CONFLICT(empresa_id, banco) DO UPDATE SET ultima_secuencia=?",
        (empresa_id, banco, nuevo, nuevo))
    con.commit()
    return nuevo


def reiniciar_consecutivo(con, empresa_id, banco, valor=0):
    con.execute(
        "INSERT INTO ap_consecutivo (empresa_id, banco, ultima_secuencia) VALUES (?,?,?) "
        "ON CONFLICT(empresa_id, banco) DO UPDATE SET ultima_secuencia=?",
        (empresa_id, banco, valor, valor))
    con.commit()


def consecutivos(con):
    rows = con.execute(
        "SELECT s.empresa_id, e.nombre empresa, s.banco, s.ultima_secuencia "
        "FROM ap_consecutivo s JOIN ap_empresa e ON e.id=s.empresa_id "
        "ORDER BY e.nombre, s.banco").fetchall()
    return [dict(r) for r in rows]


# ---- Lectura de archivos ----
def leer_tabla(filename, raw):
    """Lee xlsx/xls/csv en lista de dicts con columnas en minuscula. Devuelve (rows, cols, error)."""
    name = (filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
        else:
            text = None
            for enc in ("utf-8-sig", "latin-1"):
                try:
                    text = raw.decode(enc); break
                except UnicodeDecodeError:
                    continue
            if text is None:
                return None, [], "No se pudo decodificar el archivo."
            import csv as _csv
            sample = text[:4096]
            delim = ";" if sample.count(";") > sample.count(",") else ","
            if sample.count("\t") > max(sample.count(";"), sample.count(",")):
                delim = "\t"
            df = pd.read_csv(io.StringIO(text), dtype=str, sep=delim)
        df.columns = [str(c).strip().lower() for c in df.columns]
        rows = df.fillna("").to_dict(orient="records")
        return rows, list(df.columns), None
    except Exception as e:
        return None, [], "Error leyendo el archivo: " + str(e)


def _get(row, *claves):
    for k in claves:
        for col in row:
            if col.strip().lower() == k:
                return row[col]
    return ""


# ---- Clasificacion de banco ----
def clasificar(entidad):
    """Devuelve (grupo, codigo_banco, tipo_producto) o (None,...) si no soportada."""
    e = norm(entidad)
    if "NEQUI" in e:
        return "Bancolombia", "1507", None
    if "BANCOLOMBIA" in e:
        return "Bancolombia", "1007", None
    if "DAVIPLATA" in e:
        return "Davivienda", "51", "DP"
    if "DAVIVIENDA" in e:
        return "Davivienda", "51", "CA"
    return None, None, None


# ---- Proceso de dispersion ----
def procesar(con, nomina_rows, banco_rows, fecha_aplicacion):
    """Une nomina + bancaria por cedula, valida y arma los grupos empresa+banco.
    Devuelve dict con grupos, errores y resumen (sin guardar todavia)."""
    # indice bancario por cedula
    idx = {}
    dup_banco = set()
    for r in banco_rows:
        ced = solo_digitos(_get(r, "cedula", "cédula", "identificacion", "nit"))
        if not ced:
            continue
        if ced in idx:
            dup_banco.add(ced)
        idx[ced] = r

    emp_by_nombre = {norm(e["nombre"]): e for e in empresas(con)}
    grupos = {}   # (empresa_nombre, grupo_banco) -> list de detalle
    errores = []
    vistos = set()

    for r in nomina_rows:
        ced = solo_digitos(_get(r, "cedula", "cédula", "identificacion"))
        nombre = str(_get(r, "nombre")).strip()
        valor = to_float(_get(r, "valor_pagar", "valor", "valor a pagar", "valor_pago"))
        emp_nom = str(_get(r, "empresa")).strip()
        if not ced:
            errores.append({"cedula": "", "nombre": nombre, "empresa": emp_nom, "motivo": "Cedula vacia en nomina"})
            continue
        if ced in vistos:
            errores.append({"cedula": ced, "nombre": nombre, "empresa": emp_nom, "motivo": "Cedula duplicada en nomina"})
            continue
        vistos.add(ced)
        if valor <= 0:
            errores.append({"cedula": ced, "nombre": nombre, "empresa": emp_nom, "motivo": "Valor a pagar <= 0"})
            continue
        b = idx.get(ced)
        if not b:
            errores.append({"cedula": ced, "nombre": nombre, "empresa": emp_nom, "motivo": "Empleado sin informacion bancaria"})
            continue
        numero_cuenta = str(_get(b, "numero_cuenta", "numero cuenta", "num_cuenta", "cuenta")).strip()
        tipo_cuenta = str(_get(b, "tipo_cuenta", "tipo cuenta")).strip()
        entidad = str(_get(b, "entidad_bancaria", "entidad", "banco")).strip()
        if not entidad:
            errores.append({"cedula": ced, "nombre": nombre, "empresa": emp_nom, "motivo": "Entidad bancaria vacia"})
            continue
        if not numero_cuenta:
            errores.append({"cedula": ced, "nombre": nombre, "empresa": emp_nom, "motivo": "Numero de cuenta vacio"})
            continue
        grupo, codigo, producto = clasificar(entidad)
        if grupo is None:
            errores.append({"cedula": ced, "nombre": nombre, "empresa": emp_nom,
                            "motivo": "Entidad no soportada: " + entidad})
            continue
        if not tipo_cuenta and grupo == "Davivienda":
            # producto se infiere de la entidad; tipo_cuenta no es obligatorio para Davivienda
            pass
        nombre_b = str(_get(b, "nombre")).strip() or nombre
        apellidos_b = str(_get(b, "apellidos", "apellido")).strip()
        if not apellidos_b:
            partes = nombre_b.split()
            if len(partes) > 1:
                nombre_b, apellidos_b = partes[0], " ".join(partes[1:])
        emp = emp_by_nombre.get(norm(emp_nom))
        key = (emp_nom, grupo)
        grupos.setdefault(key, []).append({
            "cedula": ced, "nombre": nombre_b, "apellidos": apellidos_b,
            "valor": round(valor, 2), "numero_cuenta": numero_cuenta,
            "tipo_cuenta": tipo_cuenta, "codigo_banco": codigo, "producto": producto,
            "entidad": entidad, "empresa_obj": emp,
        })

    for ced in dup_banco:
        errores.append({"cedula": ced, "nombre": "", "empresa": "", "motivo": "Cedula duplicada en archivo bancario"})

    resumen = []
    for (emp_nom, grupo), filas in grupos.items():
        resumen.append({"empresa": emp_nom, "banco": grupo, "empleados": len(filas),
                        "valor_total": round(sum(f["valor"] for f in filas), 2)})
    return {"grupos": grupos, "errores": errores, "resumen": resumen,
            "total_empleados": sum(len(v) for v in grupos.values()),
            "total_valor": round(sum(f["valor"] for v in grupos.values() for f in v), 2),
            "fecha_aplicacion": fecha_aplicacion}


def _slug(nombre):
    s = "".join(ch for ch in norm(nombre) if ch.isalnum() or ch == " ")
    return "".join(p.capitalize() for p in s.split())


def construir_filas(con, emp_nom, grupo, filas, fecha_aplicacion):
    """Devuelve dict con estructura del archivo (header + detalle) listo para xlsx."""
    emp = filas[0]["empresa_obj"]
    fecha_txt = str(fecha_aplicacion).replace("-", "")
    if grupo == "Bancolombia":
        cuenta = cuenta_de(con, emp["id"], "Bancolombia") if emp else None
        seq = consecutivo_actual(con, emp["id"], "Bancolombia") + 1 if emp else 0
        header_vals = [
            cuenta["nit_pagador"] if cuenta else (emp["nit"] if emp else ""),
            cuenta["tipo_pago"] if cuenta else "220",
            cuenta["aplicacion"] if cuenta else "I",
            seq,
            cuenta["numero_cuenta"] if cuenta else "",
            cuenta["tipo_cuenta"] if cuenta else "S",
            cuenta["descripcion_pago"] if cuenta else "Pago nomina",
        ]
        detalle = []
        for f in filas:
            detalle.append(["1", f["cedula"], f["nombre"] + (" " + f["apellidos"] if f["apellidos"] else ""),
                            "37", f["codigo_banco"], f["numero_cuenta"], "", "", "", "",
                            f["valor"], fecha_txt])
        return {"tipo": "Bancolombia", "header_cols": BANCOLOMBIA_HEADER, "header_vals": header_vals,
                "detalle_cols": BANCOLOMBIA_DETALLE, "detalle": detalle, "secuencia": seq}
    else:  # Davivienda
        detalle = []
        for f in filas:
            detalle.append(["1", f["cedula"], f["nombre"], f["apellidos"], "51",
                            f["producto"] or "CA", f["numero_cuenta"], f["valor"]])
        return {"tipo": "Davivienda", "header_cols": None, "header_vals": None,
                "detalle_cols": DAVIVIENDA_COLS, "detalle": detalle, "secuencia": None}


def archivo_xlsx_bytes(estructura):
    """Genera el .xlsx del archivo bancario replicando la estructura del modelo."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ARCHIVO"
    if estructura["tipo"] == "Bancolombia":
        ws.append(estructura["header_cols"])
        ws.append(estructura["header_vals"])
        ws.append(estructura["detalle_cols"])
        for fila in estructura["detalle"]:
            ws.append(fila)
    else:
        ws.append(estructura["detalle_cols"])
        for fila in estructura["detalle"]:
            ws.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def errores_xlsx_bytes(errores):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Inconsistencias"
    ws.append(["Cedula", "Nombre", "Empresa", "Motivo"])
    for e in errores:
        ws.append([e.get("cedula", ""), e.get("nombre", ""), e.get("empresa", ""), e.get("motivo", "")])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def guardar_ejecucion(con, proc, usuario="demo"):
    """Guarda la ejecucion, incrementa consecutivos Bancolombia y persiste cada archivo."""
    cur = con.execute(
        "INSERT INTO ap_ejecucion (fecha, usuario, fecha_aplicacion, total_empleados, total_valor,"
        " n_archivos, n_errores) VALUES (?,?,?,?,?,?,?)",
        (now(), usuario, str(proc["fecha_aplicacion"]), proc["total_empleados"],
         proc["total_valor"], len(proc["grupos"]), len(proc["errores"])))
    eid = cur.lastrowid
    archivos = []
    for (emp_nom, grupo), filas in proc["grupos"].items():
        emp = filas[0]["empresa_obj"]
        est = construir_filas(con, emp_nom, grupo, filas, proc["fecha_aplicacion"])
        seq = est.get("secuencia")
        if grupo == "Bancolombia" and emp:
            seq = consecutivo_siguiente(con, emp["id"], "Bancolombia")
            est["header_vals"][3] = seq  # actualizar secuencia real
        fname = "{}_{}_{}.xlsx".format(grupo, _slug(emp_nom), str(proc["fecha_aplicacion"]).replace("-", ""))
        valor_total = round(sum(f["valor"] for f in filas), 2)
        con.execute(
            "INSERT INTO ap_archivo (ejecucion_id, empresa, banco, n_empleados, valor_total,"
            " secuencia, nombre_archivo, datos_json) VALUES (?,?,?,?,?,?,?,?)",
            (eid, emp_nom, grupo, len(filas), valor_total, seq if seq else None, fname,
             json.dumps(est, default=str)))
        archivos.append({"empresa": emp_nom, "banco": grupo, "n": len(filas),
                         "valor": valor_total, "nombre": fname, "estructura": est})
    con.commit()
    return eid, archivos


def ejecuciones(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM ap_ejecucion ORDER BY id DESC").fetchall()]


def archivos_de(con, eid):
    rows = con.execute("SELECT * FROM ap_archivo WHERE ejecucion_id=? ORDER BY empresa, banco",
                       (eid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["estructura"] = json.loads(r["datos_json"])
        except Exception:
            d["estructura"] = None
        out.append(d)
    return out


def dashboard_planos(con):
    ej = ejecuciones(con)
    tot_emp = sum(e["total_empleados"] or 0 for e in ej)
    tot_val = round(sum(e["total_valor"] or 0 for e in ej), 2)
    n_arch = sum(e["n_archivos"] or 0 for e in ej)
    n_err = sum(e["n_errores"] or 0 for e in ej)
    # resumen por empresa+banco (acumulado)
    res = con.execute(
        "SELECT empresa, banco, SUM(n_empleados) emp, SUM(valor_total) val "
        "FROM ap_archivo GROUP BY empresa, banco ORDER BY val DESC").fetchall()
    return {"tot_emp": tot_emp, "tot_val": tot_val, "n_arch": n_arch, "n_err": n_err,
            "resumen": [dict(r) for r in res]}
