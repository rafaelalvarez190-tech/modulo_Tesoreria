"""
core.py - Logica de negocio del Modulo de Tesoreria Integral (Grupo Supre).
Sin dependencias de Streamlit: maneja la base de datos SQLite, la carga/validacion
de archivos, la deduplicacion (upsert), pagos, estados y los datos del dashboard.
"""
import os
import io
import csv
import hashlib
import sqlite3
import datetime as dt

try:
    import pandas as pd
    HAS_PANDAS = True
except Exception:
    HAS_PANDAS = False

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tesoreria.db")

EXPECTED_COLS = [
    "fecha_actualizacion", "empresa", "periodo", "codigo_cuenta", "nombre_cuenta",
    "identificacion", "proveedor", "factura", "fecha_vencimiento", "dias_por_vencer",
    "treinta_dias", "sesenta_dias", "noventa_dias", "mas_de_noventa",
    "saldo_actual", "saldo_anterior", "debitos", "creditos",
]
REQUIRED_COLS = ["empresa", "identificacion", "proveedor", "factura", "saldo_actual"]

ESTADOS = ["Pendiente", "En proceso", "Abonada parcialmente", "Pagada", "Anulada"]

TRANSICIONES = {
    "Pendiente": ["En proceso", "Abonada parcialmente", "Pagada", "Anulada"],
    "En proceso": ["Pendiente", "Abonada parcialmente", "Pagada", "Anulada"],
    "Abonada parcialmente": ["Abonada parcialmente", "Pagada", "Anulada"],
    "Pagada": ["Abonada parcialmente"],
    "Anulada": ["Pendiente"],
}

MEDIOS_PAGO = ["Transferencia", "Caja", "Efectivo", "PSE", "Debito automatico"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS factura (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    llave_unica TEXT UNIQUE NOT NULL,
    empresa TEXT, periodo TEXT, codigo_cuenta TEXT, nombre_cuenta TEXT,
    identificacion TEXT, proveedor TEXT, numero_factura TEXT,
    fecha_vencimiento TEXT,
    valor_original REAL DEFAULT 0,
    saldo_contable REAL DEFAULT 0,
    total_abonado REAL DEFAULT 0,
    estado TEXT DEFAULT 'Pendiente',
    fecha_estimada_pago TEXT,
    notas TEXT,
    hash_fila TEXT,
    fecha_primera_carga TEXT,
    fecha_ultima_actualizacion TEXT,
    activo INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pago (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factura_id INTEGER NOT NULL,
    fecha_pago TEXT, empresa TEXT, banco TEXT, cuenta_bancaria TEXT,
    medio_pago TEXT, numero_comprobante TEXT, valor_pagado REAL,
    notas TEXT, usuario TEXT, created_at TEXT, anulado INTEGER DEFAULT 0,
    FOREIGN KEY (factura_id) REFERENCES factura(id)
);
CREATE TABLE IF NOT EXISTS carga_archivo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre_archivo TEXT, fecha TEXT, usuario TEXT,
    total_leidas INTEGER, nuevas INTEGER, actualizadas INTEGER,
    sin_cambios INTEGER, rechazadas INTEGER
);
CREATE TABLE IF NOT EXISTS error_carga (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    carga_id INTEGER, fila INTEGER, motivo TEXT, dato TEXT
);
CREATE TABLE IF NOT EXISTS historial_cambio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factura_id INTEGER, campo TEXT, valor_anterior TEXT, valor_nuevo TEXT,
    usuario TEXT, fecha TEXT, motivo TEXT
);
CREATE TABLE IF NOT EXISTS anticipo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa TEXT, identificacion TEXT, proveedor TEXT,
    fecha TEXT, valor REAL, origen TEXT, numero_comprobante TEXT,
    usuario TEXT, created_at TEXT, anulado INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS anulacion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comprobante TEXT, empresa TEXT, identificacion TEXT, proveedor TEXT,
    tipo TEXT, valor_facturas REAL, valor_anticipo REAL,
    n_pagos INTEGER, n_anticipos INTEGER,
    fecha TEXT, usuario TEXT, motivo TEXT
);
"""


# --------------------------------------------------------------------------
def get_conn(db_path=DB_PATH):
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _ensure_column(con, table, col, decl):
    cols = [r[1] for r in con.execute("PRAGMA table_info(" + table + ")").fetchall()]
    if col not in cols:
        con.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + decl)


def init_db(con):
    con.executescript(SCHEMA)
    _ensure_column(con, "pago", "anulado", "INTEGER DEFAULT 0")
    _ensure_column(con, "anticipo", "anulado", "INTEGER DEFAULT 0")
    con.commit()


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return dt.date.today()


def norm(v):
    return str(v if v is not None else "").strip().upper()


def llave(empresa, nit, factura):
    return norm(empresa) + "|" + norm(nit) + "|" + norm(factura)


def to_float(v):
    if v is None:
        return 0.0
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return 0.0
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.replace("$", "").replace("(", "").replace(")", "").replace(" ", "")
    s = s.lstrip("-")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if len(s.split(",")[-1]) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        # solo puntos: separador de miles si hay varios o si el ultimo grupo es de 3 digitos
        if s.count(".") > 1 or len(s.split(".")[-1]) == 3:
            s = s.replace(".", "")
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if neg else val


def row_hash(d):
    base = "|".join(str(d.get(c, "")) for c in
                     ["saldo_actual", "fecha_vencimiento", "periodo",
                      "treinta_dias", "sesenta_dias", "noventa_dias", "mas_de_noventa"])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def parse_bytes(filename, raw):
    """Devuelve (rows, cols, error)."""
    name = (filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xls")):
            if not HAS_PANDAS:
                return None, [], "Para leer Excel se requiere pandas y openpyxl."
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
            df.columns = [str(c).strip().lower() for c in df.columns]
            return df.fillna("").to_dict(orient="records"), list(df.columns), None
        text = None
        for enc in ("utf-8-sig", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return None, [], "No se pudo decodificar el archivo."
        sample = text[:4096]
        delim = ";" if sample.count(";") > sample.count(",") else ","
        if sample.count("\t") > max(sample.count(";"), sample.count(",")):
            delim = "\t"
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        reader.fieldnames = cols
        return [dict(r) for r in reader], cols, None
    except Exception as e:
        return None, [], "Error leyendo el archivo: " + str(e)


def aging_bucket(fecha_venc, saldo):
    if saldo <= 0:
        return "Pagada"
    if not fecha_venc:
        return "Sin fecha"
    fv = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            fv = dt.datetime.strptime(str(fecha_venc)[:10], fmt).date()
            break
        except ValueError:
            continue
    if fv is None:
        return "Sin fecha"
    dias = (today() - fv).days
    if dias < 0:
        return "Corriente"
    if dias <= 30:
        return "1-30"
    if dias <= 60:
        return "31-60"
    if dias <= 90:
        return "61-90"
    return ">90"


def factura_dict(r):
    d = dict(r)
    d["saldo_tesoreria"] = round((d.get("valor_original") or 0) - (d.get("total_abonado") or 0), 2)
    d["diferencia"] = round((d.get("saldo_contable") or 0) - d["saldo_tesoreria"], 2)
    d["cubeta"] = aging_bucket(d.get("fecha_vencimiento"), d["saldo_tesoreria"])
    d["vencida"] = d["cubeta"] in ("1-30", "31-60", "61-90", ">90")
    return d


def log_cambio(con, factura_id, campo, antes, despues, usuario, motivo=""):
    con.execute(
        "INSERT INTO historial_cambio (factura_id, campo, valor_anterior, valor_nuevo, usuario, fecha, motivo)"
        " VALUES (?,?,?,?,?,?,?)",
        (factura_id, campo, str(antes), str(despues), usuario, now(), motivo))


# --------------------------------------------------------------------------
def procesar_carga(con, filename, raw, usuario="demo"):
    """Procesa un archivo. Devuelve (resumen_dict, errores_list) o lanza ValueError."""
    rows, cols, err = parse_bytes(filename, raw)
    if err:
        raise ValueError(err)
    faltantes = [c for c in REQUIRED_COLS if c not in cols]
    if faltantes:
        raise ValueError("Faltan columnas obligatorias: " + ", ".join(faltantes)
                         + ". Encontradas: " + ", ".join(cols))

    cur = con.execute(
        "INSERT INTO carga_archivo (nombre_archivo, fecha, usuario, total_leidas, nuevas,"
        " actualizadas, sin_cambios, rechazadas) VALUES (?,?,?,?,?,?,?,?)",
        (filename, now(), usuario, len(rows), 0, 0, 0, 0))
    carga_id = cur.lastrowid

    nuevas = actualizadas = sin_cambios = rechazadas = 0
    errores = []
    vistas = set()

    for i, r in enumerate(rows, start=2):
        empresa = (r.get("empresa") or "").strip()
        nit = (r.get("identificacion") or "").strip()
        numfac = (r.get("factura") or "").strip()
        if not (empresa and nit and numfac):
            rechazadas += 1
            errores.append((i, "Faltan empresa/identificacion/factura", str(r)[:200]))
            continue
        lk = llave(empresa, nit, numfac)
        if lk in vistas:
            rechazadas += 1
            errores.append((i, "Duplicado dentro del mismo archivo", lk))
            continue
        vistas.add(lk)
        # La CxP suele exportarse en negativo desde el ERP: usamos el valor absoluto.
        saldo = abs(to_float(r.get("saldo_actual")))
        h = row_hash(r)
        fv = (r.get("fecha_vencimiento") or "").strip()
        periodo = (r.get("periodo") or "").strip()
        existente = con.execute("SELECT * FROM factura WHERE llave_unica=?", (lk,)).fetchone()
        if existente is None:
            con.execute(
                "INSERT INTO factura (llave_unica, empresa, periodo, codigo_cuenta, nombre_cuenta,"
                " identificacion, proveedor, numero_factura, fecha_vencimiento, valor_original,"
                " saldo_contable, total_abonado, estado, hash_fila, fecha_primera_carga,"
                " fecha_ultima_actualizacion, activo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (lk, empresa, periodo, (r.get("codigo_cuenta") or "").strip(),
                 (r.get("nombre_cuenta") or "").strip(), nit, (r.get("proveedor") or "").strip(),
                 numfac, fv, saldo, saldo, 0, "Pendiente", h, now(), now()))
            nuevas += 1
        elif existente["hash_fila"] == h:
            sin_cambios += 1
        else:
            if abs((existente["saldo_contable"] or 0) - saldo) > 0.001:
                log_cambio(con, existente["id"], "saldo_contable",
                           existente["saldo_contable"], saldo, usuario, "Carga de archivo")
            con.execute(
                "UPDATE factura SET saldo_contable=?, fecha_vencimiento=?, periodo=?,"
                " hash_fila=?, fecha_ultima_actualizacion=? WHERE id=?",
                (saldo, fv, periodo, h, now(), existente["id"]))
            actualizadas += 1

    for (fila, motivo, dato) in errores:
        con.execute("INSERT INTO error_carga (carga_id, fila, motivo, dato) VALUES (?,?,?,?)",
                    (carga_id, fila, motivo, dato))
    con.execute("UPDATE carga_archivo SET nuevas=?, actualizadas=?, sin_cambios=?, rechazadas=? WHERE id=?",
                (nuevas, actualizadas, sin_cambios, rechazadas, carga_id))
    con.commit()
    resumen = dict(carga_id=carga_id, leidas=len(rows), nuevas=nuevas,
                   actualizadas=actualizadas, sin_cambios=sin_cambios, rechazadas=rechazadas)
    return resumen, errores


def list_facturas(con, q="", estado="", empresa="", solo_vencidas=False):
    sql = "SELECT * FROM factura WHERE activo=1"
    p = []
    if q:
        sql += " AND (proveedor LIKE ? OR numero_factura LIKE ? OR identificacion LIKE ?)"
        p += ["%" + q + "%", "%" + q + "%", "%" + q + "%"]
    if estado:
        sql += " AND estado=?"
        p.append(estado)
    if empresa:
        sql += " AND empresa=?"
        p.append(empresa)
    sql += " ORDER BY fecha_vencimiento"
    items = [factura_dict(r) for r in con.execute(sql, p).fetchall()]
    if solo_vencidas:
        items = [f for f in items if f["vencida"] and f["estado"] != "Anulada"]
    return items


def get_factura(con, fid):
    r = con.execute("SELECT * FROM factura WHERE id=?", (fid,)).fetchone()
    return factura_dict(r) if r else None


def empresas_distintas(con):
    return [r["empresa"] for r in con.execute(
        "SELECT DISTINCT empresa FROM factura ORDER BY empresa").fetchall()]


def pagos_de(con, fid):
    return [dict(r) for r in con.execute(
        "SELECT * FROM pago WHERE factura_id=? AND anulado=0 ORDER BY id DESC", (fid,)).fetchall()]


def historial_de(con, fid):
    return [dict(r) for r in con.execute(
        "SELECT * FROM historial_cambio WHERE factura_id=? ORDER BY id DESC", (fid,)).fetchall()]


def todos_los_pagos(con):
    return [dict(r) for r in con.execute(
        "SELECT p.*, f.numero_factura, f.proveedor FROM pago p "
        "JOIN factura f ON f.id=p.factura_id ORDER BY p.id DESC").fetchall()]


def movimientos_pago(con):
    """Vista unificada de pagos de caja: pagos a facturas, montos que se fueron a anticipo
    (excedentes) y aplicaciones de anticipo (cruces). El total de caja suma pagos a factura
    y excedentes a anticipo (los cruces son aplicacion de anticipo ya pagado, no caja nueva)."""
    rows = []
    for p in con.execute(
            "SELECT p.*, f.numero_factura, f.proveedor FROM pago p JOIN factura f ON f.id=p.factura_id "
            "WHERE p.anulado=0 ORDER BY p.id DESC").fetchall():
        es_cruce = (p["medio_pago"] == "Cruce de anticipo")
        rows.append(dict(
            fecha=p["fecha_pago"], empresa=p["empresa"], proveedor=p["proveedor"],
            referencia="Factura " + str(p["numero_factura"]), medio=p["medio_pago"],
            comprobante=p["numero_comprobante"], valor=p["valor_pagado"], usuario=p["usuario"],
            tipo="Aplicacion de anticipo" if es_cruce else "Pago a factura",
            es_caja=(not es_cruce)))
    for a in con.execute(
            "SELECT * FROM anticipo WHERE anulado=0 AND valor > 0 AND origen LIKE 'Excedente%' "
            "ORDER BY id DESC").fetchall():
        rows.append(dict(
            fecha=a["fecha"], empresa=a["empresa"], proveedor=a["proveedor"],
            referencia=a["origen"], medio="-", comprobante=a["numero_comprobante"],
            valor=a["valor"], usuario=a["usuario"], tipo="A anticipo", es_caja=True))
    # completar proveedor de las filas de pago
    rows.sort(key=lambda x: str(x["fecha"]), reverse=True)
    return rows


def total_caja_pagada(con):
    pago = con.execute(
        "SELECT COALESCE(SUM(valor_pagado),0) v FROM pago "
        "WHERE medio_pago <> 'Cruce de anticipo' AND anulado=0"
    ).fetchone()["v"]
    antic = con.execute(
        "SELECT COALESCE(SUM(valor),0) v FROM anticipo "
        "WHERE valor > 0 AND origen LIKE 'Excedente%' AND anulado=0"
    ).fetchone()["v"]
    return round(pago + antic, 2)


def registrar_pago(con, fid, datos, usuario="demo"):
    r = con.execute("SELECT * FROM factura WHERE id=?", (fid,)).fetchone()
    if not r:
        return False, "Factura no encontrada."
    f = factura_dict(r)
    if f["estado"] == "Anulada":
        return False, "No se puede pagar una factura anulada (RN-09)."
    valor = to_float(datos.get("valor_pagado"))
    if valor <= 0:
        return False, "El valor del pago debe ser mayor a cero."
    for k in ("fecha_pago", "medio_pago", "numero_comprobante"):
        if not str(datos.get(k) or "").strip():
            return False, "Faltan datos obligatorios del pago (fecha, medio de pago, comprobante)."
    # El excedente sobre el saldo de la factura se lleva a anticipo del proveedor.
    aplicado = round(min(valor, f["saldo_tesoreria"]), 2)
    excedente = round(valor - aplicado, 2)
    if aplicado > 0:
        con.execute(
            "INSERT INTO pago (factura_id, fecha_pago, empresa, banco, cuenta_bancaria, medio_pago,"
            " numero_comprobante, valor_pagado, notas, usuario, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, str(datos.get("fecha_pago")), r["empresa"], datos.get("banco"),
             datos.get("cuenta_bancaria"), datos.get("medio_pago"),
             datos.get("numero_comprobante"), aplicado, datos.get("notas", ""), usuario, now()))
        nuevo_abonado = round((r["total_abonado"] or 0) + aplicado, 2)
        saldo_tes = round((r["valor_original"] or 0) - nuevo_abonado, 2)
        nuevo_estado = "Pagada" if saldo_tes <= 0.001 else "Abonada parcialmente"
        if nuevo_estado != r["estado"]:
            log_cambio(con, fid, "estado", r["estado"], nuevo_estado, usuario, "Aplicacion de pago")
        con.execute("UPDATE factura SET total_abonado=?, estado=? WHERE id=?",
                    (nuevo_abonado, nuevo_estado, fid))
    else:
        saldo_tes = f["saldo_tesoreria"]
    if excedente > 0:
        registrar_anticipo(con, r["empresa"], r["identificacion"], r["proveedor"], excedente,
                           "Excedente de pago factura " + str(r["numero_factura"]),
                           datos.get("numero_comprobante", ""), usuario,
                           fecha=str(datos.get("fecha_pago") or today().isoformat()))
    con.commit()
    msg = "Pago registrado por {:,.0f}. Saldo de tesoreria: {:,.0f}.".format(aplicado, max(saldo_tes, 0))
    if excedente > 0:
        msg += " Excedente {:,.0f} llevado a anticipo del proveedor.".format(excedente)
    return True, msg


# --------------------------------------------------------------------------
# Abono a nivel de proveedor (distribucion por antiguedad)
# --------------------------------------------------------------------------
def proveedores_con_saldo(con, empresa=""):
    """Devuelve [{nit, proveedor, n, saldo}] de proveedores con saldo pendiente."""
    facs = list_facturas(con, empresa=empresa)
    agg = {}
    for f in facs:
        if f["estado"] == "Anulada" or f["saldo_tesoreria"] <= 0:
            continue
        k = f["identificacion"]
        if k not in agg:
            agg[k] = {"nit": k, "proveedor": f["proveedor"], "n": 0, "saldo": 0.0, "vencido": 0.0}
        agg[k]["n"] += 1
        agg[k]["saldo"] = round(agg[k]["saldo"] + f["saldo_tesoreria"], 2)
        if f["vencida"]:
            agg[k]["vencido"] = round(agg[k]["vencido"] + f["saldo_tesoreria"], 2)
    return sorted(agg.values(), key=lambda x: -x["saldo"])


def _orden_antiguedad(f):
    """Clave de orden: vencimiento mas antiguo primero (= mas vencida primero)."""
    fv = str(f.get("fecha_vencimiento") or "")[:10]
    if not fv:
        return ("9999-99-99", f["id"])
    return (fv, f["id"])


def facturas_pagables_proveedor(con, nit, empresa=""):
    """Facturas del proveedor con saldo > 0, ordenadas de la mas vencida a la menos vencida."""
    facs = [f for f in list_facturas(con, empresa=empresa)
            if f["identificacion"] == nit and f["estado"] != "Anulada" and f["saldo_tesoreria"] > 0]
    return sorted(facs, key=_orden_antiguedad)


def distribuir_abono(facturas, monto):
    """Reparte 'monto' entre 'facturas' (ya ordenadas) sin exceder el saldo de cada una
    ni el saldo total. Devuelve (plan, aplicado, remanente, saldo_total).
    plan = lista de (factura_dict, abono)."""
    rem = round(float(monto), 2)
    plan = []
    saldo_total = round(sum(f["saldo_tesoreria"] for f in facturas), 2)
    for f in facturas:
        if rem <= 0:
            break
        ap = round(min(rem, f["saldo_tesoreria"]), 2)
        if ap > 0:
            plan.append((f, ap))
            rem = round(rem - ap, 2)
    aplicado = round(float(monto) - rem, 2)
    return plan, aplicado, round(rem, 2), saldo_total


def abono_por_proveedor(con, nit, monto, datos, empresa="", usuario="demo"):
    """Aplica un abono al proveedor repartiendolo entre sus facturas mas vencidas primero,
    sin exceder el saldo total. Devuelve (ok, mensaje, resumen)."""
    monto = to_float(monto)
    if monto <= 0:
        return False, "El monto a abonar debe ser mayor a cero.", None
    for k in ("fecha_pago", "banco", "cuenta_bancaria", "medio_pago", "numero_comprobante"):
        if not str(datos.get(k) or "").strip():
            return False, "Faltan datos obligatorios del pago (fecha, banco, cuenta, medio, comprobante).", None
    facturas = facturas_pagables_proveedor(con, nit, empresa)
    if not facturas:
        return False, "El proveedor no tiene facturas con saldo pendiente.", None
    plan, aplicado, remanente, saldo_total = distribuir_abono(facturas, monto)
    detalle = []
    for f, ap in plan:
        d = dict(datos)
        d["valor_pagado"] = ap
        d["notas"] = (datos.get("notas") or "") + " [Abono por proveedor]"
        ok, m = registrar_pago(con, f["id"], d, usuario)
        if ok:
            detalle.append({"factura": f["numero_factura"], "vencimiento": f["fecha_vencimiento"],
                            "cubeta": f["cubeta"], "abono": ap})
    if remanente > 0:
        prov_name = facturas[0]["proveedor"]
        emp = empresa or facturas[0]["empresa"]
        registrar_anticipo(con, emp, nit, prov_name, remanente,
                           "Excedente de abono por proveedor",
                           datos.get("numero_comprobante", ""), usuario,
                           fecha=str(datos.get("fecha_pago") or today().isoformat()))
    resumen = dict(n=len(detalle), aplicado=round(aplicado, 2), remanente=round(remanente, 2),
                   anticipo=round(remanente, 2) if remanente > 0 else 0,
                   saldo_total=saldo_total, detalle=detalle)
    msg = "Abono aplicado a {} factura(s) por {:,.0f}.".format(len(detalle), aplicado)
    if remanente > 0:
        msg += " Excedente {:,.0f} llevado a anticipo del proveedor.".format(remanente)
    return True, msg, resumen


def registrar_anticipo(con, empresa, nit, proveedor, valor, origen, comprobante="", usuario="demo", fecha=None):
    con.execute(
        "INSERT INTO anticipo (empresa, identificacion, proveedor, fecha, valor, origen,"
        " numero_comprobante, usuario, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (empresa, nit, proveedor, (fecha or today().isoformat()), round(float(valor), 2),
         origen, comprobante, usuario, now()))
    con.commit()


def saldo_anticipo_proveedor(con, nit):
    r = con.execute("SELECT COALESCE(SUM(valor),0) v FROM anticipo WHERE identificacion=? AND anulado=0",
                    (nit,)).fetchone()
    return round(r["v"], 2)


def anticipos_resumen(con, empresa=""):
    """Saldo de anticipo agrupado por proveedor."""
    sql = ("SELECT identificacion, MAX(proveedor) proveedor, SUM(valor) saldo, COUNT(*) movimientos "
           "FROM anticipo WHERE anulado=0")
    p = []
    if empresa:
        sql += " AND empresa=?"
        p.append(empresa)
    sql += " GROUP BY identificacion HAVING SUM(valor) <> 0 ORDER BY saldo DESC"
    return [dict(nit=r["identificacion"], proveedor=r["proveedor"],
                 saldo=round(r["saldo"], 2), movimientos=r["movimientos"])
            for r in con.execute(sql, p).fetchall()]


def anticipos_movimientos(con, nit=None):
    sql = "SELECT * FROM anticipo WHERE anulado=0"
    p = []
    if nit:
        sql += " AND identificacion=?"
        p.append(nit)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in con.execute(sql, p).fetchall()]


def cruzar_anticipo(con, nit, monto, empresa="", usuario="demo"):
    """Cruza (aplica) el saldo de anticipo disponible del proveedor contra sus facturas,
    de la mas vencida a la menos vencida. Registra pagos tipo 'Cruce de anticipo' y
    descarga el anticipo con un movimiento negativo. Devuelve (ok, mensaje, resumen)."""
    disponible = saldo_anticipo_proveedor(con, nit)
    if disponible <= 0:
        return False, "El proveedor no tiene saldo de anticipo disponible.", None
    monto = to_float(monto)
    if monto <= 0:
        monto = disponible  # por defecto, cruzar todo el disponible
    if monto > disponible + 0.001:
        return False, ("El monto a cruzar ({:,.0f}) no puede ser mayor al anticipo disponible "
                       "({:,.0f}).".format(monto, disponible)), None
    facturas = facturas_pagables_proveedor(con, nit, empresa)
    if not facturas:
        return False, "El proveedor no tiene facturas con saldo para cruzar.", None
    saldo_total = round(sum(f["saldo_tesoreria"] for f in facturas), 2)
    cruce = round(min(monto, disponible, saldo_total), 2)
    if cruce <= 0:
        return False, "No hay monto disponible para cruzar.", None
    ref = "CRUCE-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    plan, aplicado, _, _ = distribuir_abono(facturas, cruce)
    detalle = []
    for f, ap in plan:
        datos = dict(fecha_pago=today().isoformat(), banco="Anticipo", cuenta_bancaria="-",
                     medio_pago="Cruce de anticipo", numero_comprobante=ref,
                     notas="Cruce de anticipo", valor_pagado=ap)
        ok, m = registrar_pago(con, f["id"], datos, usuario)
        if ok:
            detalle.append({"factura": f["numero_factura"], "vencimiento": f["fecha_vencimiento"],
                            "cubeta": f["cubeta"], "cruce": ap})
    prov_name = facturas[0]["proveedor"]
    emp = empresa or facturas[0]["empresa"]
    # movimiento negativo que descarga el anticipo
    registrar_anticipo(con, emp, nit, prov_name, -round(aplicado, 2),
                       "Cruce de anticipo con facturas", ref, usuario)
    resumen = dict(n=len(detalle), cruzado=round(aplicado, 2),
                   anticipo_restante=round(disponible - aplicado, 2), detalle=detalle)
    msg = "Cruce aplicado: {:,.0f} a {} factura(s). Anticipo restante: {:,.0f}.".format(
        aplicado, len(detalle), disponible - aplicado)
    return True, msg, resumen


def comprobantes_resumen(con):
    """Agrupa los movimientos activos por numero de comprobante (un comprobante = una operacion)."""
    grupos = {}
    for p in con.execute(
            "SELECT p.*, f.numero_factura, f.proveedor, f.identificacion FROM pago p "
            "JOIN factura f ON f.id=p.factura_id WHERE p.anulado=0 ORDER BY p.id DESC").fetchall():
        k = p["numero_comprobante"]
        g = grupos.setdefault(k, dict(comprobante=k, fecha=p["fecha_pago"], empresa=p["empresa"],
                                      proveedor=p["proveedor"], nit=p["identificacion"],
                                      n_facturas=0, valor_facturas=0.0, valor_anticipo=0.0,
                                      es_cruce=False))
        g["n_facturas"] += 1
        g["valor_facturas"] = round(g["valor_facturas"] + (p["valor_pagado"] or 0), 2)
        if p["medio_pago"] == "Cruce de anticipo":
            g["es_cruce"] = True
    for a in con.execute(
            "SELECT * FROM anticipo WHERE anulado=0 AND valor > 0 AND origen LIKE 'Excedente%' "
            "ORDER BY id DESC").fetchall():
        k = a["numero_comprobante"]
        g = grupos.setdefault(k, dict(comprobante=k, fecha=a["fecha"], empresa=a["empresa"],
                                      proveedor=a["proveedor"], nit=a["identificacion"],
                                      n_facturas=0, valor_facturas=0.0, valor_anticipo=0.0,
                                      es_cruce=False))
        g["valor_anticipo"] = round(g["valor_anticipo"] + a["valor"], 2)
    out = []
    for g in grupos.values():
        g["total"] = round((0 if g["es_cruce"] else g["valor_facturas"]) + g["valor_anticipo"], 2)
        out.append(g)
    out.sort(key=lambda x: str(x["fecha"]), reverse=True)
    return out


def anular_comprobante(con, comprobante, usuario="demo", motivo="Anulacion de pago"):
    """Anula todos los movimientos (pagos y anticipos) de un comprobante y restablece los
    saldos de cartera y de anticipo del proveedor."""
    pagos = con.execute("SELECT * FROM pago WHERE numero_comprobante=? AND anulado=0",
                        (comprobante,)).fetchall()
    antics = con.execute("SELECT * FROM anticipo WHERE numero_comprobante=? AND anulado=0",
                         (comprobante,)).fetchall()
    if not pagos and not antics:
        return False, "No hay movimientos activos con ese comprobante."
    # datos resumen para el historico de anulaciones
    es_cruce = any(p["medio_pago"] == "Cruce de anticipo" for p in pagos)
    val_fact = round(sum((p["valor_pagado"] or 0) for p in pagos), 2)
    val_ant = round(sum((a["valor"] or 0) for a in antics if (a["valor"] or 0) > 0), 2)
    empresa = identificacion = proveedor = ""
    if pagos:
        fr = con.execute("SELECT empresa, identificacion, proveedor FROM factura WHERE id=?",
                         (pagos[0]["factura_id"],)).fetchone()
        if fr:
            empresa, identificacion, proveedor = fr["empresa"], fr["identificacion"], fr["proveedor"]
    elif antics:
        empresa, identificacion, proveedor = antics[0]["empresa"], antics[0]["identificacion"], antics[0]["proveedor"]
    con.execute(
        "INSERT INTO anulacion (comprobante, empresa, identificacion, proveedor, tipo,"
        " valor_facturas, valor_anticipo, n_pagos, n_anticipos, fecha, usuario, motivo)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (comprobante, empresa, identificacion, proveedor,
         "Cruce de anticipo" if es_cruce else "Pago", val_fact, val_ant,
         len(pagos), len(antics), now(), usuario, motivo))
    for p in pagos:
        r = con.execute("SELECT * FROM factura WHERE id=?", (p["factura_id"],)).fetchone()
        if r:
            nuevo_ab = round((r["total_abonado"] or 0) - (p["valor_pagado"] or 0), 2)
            if nuevo_ab < 0:
                nuevo_ab = 0.0
            saldo_tes = round((r["valor_original"] or 0) - nuevo_ab, 2)
            if saldo_tes <= 0.001:
                nuevo_estado = "Pagada"
            elif nuevo_ab > 0:
                nuevo_estado = "Abonada parcialmente"
            else:
                nuevo_estado = "Pendiente"
            con.execute("UPDATE factura SET total_abonado=?, estado=? WHERE id=?",
                        (nuevo_ab, nuevo_estado, p["factura_id"]))
            log_cambio(con, p["factura_id"], "pago_anulado", p["valor_pagado"], 0, usuario,
                       motivo + " (comprobante " + str(comprobante) + ")")
        con.execute("UPDATE pago SET anulado=1 WHERE id=?", (p["id"],))
    for a in antics:
        con.execute("UPDATE anticipo SET anulado=1 WHERE id=?", (a["id"],))
    con.commit()
    return True, ("Comprobante {} anulado: {} pago(s) y {} mov. de anticipo revertidos. "
                  "Saldos restablecidos.".format(comprobante, len(pagos), len(antics)))


def anulaciones_historico(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM anulacion ORDER BY id DESC").fetchall()]


def cambiar_estado(con, fid, nuevo, motivo="", usuario="demo"):
    r = con.execute("SELECT * FROM factura WHERE id=?", (fid,)).fetchone()
    if not r:
        return False, "Factura no encontrada."
    actual = r["estado"]
    if nuevo not in TRANSICIONES.get(actual, []):
        return False, "Transicion no permitida: {} -> {}.".format(actual, nuevo)
    if nuevo == "Anulada" and not motivo.strip():
        return False, "Anular requiere un motivo."
    log_cambio(con, fid, "estado", actual, nuevo, usuario, motivo or "Cambio manual")
    con.execute("UPDATE factura SET estado=? WHERE id=?", (nuevo, fid))
    con.commit()
    return True, "Estado cambiado a {}.".format(nuevo)


def editar_factura(con, fid, fecha_estimada_pago, notas, usuario="demo"):
    r = con.execute("SELECT * FROM factura WHERE id=?", (fid,)).fetchone()
    if not r:
        return False, "Factura no encontrada."
    if (r["fecha_estimada_pago"] or "") != (fecha_estimada_pago or ""):
        log_cambio(con, fid, "fecha_estimada_pago", r["fecha_estimada_pago"],
                   fecha_estimada_pago, usuario, "Edicion manual")
    con.execute("UPDATE factura SET fecha_estimada_pago=?, notas=? WHERE id=?",
                (fecha_estimada_pago, notas, fid))
    con.commit()
    return True, "Factura actualizada."


def errores_de_carga(con, carga_id):
    return [dict(r) for r in con.execute(
        "SELECT * FROM error_carga WHERE carga_id=? ORDER BY fila", (carga_id,)).fetchall()]


def cargas_recientes(con, limit=20):
    return [dict(r) for r in con.execute(
        "SELECT * FROM carga_archivo ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def reset_db(con):
    for t in ("pago", "anticipo", "anulacion", "error_carga", "historial_cambio", "carga_archivo", "factura"):
        con.execute("DELETE FROM " + t)
    con.commit()


def dashboard_data(con):
    facturas = list_facturas(con)
    total_pagado = con.execute(
        "SELECT COALESCE(SUM(valor_pagado),0) v FROM pago WHERE anulado=0").fetchone()["v"]
    activas = [f for f in facturas if f["estado"] != "Anulada"]
    # Total cuentas por pagar = total bruto de la obligacion (valor original).
    total_cxp = sum(f["valor_original"] for f in activas)
    # Total pendiente = total cuentas por pagar - total pagado.
    total_pendiente = round(total_cxp - total_pagado, 2)
    # Anticipo disponible (saldo a favor neto de proveedores).
    total_anticipo = con.execute("SELECT COALESCE(SUM(valor),0) v FROM anticipo WHERE anulado=0").fetchone()["v"]
    vencidas = [f for f in activas if f["vencida"]]
    por_vencer = [f for f in facturas
                  if f["cubeta"] == "Corriente" and f["estado"] not in ("Pagada", "Anulada")]
    buckets = ["Corriente", "1-30", "31-60", "61-90", ">90"]
    aging = {b: 0.0 for b in buckets}
    for f in activas:
        if f["cubeta"] in aging:
            aging[f["cubeta"]] += f["saldo_tesoreria"]
    estados_count = {e: 0 for e in ESTADOS}
    for f in facturas:
        estados_count[f["estado"]] = estados_count.get(f["estado"], 0) + 1
    flujo_map = {}
    for r in con.execute(
            "SELECT substr(fecha_pago,1,10) d, SUM(valor_pagado) v FROM pago "
            "WHERE anulado=0 AND medio_pago <> 'Cruce de anticipo' "
            "GROUP BY substr(fecha_pago,1,10)").fetchall():
        flujo_map[r["d"]] = flujo_map.get(r["d"], 0) + (r["v"] or 0)
    for r in con.execute(
            "SELECT substr(fecha,1,10) d, SUM(valor) v FROM anticipo "
            "WHERE anulado=0 AND valor > 0 AND origen LIKE 'Excedente%' "
            "GROUP BY substr(fecha,1,10)").fetchall():
        flujo_map[r["d"]] = flujo_map.get(r["d"], 0) + (r["v"] or 0)
    flujo = [(d, round(flujo_map[d], 2)) for d in sorted(flujo_map)]
    empresas = {}
    for f in activas:
        empresas[f["empresa"]] = round(empresas.get(f["empresa"], 0) + f["saldo_tesoreria"], 2)
    kpis = dict(
        total_cxp=round(total_cxp, 2), total_pagado=round(total_pagado, 2),
        total_anticipo=round(total_anticipo, 2), total_pendiente=round(total_pendiente, 2),
        n_facturas=len(activas), n_vencidas=len(vencidas),
        monto_vencido=round(sum(f["saldo_tesoreria"] for f in vencidas), 2),
        n_por_vencer=len(por_vencer),
        monto_por_vencer=round(sum(f["saldo_tesoreria"] for f in por_vencer), 2))
    return dict(kpis=kpis, aging={k: round(v, 2) for k, v in aging.items()},
                estados=estados_count, flujo=flujo,
                empresas={k: v for k, v in sorted(empresas.items(), key=lambda x: -x[1])})
