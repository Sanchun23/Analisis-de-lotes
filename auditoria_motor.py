"""
Motor local y reproducible de auditoría de datos para el caso:
Comercio Electrónico Jacaranda S. A. — Ventas y devoluciones digitales.

Principios de diseño:
1. Solo lectura: nunca modifica los archivos CSV originales.
2. Dos etapas estrictamente separadas:
   - Etapa A: calidad, integridad referencial y aptitud por regla.
   - Etapa B: ejecución de reglas empresariales (R-D01 a R-D05) solo si la
     Etapa A determina aptitud suficiente.
3. Sin juicios de fraude confirmado: toda alerta es un indicio explicable
   que requiere corroboración documental externa.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

import pandas as pd

# ---------------------------------------------------------------------------
# Esquema oficial (respeta exactamente nombres y tipos de los cuatro CSV)
# ---------------------------------------------------------------------------

UMBRAL_AUTORIZACION_CRC = 120_000
ESTADOS_DEVOLUCION_PAGADA = {"PAGADA", "PAGADO"}
FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"

ESQUEMA_TABLAS: Dict[str, Dict[str, Any]] = {
    "clientes": {
        "archivo_esperado": "clientes.csv",
        "pk": "cliente_id",
        "columnas": {
            "cliente_id": "str",
            "nombre_ficticio": "str",
            "segmento": "str",
            "cuenta_reembolso": "str",
            "estado": "str",
            "registrado_en": "datetime",
        },
        "obligatorias": [
            "cliente_id",
            "nombre_ficticio",
            "segmento",
            "cuenta_reembolso",
            "estado",
            "registrado_en",
        ],
    },
    "ventas": {
        "archivo_esperado": "ventas.csv",
        "pk": "venta_id",
        "columnas": {
            "venta_id": "str",
            "cliente_id": "str",
            "vendido_en": "datetime",
            "monto_total_crc": "int",
            "estado": "str",
            "canal": "str",
        },
        "obligatorias": [
            "venta_id",
            "cliente_id",
            "vendido_en",
            "monto_total_crc",
            "estado",
            "canal",
        ],
    },
    "devoluciones": {
        "archivo_esperado": "devoluciones.csv",
        "pk": "devolucion_id",
        "columnas": {
            "devolucion_id": "str",
            "venta_id": "str",
            "cliente_id": "str",
            "monto_crc": "int",
            "solicitado_en": "datetime",
            "procesado_en": "datetime",
            "ejecutor_id": "str",
            "cuenta_destino": "str",
            "motivo": "str",
            "estado": "str",
        },
        "obligatorias": [
            "devolucion_id",
            "venta_id",
            "cliente_id",
            "monto_crc",
            "solicitado_en",
            "procesado_en",
            "ejecutor_id",
            "cuenta_destino",
            "motivo",
            "estado",
        ],
    },
    "autorizaciones_devolucion": {
        "archivo_esperado": "autorizaciones_devolucion.csv",
        "pk": "autorizacion_id",
        "columnas": {
            "autorizacion_id": "str",
            "devolucion_id": "str",
            "aprobador_id": "str",
            "aprobado_en": "datetime",
            "estado": "str",
        },
        "obligatorias": [
            "autorizacion_id",
            "devolucion_id",
            "aprobador_id",
            "aprobado_en",
            "estado",
        ],
    },
}

REQUISITOS_POR_REGLA: Dict[str, Dict[str, Any]] = {
    "R-D01": {
        "nombre": "Integridad y titularidad venta-cliente en reembolsos pagados",
        "tablas": {
            "devoluciones": ["devolucion_id", "venta_id", "cliente_id", "estado", "monto_crc"],
            "ventas": ["venta_id", "cliente_id"],
            "clientes": ["cliente_id"],
        },
        "pks_unicas_requeridas": [("ventas", "venta_id"), ("clientes", "cliente_id")],
    },
    "R-D02": {
        "nombre": "Techo acumulado de reembolsos pagados por venta",
        "tablas": {
            "devoluciones": ["devolucion_id", "venta_id", "monto_crc", "estado"],
            "ventas": ["venta_id", "monto_total_crc"],
        },
        "pks_unicas_requeridas": [("devoluciones", "devolucion_id"), ("ventas", "venta_id")],
    },
    "R-D03": {
        "nombre": "Autorización previa y segregación de funciones (>= 120.000 CRC)",
        "tablas": {
            "devoluciones": [
                "devolucion_id",
                "monto_crc",
                "procesado_en",
                "ejecutor_id",
                "estado",
            ],
            "autorizaciones_devolucion": [
                "autorizacion_id",
                "devolucion_id",
                "aprobador_id",
                "aprobado_en",
                "estado",
            ],
        },
        "pks_unicas_requeridas": [
            ("devoluciones", "devolucion_id"),
            ("autorizaciones_devolucion", "autorizacion_id"),
        ],
    },
    "R-D04": {
        "nombre": "Consistencia entre cuenta destino y cuenta registrada del cliente",
        "tablas": {
            "devoluciones": ["devolucion_id", "cliente_id", "cuenta_destino", "estado", "monto_crc"],
            "clientes": ["cliente_id", "cuenta_reembolso"],
        },
        "pks_unicas_requeridas": [("clientes", "cliente_id")],
    },
    "R-D05": {
        "nombre": "Sustento en venta con estado ENTREGADA",
        "tablas": {
            "devoluciones": ["devolucion_id", "venta_id", "estado", "monto_crc"],
            "ventas": ["venta_id", "estado", "vendido_en"],
        },
        "pks_unicas_requeridas": [("ventas", "venta_id")],
    },
}

NOTA_METODOLOGICA = (
    "AVISO METODOLÓGICO: Las alertas emitidas por esta herramienta son señales técnicas "
    "basadas exclusivamente en los cuatro archivos CSV cargados. Ninguna alerta constituye "
    "'fraude confirmado' ni prueba de dolo sin corroboración documental adicional "
    "(estados bancarios, guías de transporte, devoluciones físicas, bitácoras de usuarios y cuentas)."
)


# ---------------------------------------------------------------------------
# Utilidades de lectura segura (solo memoria) y normalización
# ---------------------------------------------------------------------------

def _norm_str(val: Any) -> Optional[str]:
    if pd.isna(val):
        return None
    s = str(val).strip()
    return s.upper() if s != "" else None


def _alnum_str(val: Any) -> Optional[str]:
    s = _norm_str(val)
    if s is None:
        return None
    return re.sub(r"[^A-Z0-9]", "", s)


def _parse_int_estricto(serie: pd.Series) -> Tuple[pd.Series, pd.Series]:
    limpio = serie.astype(str).str.strip()
    es_nulo = serie.isna() | (limpio == "") | (limpio.str.lower() == "nan")
    es_entero_regex = limpio.str.match(r"^[+-]?\d+$", na=False)
    invalido = (~es_nulo) & (~es_entero_regex)
    numerico = pd.to_numeric(limpio.where(es_entero_regex), errors="coerce").astype("Int64")
    return numerico, invalido


def _parse_datetime_estricto(serie: pd.Series) -> Tuple[pd.Series, pd.Series]:
    limpio = serie.astype(str).str.strip()
    es_nulo = serie.isna() | (limpio == "") | (limpio.str.lower() == "nan")
    parsed = pd.to_datetime(limpio.where(~es_nulo), format=FORMATO_FECHA, errors="coerce")
    invalido = (~es_nulo) & parsed.isna()
    return parsed, invalido


def cargar_csv_solo_lectura(
    fuente: Union[str, bytes, BinaryIO],
    nombre_tabla: str,
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "tabla": nombre_tabla,
        "cargado": False,
        "error": None,
        "filas": 0,
        "columnas_detectadas": [],
        "codificacion": None,
        "sha256": None,
    }
    try:
        if isinstance(fuente, str):
            with open(fuente, "rb") as f:
                raw = f.read()
        elif isinstance(fuente, bytes):
            raw = fuente
        else:
            pos = fuente.tell() if hasattr(fuente, "tell") else None
            raw = fuente.read()
            if pos is not None and hasattr(fuente, "seek"):
                fuente.seek(pos)

        if not raw or len(raw.strip()) == 0:
            meta["error"] = "El archivo está vacío (0 bytes útiles)."
            return None, meta

        meta["sha256"] = hashlib.sha256(raw).hexdigest()

        df = None
        last_err = None
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                df = pd.read_csv(
                    io.BytesIO(raw),
                    dtype=str,
                    keep_default_na=True,
                    na_values=["", "NULL", "null", "None", "NaN", "nan"],
                    encoding=enc,
                )
                meta["codificacion"] = enc
                break
            except UnicodeDecodeError as exc:
                last_err = exc
            except pd.errors.EmptyDataError:
                meta["error"] = "El archivo no contiene encabezados ni filas."
                return None, meta
            except pd.errors.ParserError as exc:
                meta["error"] = f"Error de sintaxis CSV (verifique separador ',' y comillas): {exc}"
                return None, meta

        if df is None:
            meta["error"] = f"No fue posible decodificar el archivo: {last_err}"
            return None, meta

        if len(df.columns) == 1 and ";" in str(df.columns[0]):
            meta["error"] = (
                "Se detectó una sola columna con separador ';'. "
                "El formato esperado de los CSV del caso utiliza coma (',')."
            )
            return None, meta

        meta["cargado"] = True
        meta["filas"] = int(len(df))
        meta["columnas_detectadas"] = [str(c) for c in df.columns]
        return df, meta

    except FileNotFoundError:
        meta["error"] = "Archivo no encontrado en la ruta especificada."
        return None, meta
    except Exception as exc:
        meta["error"] = f"Error inesperado al leer el archivo: {exc}"
        return None, meta


# ---------------------------------------------------------------------------
# Estructuras de resultado
# ---------------------------------------------------------------------------

@dataclass
class ResultadoAuditoria:
    nombre_lote: str
    ejecutado_en: str
    metadatos_carga: Dict[str, Dict[str, Any]]
    etapa_a_resumen_tablas: pd.DataFrame
    etapa_a_hallazgos: pd.DataFrame
    etapa_a_aptitud: pd.DataFrame
    etapa_b_resumen_reglas: pd.DataFrame
    etapa_b_alertas: pd.DataFrame
    etapa_b_senales_exploratorias: pd.DataFrame
    limitaciones_declaradas: List[str] = field(default_factory=list)
    tablas_consulta: Dict[str, Optional[pd.DataFrame]] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Etapa A: Calidad, integridad referencial y aptitud por regla
# ---------------------------------------------------------------------------

def _ejecutar_etapa_a(
    tablas_raw: Dict[str, Optional[pd.DataFrame]],
    metas: Dict[str, Dict[str, Any]],
) -> Tuple[
    Dict[str, Optional[pd.DataFrame]],
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    Dict[str, Dict[str, Any]],
    List[str],
]:
    hallazgos: List[Dict[str, Any]] = []
    resumen_tablas_rows: List[Dict[str, Any]] = []
    tablas_tipadas: Dict[str, Optional[pd.DataFrame]] = {}
    estado_tablas: Dict[str, Dict[str, Any]] = {}
    limitaciones: List[str] = []

    def add_hallazgo(
        tabla: str,
        tipo: str,
        severidad: str,
        campo: str,
        registro_id: str,
        cantidad: int,
        detalle: str,
    ) -> None:
        hallazgos.append(
            {
                "etapa": "A_CALIDAD",
                "tabla": tabla,
                "tipo_control": tipo,
                "severidad": severidad,
                "campo": campo,
                "registro_id": registro_id,
                "cantidad_afectada": int(cantidad),
                "detalle": detalle,
            }
        )

    for nombre, spec in ESQUEMA_TABLAS.items():
        df_raw = tablas_raw.get(nombre)
        meta = metas[nombre]
        pk = spec["pk"]
        info = {
            "cargada": False,
            "filas": 0,
            "cols_faltantes": [],
            "cols_extra": [],
            "pk_duplicada": False,
            "errores_tipo_criticos": {},
            "nulos_por_columna": {},
        }

        if df_raw is None or not meta["cargado"]:
            add_hallazgo(
                nombre,
                "CARGA_ARCHIVO",
                "CRITICO",
                "*",
                "-",
                0,
                f"No se pudo cargar {spec['archivo_esperado']}: {meta.get('error', 'sin detalle')}",
            )
            resumen_tablas_rows.append(
                {
                    "tabla": nombre,
                    "archivo": spec["archivo_esperado"],
                    "cargada": False,
                    "filas": 0,
                    "columnas_faltantes": len(spec["columnas"]),
                    "pk_duplicadas": None,
                    "errores_tipo": None,
                    "sha256_corto": "-",
                }
            )
            tablas_tipadas[nombre] = None
            estado_tablas[nombre] = info
            continue

        info["cargada"] = True
        info["filas"] = len(df_raw)

        cols_esperadas = list(spec["columnas"].keys())
        cols_presentes = list(df_raw.columns)
        cols_faltantes = [c for c in cols_esperadas if c not in cols_presentes]
        cols_extra = [c for c in cols_presentes if c not in cols_esperadas]
        info["cols_faltantes"] = cols_faltantes
        info["cols_extra"] = cols_extra

        if cols_faltantes:
            add_hallazgo(
                nombre,
                "ESQUEMA_COLUMNAS",
                "CRITICO",
                ", ".join(cols_faltantes),
                "-",
                len(cols_faltantes),
                f"Faltan columnas requeridas por el esquema exacto: {cols_faltantes}.",
            )
        if cols_extra:
            add_hallazgo(
                nombre,
                "ESQUEMA_COLUMNAS",
                "INFO",
                ", ".join(cols_extra),
                "-",
                len(cols_extra),
                f"Se detectaron columnas adicionales no previstas en el esquema: {cols_extra}.",
            )

        df = df_raw.copy()
        df["_fila_csv"] = range(2, len(df) + 2)

        errores_tipo_total = 0

        for col, tipo_esperado in spec["columnas"].items():
            if col not in df.columns:
                continue

            serie_raw = df[col]
            es_vacio = serie_raw.isna() | (serie_raw.astype(str).str.strip() == "")
            n_nulos = int(es_vacio.sum())
            info["nulos_por_columna"][col] = n_nulos
            if n_nulos > 0:
                ejemplos = (
                    df.loc[es_vacio, pk].fillna("(PK nula)").astype(str).head(5).tolist()
                    if pk in df.columns
                    else df.loc[es_vacio, "_fila_csv"].astype(str).head(5).tolist()
                )
                add_hallazgo(
                    nombre,
                    "COMPLETITUD_NULOS",
                    "ADVERTENCIA",
                    col,
                    ", ".join(ejemplos),
                    n_nulos,
                    f"{n_nulos} registro(s) presentan valor nulo o vacío en '{col}'.",
                )

            if tipo_esperado == "int":
                num, inv = _parse_int_estricto(serie_raw)
                df[f"{col}__num"] = num
                df[f"{col}__invalido"] = inv
                n_inv = int(inv.sum())
                if n_inv > 0:
                    errores_tipo_total += n_inv
                    info["errores_tipo_criticos"][col] = n_inv
                    ej = (
                        df.loc[inv, pk].fillna("(PK nula)").astype(str).head(5).tolist()
                        if pk in df.columns
                        else df.loc[inv, "_fila_csv"].astype(str).head(5).tolist()
                    )
                    add_hallazgo(
                        nombre,
                        "TIPO_DATO_ENTERO",
                        "CRITICO" if (len(df) > 0 and n_inv == len(df)) else "ADVERTENCIA",
                        col,
                        ", ".join(ej),
                        n_inv,
                        f"{n_inv} registro(s) no cumplen formato entero exacto en '{col}'.",
                    )
                no_pos = num.notna() & (num <= 0)
                if int(no_pos.sum()) > 0:
                    ej = (
                        df.loc[no_pos, pk].fillna("(PK nula)").astype(str).head(5).tolist()
                        if pk in df.columns
                        else df.loc[no_pos, "_fila_csv"].astype(str).head(5).tolist()
                    )
                    add_hallazgo(
                        nombre,
                        "DOMINIO_MONTO",
                        "ADVERTENCIA",
                        col,
                        ", ".join(ej),
                        int(no_pos.sum()),
                        f"{int(no_pos.sum())} registro(s) tienen monto <= 0 en '{col}'.",
                    )

            elif tipo_esperado == "datetime":
                dt, inv = _parse_datetime_estricto(serie_raw)
                df[f"{col}__dt"] = dt
                df[f"{col}__invalido"] = inv
                n_inv = int(inv.sum())
                if n_inv > 0:
                    errores_tipo_total += n_inv
                    info["errores_tipo_criticos"][col] = n_inv
                    ej = (
                        df.loc[inv, pk].fillna("(PK nula)").astype(str).head(5).tolist()
                        if pk in df.columns
                        else df.loc[inv, "_fila_csv"].astype(str).head(5).tolist()
                    )
                    add_hallazgo(
                        nombre,
                        "TIPO_DATO_FECHA",
                        "CRITICO" if (len(df) > 0 and n_inv == len(df)) else "ADVERTENCIA",
                        col,
                        ", ".join(ej),
                        n_inv,
                        f"{n_inv} registro(s) no cumplen formato '{FORMATO_FECHA}' en '{col}'.",
                    )

            elif tipo_esperado == "str":
                df[f"{col}__norm"] = serie_raw.map(_norm_str)

        n_dup_pk = 0
        if pk in df.columns:
            norm_pk = df[f"{pk}__norm"]
            dup_mask = norm_pk.notna() & norm_pk.duplicated(keep=False)
            n_dup_pk = int(dup_mask.sum())
            if n_dup_pk > 0:
                info["pk_duplicada"] = True
                ids_dup = sorted(norm_pk[dup_mask].unique().tolist())
                add_hallazgo(
                    nombre,
                    "UNICIDAD_PK",
                    "CRITICO",
                    pk,
                    ", ".join(ids_dup[:10]),
                    n_dup_pk,
                    (
                        f"Clave primaria '{pk}' duplicada en {n_dup_pk} filas "
                        f"({len(ids_dup)} clave(s) repetida(s)). Riesgo de duplicación artificial al cruzar tablas."
                    ),
                )

        if nombre == "autorizaciones_devolucion" and "devolucion_id__norm" in df.columns:
            dup_dev = df["devolucion_id__norm"].notna() & df["devolucion_id__norm"].duplicated(keep=False)
            if int(dup_dev.sum()) > 0:
                ids_mult = sorted(df.loc[dup_dev, "devolucion_id__norm"].unique().tolist())
                add_hallazgo(
                    nombre,
                    "CARDINALIDAD_1_A_N",
                    "INFO",
                    "devolucion_id",
                    ", ".join(ids_mult[:10]),
                    int(dup_dev.sum()),
                    (
                        f"Se detectaron múltiples autorizaciones para {len(ids_mult)} devolucion_id. "
                        "La Etapa B evaluará por devolución sin duplicar montos."
                    ),
                )

        if nombre == "devoluciones" and "estado__norm" in df.columns:
            estados_unicos = sorted(df["estado__norm"].dropna().unique().tolist())
            otros = [e for e in estados_unicos if e not in ESTADOS_DEVOLUCION_PAGADA]
            if otros:
                add_hallazgo(
                    nombre,
                    "DOMINIO_ESTADO",
                    "INFO",
                    "estado",
                    "-",
                    int(df["estado__norm"].isin(otros).sum()),
                    f"Existen devoluciones con estado distinto de PAGADA/PAGADO: {otros}.",
                )

        resumen_tablas_rows.append(
            {
                "tabla": nombre,
                "archivo": spec["archivo_esperado"],
                "cargada": True,
                "filas": len(df),
                "columnas_faltantes": len(cols_faltantes),
                "pk_duplicadas": n_dup_pk,
                "errores_tipo": errores_tipo_total,
                "sha256_corto": (meta["sha256"] or "")[:12],
            }
        )
        tablas_tipadas[nombre] = df
        estado_tablas[nombre] = info

    df_cli = tablas_tipadas.get("clientes")
    df_ven = tablas_tipadas.get("ventas")
    df_dev = tablas_tipadas.get("devoluciones")
    df_aut = tablas_tipadas.get("autorizaciones_devolucion")

    if (
        df_ven is not None
        and df_cli is not None
        and "cliente_id__norm" in df_ven.columns
        and "cliente_id__norm" in df_cli.columns
    ):
        cli_set = set(df_cli["cliente_id__norm"].dropna())
        huerf_vc = df_ven["cliente_id__norm"].notna() & (~df_ven["cliente_id__norm"].isin(cli_set))
        if int(huerf_vc.sum()) > 0:
            ids = df_ven.loc[huerf_vc, "venta_id"].fillna("(sin id)").head(10).tolist()
            add_hallazgo(
                "ventas",
                "INTEGRIDAD_REFERENCIAL",
                "ADVERTENCIA",
                "cliente_id -> clientes.cliente_id",
                ", ".join(ids),
                int(huerf_vc.sum()),
                f"{int(huerf_vc.sum())} venta(s) referencian un cliente_id inexistente en clientes.csv.",
            )

    if (
        df_dev is not None
        and df_ven is not None
        and "venta_id__norm" in df_dev.columns
        and "venta_id__norm" in df_ven.columns
    ):
        ven_set = set(df_ven["venta_id__norm"].dropna())
        huerf_dv = df_dev["venta_id__norm"].notna() & (~df_dev["venta_id__norm"].isin(ven_set))
        if int(huerf_dv.sum()) > 0:
            ids = df_dev.loc[huerf_dv, "devolucion_id"].fillna("(sin id)").head(10).tolist()
            add_hallazgo(
                "devoluciones",
                "INTEGRIDAD_REFERENCIAL",
                "ADVERTENCIA",
                "venta_id -> ventas.venta_id",
                ", ".join(ids),
                int(huerf_dv.sum()),
                f"{int(huerf_dv.sum())} devolución(es) referencian una venta_id ausente en ventas.csv.",
            )

    if (
        df_dev is not None
        and df_cli is not None
        and "cliente_id__norm" in df_dev.columns
        and "cliente_id__norm" in df_cli.columns
    ):
        cli_set = set(df_cli["cliente_id__norm"].dropna())
        huerf_dc = df_dev["cliente_id__norm"].notna() & (~df_dev["cliente_id__norm"].isin(cli_set))
        if int(huerf_dc.sum()) > 0:
            ids = df_dev.loc[huerf_dc, "devolucion_id"].fillna("(sin id)").head(10).tolist()
            add_hallazgo(
                "devoluciones",
                "INTEGRIDAD_REFERENCIAL",
                "ADVERTENCIA",
                "cliente_id -> clientes.cliente_id",
                ", ".join(ids),
                int(huerf_dc.sum()),
                f"{int(huerf_dc.sum())} devolución(es) referencian un cliente_id ausente en clientes.csv.",
            )

    if (
        df_aut is not None
        and df_dev is not None
        and "devolucion_id__norm" in df_aut.columns
        and "devolucion_id__norm" in df_dev.columns
    ):
        dev_set = set(df_dev["devolucion_id__norm"].dropna())
        huerf_ad = df_aut["devolucion_id__norm"].notna() & (~df_aut["devolucion_id__norm"].isin(dev_set))
        if int(huerf_ad.sum()) > 0:
            ids = df_aut.loc[huerf_ad, "autorizacion_id"].fillna("(sin id)").head(10).tolist()
            add_hallazgo(
                "autorizaciones_devolucion",
                "INTEGRIDAD_REFERENCIAL",
                "ADVERTENCIA",
                "devolucion_id -> devoluciones.devolucion_id",
                ", ".join(ids),
                int(huerf_ad.sum()),
                f"{int(huerf_ad.sum())} autorización(es) huérfana(s) sin devolucion_id en devoluciones.csv.",
            )

    if df_dev is not None and {"solicitado_en__dt", "procesado_en__dt"}.issubset(df_dev.columns):
        m_cron = (
            df_dev["solicitado_en__dt"].notna()
            & df_dev["procesado_en__dt"].notna()
            & (df_dev["procesado_en__dt"] < df_dev["solicitado_en__dt"])
        )
        if int(m_cron.sum()) > 0:
            ids = df_dev.loc[m_cron, "devolucion_id"].fillna("(sin id)").head(10).tolist()
            add_hallazgo(
                "devoluciones",
                "CRONOLOGIA_TECNICA",
                "ADVERTENCIA",
                "procesado_en < solicitado_en",
                ", ".join(ids),
                int(m_cron.sum()),
                f"{int(m_cron.sum())} devolución(es) registran procesado_en anterior a solicitado_en.",
            )

    if (
        df_dev is not None
        and df_ven is not None
        and not estado_tablas["ventas"]["pk_duplicada"]
        and {"venta_id__norm", "solicitado_en__dt"}.issubset(df_dev.columns)
        and {"venta_id__norm", "vendido_en__dt"}.issubset(df_ven.columns)
    ):
        m_dv = df_dev[["devolucion_id", "venta_id__norm", "solicitado_en__dt"]].merge(
            df_ven[["venta_id__norm", "vendido_en__dt"]],
            on="venta_id__norm",
            how="inner",
        )
        m_pre = (
            m_dv["solicitado_en__dt"].notna()
            & m_dv["vendido_en__dt"].notna()
            & (m_dv["solicitado_en__dt"] < m_dv["vendido_en__dt"])
        )
        if int(m_pre.sum()) > 0:
            ids = m_dv.loc[m_pre, "devolucion_id"].fillna("(sin id)").head(10).tolist()
            add_hallazgo(
                "devoluciones",
                "CRONOLOGIA_TECNICA",
                "ADVERTENCIA",
                "solicitado_en < vendido_en",
                ", ".join(ids),
                int(m_pre.sum()),
                f"{int(m_pre.sum())} devolución(es) tienen solicitado_en anterior a ventas.vendido_en.",
            )

    if (
        df_aut is not None
        and df_dev is not None
        and not estado_tablas["devoluciones"]["pk_duplicada"]
        and {"devolucion_id__norm", "aprobado_en__dt"}.issubset(df_aut.columns)
        and {"devolucion_id__norm", "solicitado_en__dt"}.issubset(df_dev.columns)
    ):
        m_ad = df_aut[["autorizacion_id", "devolucion_id", "devolucion_id__norm", "aprobado_en__dt"]].merge(
            df_dev[["devolucion_id__norm", "solicitado_en__dt"]],
            on="devolucion_id__norm",
            how="inner",
        )
        m_pre_ap = (
            m_ad["aprobado_en__dt"].notna()
            & m_ad["solicitado_en__dt"].notna()
            & (m_ad["aprobado_en__dt"] < m_ad["solicitado_en__dt"])
        )
        if int(m_pre_ap.sum()) > 0:
            for _, row in m_ad.loc[m_pre_ap].iterrows():
                diff_min = round(
                    (row["solicitado_en__dt"] - row["aprobado_en__dt"]).total_seconds() / 60.0,
                    2,
                )
                add_hallazgo(
                    "autorizaciones_devolucion",
                    "CRONOLOGIA_TECNICA",
                    "ADVERTENCIA",
                    "aprobado_en < solicitado_en",
                    f"{row['autorizacion_id']} / {row['devolucion_id']}",
                    1,
                    (
                        f"aprobado_en ({row['aprobado_en__dt']}) precede a solicitado_en "
                        f"({row['solicitado_en__dt']}) por {diff_min} min. Posible desfase de relojes o registro diferido."
                    ),
                )

    aptitud_rows: List[Dict[str, Any]] = []
    for regla_id, req in REQUISITOS_POR_REGLA.items():
        bloqueos: List[str] = []
        observaciones: List[str] = []

        for tabla_req, cols_req in req["tablas"].items():
            st = estado_tablas[tabla_req]
            if not st["cargada"]:
                bloqueos.append(f"Tabla '{tabla_req}' no cargada o con error de lectura.")
                continue
            falt = [c for c in cols_req if c in st["cols_faltantes"]]
            if falt:
                bloqueos.append(f"Faltan columnas en '{tabla_req}': {falt}.")
            if tabla_req == "devoluciones" and st["filas"] == 0:
                bloqueos.append("Tabla 'devoluciones' sin registros (0 filas).")
            for c in cols_req:
                n_err = st["errores_tipo_criticos"].get(c, 0)
                if n_err > 0:
                    if st["filas"] > 0 and n_err == st["filas"]:
                        bloqueos.append(f"Columna '{tabla_req}.{c}' tiene errores de tipo en todas las filas.")
                    else:
                        observaciones.append(
                            f"{n_err} fila(s) con tipo inválido en '{tabla_req}.{c}' serán marcadas como no evaluables."
                        )
                n_nul = st["nulos_por_columna"].get(c, 0)
                if n_nul > 0:
                    observaciones.append(
                        f"{n_nul} fila(s) con valor nulo en '{tabla_req}.{c}'."
                    )

        for tabla_pk, col_pk in req["pks_unicas_requeridas"]:
            st = estado_tablas[tabla_pk]
            if st["cargada"] and st["pk_duplicada"]:
                bloqueos.append(
                    f"Clave primaria duplicada en '{tabla_pk}.{col_pk}': impide uniones/agregaciones sin inflación artificial."
                )

        if bloqueos:
            estado_apt = "NO_APTA"
            ejecutable = False
            motivo_apt = " | ".join(bloqueos)
            limitaciones.append(f"{regla_id} omitida en Etapa B por datos insuficientes en Etapa A: {motivo_apt}")
        elif observaciones:
            estado_apt = "APTA_CON_OBSERVACIONES"
            ejecutable = True
            motivo_apt = " | ".join(observaciones)
        else:
            estado_apt = "APTA"
            ejecutable = True
            motivo_apt = "Estructura, tipos y claves verificados para ejecución fiable."

        aptitud_rows.append(
            {
                "regla_id": regla_id,
                "nombre_regla": req["nombre"],
                "estado_aptitud": estado_apt,
                "habilitada_etapa_b": ejecutable,
                "justificacion_aptitud": motivo_apt,
            }
        )

    df_resumen_tablas = pd.DataFrame(resumen_tablas_rows)
    df_hallazgos = pd.DataFrame(
        hallazgos,
        columns=[
            "etapa",
            "tabla",
            "tipo_control",
            "severidad",
            "campo",
            "registro_id",
            "cantidad_afectada",
            "detalle",
        ],
    )
    df_aptitud = pd.DataFrame(aptitud_rows)
    return tablas_tipadas, df_resumen_tablas, df_hallazgos, df_aptitud, estado_tablas, limitaciones


# ---------------------------------------------------------------------------
# Etapa B: Reglas empresariales (R-D01 a R-D05) solo si Etapa A habilita
# ---------------------------------------------------------------------------

def _ejecutar_etapa_b(
    tablas: Dict[str, Optional[pd.DataFrame]],
    df_aptitud: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    alertas: List[Dict[str, Any]] = []
    senales_exp: List[Dict[str, Any]] = []
    resumen_reglas: List[Dict[str, Any]] = []

    apt_map = df_aptitud.set_index("regla_id").to_dict(orient="index")

    def add_alerta(
        regla_id: str,
        categoria: str,
        subtipo: str,
        devolucion_id: str,
        venta_id: str,
        cliente_id: str,
        autorizacion_id: str,
        monto_crc: Optional[int],
        justificacion: str,
    ) -> None:
        alertas.append(
            {
                "regla_id": regla_id,
                "categoria": categoria,
                "subtipo": subtipo,
                "devolucion_id": devolucion_id,
                "venta_id": venta_id,
                "cliente_id": cliente_id,
                "autorizacion_id": autorizacion_id,
                "monto_crc": monto_crc,
                "justificacion": justificacion,
                "estado_investigacion": "INDICIO_REQUIERE_CORROBORACION",
            }
        )

    df_cli = tablas.get("clientes")
    df_ven = tablas.get("ventas")
    df_dev = tablas.get("devoluciones")
    df_aut = tablas.get("autorizaciones_devolucion")

    def get_dev_pagadas() -> pd.DataFrame:
        assert df_dev is not None
        return df_dev[df_dev["estado__norm"].isin(ESTADOS_DEVOLUCION_PAGADA)].copy()

    # ---------------------- R-D01 ----------------------
    r_id = "R-D01"
    if not apt_map[r_id]["habilitada_etapa_b"]:
        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "NO_EJECUTADA_DATOS_INSUFICIENTES",
                "evaluados": 0,
                "conformes": 0,
                "alertas_empresariales": 0,
                "no_evaluables_calidad": 0,
                "monto_en_alerta_crc": 0,
            }
        )
    else:
        assert df_ven is not None and df_cli is not None
        d_pag = get_dev_pagadas()
        cli_set = set(df_cli["cliente_id__norm"].dropna())
        ven_map = df_ven.set_index("venta_id__norm")[["cliente_id__norm", "estado__norm"]].to_dict(orient="index")

        eval_cnt = len(d_pag)
        alerta_cnt = 0
        no_eval_cnt = 0
        monto_alerta = 0

        for _, row in d_pag.iterrows():
            dev_id = str(row["devolucion_id"]) if pd.notna(row["devolucion_id"]) else f"FILA_{row['_fila_csv']}"
            v_norm = row["venta_id__norm"]
            c_norm = row["cliente_id__norm"]
            monto = int(row["monto_crc__num"]) if pd.notna(row["monto_crc__num"]) else None

            if v_norm is None or c_norm is None:
                no_eval_cnt += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "CLAVE_NULA",
                    dev_id,
                    str(row["venta_id"]),
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    "venta_id o cliente_id nulo en devoluciones.csv; requiere corrección de calidad antes de evaluar D-01.",
                )
                continue

            fallos = []
            ven_info = ven_map.get(v_norm)
            if ven_info is None:
                fallos.append(f"venta_id '{v_norm}' no existe en ventas.csv")
            if c_norm not in cli_set:
                fallos.append(f"cliente_id '{c_norm}' no existe en clientes.csv")
            if ven_info is not None and ven_info["cliente_id__norm"] != c_norm:
                fallos.append(
                    f"cliente_id de devolución ('{c_norm}') no coincide con cliente_id de la venta ('{ven_info['cliente_id__norm']}')"
                )

            if fallos:
                alerta_cnt += 1
                monto_alerta += monto or 0
                add_alerta(
                    r_id,
                    "ALERTA_EMPRESARIAL",
                    "INTEGRIDAD_VENTA_CLIENTE",
                    dev_id,
                    v_norm,
                    c_norm,
                    "-",
                    monto,
                    "Incumplimiento D-01: " + "; ".join(fallos) + ".",
                )

        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "EJECUTADA",
                "evaluados": eval_cnt,
                "conformes": eval_cnt - alerta_cnt - no_eval_cnt,
                "alertas_empresariales": alerta_cnt,
                "no_evaluables_calidad": no_eval_cnt,
                "monto_en_alerta_crc": monto_alerta,
            }
        )

    # ---------------------- R-D02 ----------------------
    r_id = "R-D02"
    if not apt_map[r_id]["habilitada_etapa_b"]:
        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "NO_EJECUTADA_DATOS_INSUFICIENTES",
                "evaluados": 0,
                "conformes": 0,
                "alertas_empresariales": 0,
                "no_evaluables_calidad": 0,
                "monto_en_alerta_crc": 0,
            }
        )
    else:
        assert df_ven is not None
        d_pag = get_dev_pagadas()
        valid_d = d_pag[
            d_pag["venta_id__norm"].notna()
            & d_pag["monto_crc__num"].notna()
            & (~d_pag["monto_crc__invalido"])
            & (d_pag["monto_crc__num"] > 0)
        ]
        invalid_d = d_pag.loc[~d_pag.index.isin(valid_d.index)]

        no_eval_cnt = 0
        for _, row in invalid_d.iterrows():
            no_eval_cnt += 1
            add_alerta(
                r_id,
                "NO_EVALUABLE_CALIDAD",
                "MONTO_O_VENTA_INVALIDO",
                str(row["devolucion_id"]),
                str(row["venta_id"]),
                str(row["cliente_id"]),
                "-",
                None,
                "Registro excluido de la suma acumulada D-02 por venta_id nulo o monto_crc inválido/<=0.",
            )

        agg = (
            valid_d.groupby("venta_id__norm", as_index=False)
            .agg(
                total_reembolsado_crc=("monto_crc__num", "sum"),
                cantidad_devoluciones=("devolucion_id", "count"),
                devoluciones_ids=("devolucion_id", lambda s: ", ".join(sorted(s.astype(str)))),
                cliente_ids=("cliente_id", lambda s: ", ".join(sorted(set(s.dropna().astype(str))))),
            )
        )
        m2 = agg.merge(
            df_ven[["venta_id__norm", "cliente_id", "monto_total_crc__num", "monto_total_crc__invalido"]],
            on="venta_id__norm",
            how="left",
        )

        eval_cnt = len(m2)
        alerta_cnt = 0
        no_eval_ventas = 0
        monto_alerta = 0

        for _, row in m2.iterrows():
            v_id = row["venta_id__norm"]
            tot_rem = int(row["total_reembolsado_crc"])
            m_ven = row["monto_total_crc__num"]
            inval_ven = bool(row["monto_total_crc__invalido"]) if pd.notna(row["monto_total_crc__invalido"]) else False

            if pd.isna(m_ven) or inval_ven or int(m_ven) <= 0:
                no_eval_cnt += 1
                no_eval_ventas += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "VENTA_SIN_MONTO_VALIDO",
                    row["devoluciones_ids"],
                    v_id,
                    row["cliente_ids"],
                    "-",
                    tot_rem,
                    f"No es posible evaluar techo D-02: la venta '{v_id}' no existe o carece de monto_total_crc válido.",
                )
                continue

            m_ven_int = int(m_ven)
            if tot_rem > m_ven_int:
                exceso = tot_rem - m_ven_int
                alerta_cnt += 1
                monto_alerta += exceso
                add_alerta(
                    r_id,
                    "ALERTA_EMPRESARIAL",
                    "EXCESO_REEMBOLSO_ACUMULADO",
                    row["devoluciones_ids"],
                    v_id,
                    str(row["cliente_id"]),
                    "-",
                    exceso,
                    (
                        f"La suma de {int(row['cantidad_devoluciones'])} devolución(es) pagada(s) "
                        f"(CRC {tot_rem:,}) supera el importe original de la venta (CRC {m_ven_int:,}) "
                        f"en CRC {exceso:,}."
                    ),
                )

            sub_v = valid_d[valid_d["venta_id__norm"] == v_id]
            if (
                len(sub_v) > 1
                and (sub_v["monto_crc__num"] < UMBRAL_AUTORIZACION_CRC).all()
                and tot_rem >= UMBRAL_AUTORIZACION_CRC
            ):
                senales_exp.append(
                    {
                        "tipo_senal": "EXPLORATORIA_POSIBLE_FRACCIONAMIENTO",
                        "venta_id": v_id,
                        "devoluciones_ids": row["devoluciones_ids"],
                        "cantidad_devoluciones": int(len(sub_v)),
                        "suma_crc": tot_rem,
                        "detalle": (
                            f"Venta con {len(sub_v)} devoluciones individuales < CRC {UMBRAL_AUTORIZACION_CRC:,} "
                            f"cuya suma acumulada alcanza CRC {tot_rem:,}. Señal informativa de monitoreo; "
                            "no viola literal D-03 si el umbral es por devolución."
                        ),
                    }
                )

        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "EJECUTADA",
                "evaluados": eval_cnt,
                "conformes": max(0, eval_cnt - alerta_cnt - no_eval_ventas),
                "alertas_empresariales": alerta_cnt,
                "no_evaluables_calidad": no_eval_cnt,
                "monto_en_alerta_crc": monto_alerta,
            }
        )

    # ---------------------- R-D03 ----------------------
    r_id = "R-D03"
    if not apt_map[r_id]["habilitada_etapa_b"]:
        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "NO_EJECUTADA_DATOS_INSUFICIENTES",
                "evaluados": 0,
                "conformes": 0,
                "alertas_empresariales": 0,
                "no_evaluables_calidad": 0,
                "monto_en_alerta_crc": 0,
            }
        )
    else:
        assert df_dev is not None and df_aut is not None
        d_valid_m = df_dev[df_dev["monto_crc__num"].notna() & (~df_dev["monto_crc__invalido"])]
        d_inval_m = df_dev.loc[~df_dev.index.isin(d_valid_m.index)]
        d_sujetas = d_valid_m[d_valid_m["monto_crc__num"] >= UMBRAL_AUTORIZACION_CRC]

        aut_por_dev: Dict[str, List[Dict[str, Any]]] = {}
        for _, arow in df_aut.iterrows():
            k = arow["devolucion_id__norm"]
            if k is not None:
                aut_por_dev.setdefault(k, []).append(arow.to_dict())

        eval_cnt = len(d_sujetas)
        alerta_cnt = 0
        no_eval_sujetas = 0
        no_eval_cnt = 0
        monto_alerta = 0

        for _, row in d_inval_m.iterrows():
            no_eval_cnt += 1
            add_alerta(
                r_id,
                "NO_EVALUABLE_CALIDAD",
                "MONTO_INVALIDO",
                str(row["devolucion_id"]),
                str(row["venta_id"]),
                str(row["cliente_id"]),
                "-",
                None,
                "No se puede determinar si supera umbral de CRC 120,000 por monto_crc nulo o inválido.",
            )

        for _, row in d_sujetas.iterrows():
            dev_id = str(row["devolucion_id"])
            dev_norm = row["devolucion_id__norm"]
            monto = int(row["monto_crc__num"])
            proc_dt = row["procesado_en__dt"]
            ejec_norm = row["ejecutor_id__norm"]
            est_dev = row["estado__norm"] or "(NULO)"

            if pd.isna(proc_dt) or ejec_norm is None:
                no_eval_cnt += 1
                no_eval_sujetas += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "PROCESAMIENTO_O_EJECUTOR_NULO",
                    dev_id,
                    str(row["venta_id"]),
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    f"Devolución >= CRC {UMBRAL_AUTORIZACION_CRC:,} ({est_dev}) sin procesado_en válido o sin ejecutor_id; no evaluable en D-03.",
                )
                continue

            candidatos = aut_por_dev.get(dev_norm, [])
            if not candidatos:
                alerta_cnt += 1
                monto_alerta += monto
                add_alerta(
                    r_id,
                    "ALERTA_EMPRESARIAL",
                    "SIN_AUTORIZACION",
                    dev_id,
                    str(row["venta_id"]),
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    f"Devolución {est_dev} de CRC {monto:,} (>= CRC {UMBRAL_AUTORIZACION_CRC:,}) sin registro en autorizaciones_devolucion.csv.",
                )
                continue

            cumple_alguna = False
            razones_fallo: List[str] = []
            aut_ids_list: List[str] = []

            for cand in candidatos:
                aid = str(cand.get("autorizacion_id", "-"))
                aut_ids_list.append(aid)
                est_aut = cand.get("estado__norm")
                aprob_dt = cand.get("aprobado_en__dt")
                aprob_norm = cand.get("aprobador_id__norm")

                subfallos = []
                if est_aut != "APROBADA":
                    subfallos.append(f"estado='{est_aut}' (se requiere APROBADA)")
                if pd.isna(aprob_dt):
                    subfallos.append("aprobado_en nulo/inválido")
                elif not (aprob_dt < proc_dt):
                    subfallos.append(
                        f"aprobado_en ({aprob_dt}) no es anterior a procesado_en ({proc_dt})"
                    )
                if aprob_norm is None:
                    subfallos.append("aprobador_id nulo")
                elif aprob_norm == ejec_norm:
                    subfallos.append(
                        f"aprobador_id ('{aprob_norm}') coincide con ejecutor_id ('{ejec_norm}')"
                    )

                if not subfallos:
                    cumple_alguna = True
                    break
                razones_fallo.append(f"{aid}: " + ", ".join(subfallos))

            if not cumple_alguna:
                alerta_cnt += 1
                monto_alerta += monto
                add_alerta(
                    r_id,
                    "ALERTA_EMPRESARIAL",
                    "AUTORIZACION_NO_CONFORME",
                    dev_id,
                    str(row["venta_id"]),
                    str(row["cliente_id"]),
                    ", ".join(aut_ids_list),
                    monto,
                    f"Devolución {est_dev} de CRC {monto:,} no cumple D-03 -> " + " | ".join(razones_fallo) + ".",
                )

        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "EJECUTADA",
                "evaluados": eval_cnt,
                "conformes": max(0, eval_cnt - alerta_cnt - no_eval_sujetas),
                "alertas_empresariales": alerta_cnt,
                "no_evaluables_calidad": no_eval_cnt,
                "monto_en_alerta_crc": monto_alerta,
            }
        )

    # ---------------------- R-D04 ----------------------
    r_id = "R-D04"
    if not apt_map[r_id]["habilitada_etapa_b"]:
        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "NO_EJECUTADA_DATOS_INSUFICIENTES",
                "evaluados": 0,
                "conformes": 0,
                "alertas_empresariales": 0,
                "no_evaluables_calidad": 0,
                "monto_en_alerta_crc": 0,
            }
        )
    else:
        assert df_cli is not None
        d_pag = get_dev_pagadas()
        cli_cuenta = df_cli.set_index("cliente_id__norm")["cuenta_reembolso"].to_dict()
        cuenta_a_clientes: Dict[str, List[str]] = {}
        for _, crow in df_cli.iterrows():
            cn = _norm_str(crow["cuenta_reembolso"])
            cid = crow["cliente_id__norm"]
            if cn and cid:
                cuenta_a_clientes.setdefault(cn, []).append(cid)

        eval_cnt = len(d_pag)
        alerta_cnt = 0
        no_eval_cnt = 0
        monto_alerta = 0

        for _, row in d_pag.iterrows():
            dev_id = str(row["devolucion_id"])
            c_norm = row["cliente_id__norm"]
            monto = int(row["monto_crc__num"]) if pd.notna(row["monto_crc__num"]) else None
            cta_dest_raw = row["cuenta_destino"]
            cta_dest_norm = _norm_str(cta_dest_raw)

            if c_norm is None or c_norm not in cli_cuenta:
                no_eval_cnt += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "CLIENTE_INEXISTENTE_O_NULO",
                    dev_id,
                    str(row["venta_id"]),
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    "No es posible validar D-04 porque cliente_id es nulo o no existe en clientes.csv.",
                )
                continue

            cta_reg_raw = cli_cuenta[c_norm]
            cta_reg_norm = _norm_str(cta_reg_raw)

            if cta_dest_norm is None or cta_reg_norm is None:
                no_eval_cnt += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "CUENTA_NULA",
                    dev_id,
                    str(row["venta_id"]),
                    c_norm,
                    "-",
                    monto,
                    "cuenta_destino o cuenta_reembolso es nula/vacía; requiere revisión de calidad.",
                )
                continue

            if cta_dest_norm != cta_reg_norm:
                if _alnum_str(cta_dest_raw) == _alnum_str(cta_reg_raw):
                    no_eval_cnt += 1
                    add_alerta(
                        r_id,
                        "NO_EVALUABLE_CALIDAD",
                        "DIFERENCIA_SOLO_FORMATO",
                        dev_id,
                        str(row["venta_id"]),
                        c_norm,
                        "-",
                        monto,
                        f"Diferencia únicamente de formato/separadores: destino='{cta_dest_raw}' vs registrada='{cta_reg_raw}'.",
                    )
                else:
                    alerta_cnt += 1
                    monto_alerta += monto or 0
                    otros_duenos = [x for x in cuenta_a_clientes.get(cta_dest_norm, []) if x != c_norm]
                    ctx = (
                        f" Nota: la cuenta destino figura registrada a otro(s) cliente(s): {otros_duenos}."
                        if otros_duenos
                        else ""
                    )
                    add_alerta(
                        r_id,
                        "ALERTA_VERIFICACION_D04",
                        "CUENTA_DESTINO_DISTINTA",
                        dev_id,
                        str(row["venta_id"]),
                        c_norm,
                        "-",
                        monto,
                        (
                            f"cuenta_destino ('{cta_dest_norm}') difiere de cuenta_reembolso registrada "
                            f"('{cta_reg_norm}'). Debe verificarse antes de considerarse irregular.{ctx}"
                        ),
                    )

        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "EJECUTADA",
                "evaluados": eval_cnt,
                "conformes": eval_cnt - alerta_cnt - no_eval_cnt,
                "alertas_empresariales": alerta_cnt,
                "no_evaluables_calidad": no_eval_cnt,
                "monto_en_alerta_crc": monto_alerta,
            }
        )

    # ---------------------- R-D05 ----------------------
    r_id = "R-D05"
    if not apt_map[r_id]["habilitada_etapa_b"]:
        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "NO_EJECUTADA_DATOS_INSUFICIENTES",
                "evaluados": 0,
                "conformes": 0,
                "alertas_empresariales": 0,
                "no_evaluables_calidad": 0,
                "monto_en_alerta_crc": 0,
            }
        )
    else:
        assert df_ven is not None
        d_pag = get_dev_pagadas()
        ven_est = df_ven.set_index("venta_id__norm")["estado__norm"].to_dict()

        eval_cnt = len(d_pag)
        alerta_cnt = 0
        no_eval_cnt = 0
        monto_alerta = 0

        for _, row in d_pag.iterrows():
            dev_id = str(row["devolucion_id"])
            v_norm = row["venta_id__norm"]
            monto = int(row["monto_crc__num"]) if pd.notna(row["monto_crc__num"]) else None

            if v_norm is None or v_norm not in ven_est:
                no_eval_cnt += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "VENTA_INEXISTENTE_O_NULA",
                    dev_id,
                    str(row["venta_id"]),
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    "No es posible validar D-05 porque venta_id es nulo o inexistente en ventas.csv.",
                )
                continue

            est_v = ven_est[v_norm]
            if est_v is None:
                no_eval_cnt += 1
                add_alerta(
                    r_id,
                    "NO_EVALUABLE_CALIDAD",
                    "ESTADO_VENTA_NULO",
                    dev_id,
                    v_norm,
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    "ventas.estado es nulo/vacío; defecto de calidad impide evaluar D-05.",
                )
            elif est_v == "ANULADA":
                alerta_cnt += 1
                monto_alerta += monto or 0
                add_alerta(
                    r_id,
                    "ALERTA_EMPRESARIAL",
                    "VENTA_ANULADA_CORROBORAR_SECUENCIA",
                    dev_id,
                    v_norm,
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    "Devolución pagada vinculada a venta ANULADA. Amerita corroborar secuencia temporal y posible reversión.",
                )
            elif est_v != "ENTREGADA":
                alerta_cnt += 1
                monto_alerta += monto or 0
                add_alerta(
                    r_id,
                    "ALERTA_EMPRESARIAL",
                    "VENTA_ESTADO_NO_ENTREGADA",
                    dev_id,
                    v_norm,
                    str(row["cliente_id"]),
                    "-",
                    monto,
                    f"Devolución pagada vinculada a venta con estado '{est_v}' (política D-05 exige ENTREGADA).",
                )

        resumen_reglas.append(
            {
                "regla_id": r_id,
                "nombre_regla": apt_map[r_id]["nombre_regla"],
                "estado_ejecucion": "EJECUTADA",
                "evaluados": eval_cnt,
                "conformes": eval_cnt - alerta_cnt - no_eval_cnt,
                "alertas_empresariales": alerta_cnt,
                "no_evaluables_calidad": no_eval_cnt,
                "monto_en_alerta_crc": monto_alerta,
            }
        )

    df_resumen_reglas = pd.DataFrame(resumen_reglas)
    df_alertas = pd.DataFrame(
        alertas,
        columns=[
            "regla_id",
            "categoria",
            "subtipo",
            "devolucion_id",
            "venta_id",
            "cliente_id",
            "autorizacion_id",
            "monto_crc",
            "justificacion",
            "estado_investigacion",
        ],
    )
    df_senales = pd.DataFrame(
        senales_exp,
        columns=[
            "tipo_senal",
            "venta_id",
            "devoluciones_ids",
            "cantidad_devoluciones",
            "suma_crc",
            "detalle",
        ],
    )
    return df_resumen_reglas, df_alertas, df_senales


# ---------------------------------------------------------------------------
# Orquestador principal y exportadores
# ---------------------------------------------------------------------------

def analizar_lote(
    fuentes: Dict[str, Union[str, bytes, BinaryIO]],
    nombre_lote: str = "Lote_sin_etiqueta",
) -> ResultadoAuditoria:
    tablas_raw: Dict[str, Optional[pd.DataFrame]] = {}
    metas: Dict[str, Dict[str, Any]] = {}

    for nombre in ESQUEMA_TABLAS:
        if nombre not in fuentes or fuentes[nombre] is None:
            tablas_raw[nombre] = None
            metas[nombre] = {
                "tabla": nombre,
                "cargado": False,
                "error": "Archivo no suministrado.",
                "filas": 0,
                "columnas_detectadas": [],
                "codificacion": None,
                "sha256": None,
            }
        else:
            df, meta = cargar_csv_solo_lectura(fuentes[nombre], nombre)
            tablas_raw[nombre] = df
            metas[nombre] = meta

    (
        tablas_tipadas,
        df_resumen_tablas,
        df_hallazgos_a,
        df_aptitud,
        _,
        limitaciones,
    ) = _ejecutar_etapa_a(tablas_raw, metas)

    df_resumen_reglas, df_alertas_b, df_senales = _ejecutar_etapa_b(
        tablas_tipadas, df_aptitud
    )

    limitaciones.append(
        "Límites estructurales del escenario: no se dispone de guías de transporte, "
        "devoluciones físicas, estados bancarios, histórico de cambios de cuenta ni catálogo de roles de usuarios."
    )

    # Copia ligera solo con columnas originales para consulta de registros por ID en el chatbot
    tablas_consulta: Dict[str, Optional[pd.DataFrame]] = {}
    for nombre, df_raw in tablas_raw.items():
        tablas_consulta[nombre] = df_raw.copy() if df_raw is not None else None

    return ResultadoAuditoria(
        nombre_lote=nombre_lote,
        ejecutado_en=datetime.now().strftime(FORMATO_FECHA),
        metadatos_carga=metas,
        etapa_a_resumen_tablas=df_resumen_tablas,
        etapa_a_hallazgos=df_hallazgos_a,
        etapa_a_aptitud=df_aptitud,
        etapa_b_resumen_reglas=df_resumen_reglas,
        etapa_b_alertas=df_alertas_b,
        etapa_b_senales_exploratorias=df_senales,
        limitaciones_declaradas=limitaciones,
        tablas_consulta=tablas_consulta,
    )


def exportar_reporte_json(res: ResultadoAuditoria) -> str:
    payload = {
        "nombre_lote": res.nombre_lote,
        "ejecutado_en": res.ejecutado_en,
        "nota_metodologica": NOTA_METODOLOGICA,
        "trazabilidad_archivos": res.metadatos_carga,
        "etapa_a": {
            "resumen_tablas": res.etapa_a_resumen_tablas.to_dict(orient="records"),
            "aptitud_reglas": res.etapa_a_aptitud.to_dict(orient="records"),
            "hallazgos_calidad": res.etapa_a_hallazgos.to_dict(orient="records"),
        },
        "etapa_b": {
            "resumen_reglas": res.etapa_b_resumen_reglas.to_dict(orient="records"),
            "alertas": res.etapa_b_alertas.to_dict(orient="records"),
            "senales_exploratorias": res.etapa_b_senales_exploratorias.to_dict(orient="records"),
        },
        "limitaciones_declaradas": res.limitaciones_declaradas,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def exportar_reporte_markdown(res: ResultadoAuditoria) -> str:
    def _df_a_md(df: pd.DataFrame) -> str:
        if df.empty:
            return "_Sin registros._"
        cols = list(df.columns)
        enc = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join(["---"] * len(cols)) + " |"
        filas = []
        for _, r in df.iterrows():
            vals = [str(r[c]).replace("|", "\\|").replace("\n", " ") for c in cols]
            filas.append("| " + " | ".join(vals) + " |")
        return "\n".join([enc, sep] + filas)

    secciones = [
        f"# Informe Técnico de Auditoría de Datos — {res.nombre_lote}",
        f"**Fecha de ejecución:** {res.ejecutado_en}",
        f"> {NOTA_METODOLOGICA}",
        "## Etapa A · Resumen de carga y calidad de tablas",
        _df_a_md(res.etapa_a_resumen_tablas),
        "## Etapa A · Matriz de aptitud por regla",
        _df_a_md(res.etapa_a_aptitud),
        "## Etapa A · Hallazgos de calidad e integridad referencial",
        _df_a_md(res.etapa_a_hallazgos),
        "## Etapa B · Totales por regla empresarial",
        _df_a_md(res.etapa_b_resumen_reglas),
        "## Etapa B · Detalle de alertas y registros no evaluables",
        _df_a_md(res.etapa_b_alertas),
        "## Etapa B · Señales exploratorias complementarias (no incumplen política literal)",
        _df_a_md(res.etapa_b_senales_exploratorias),
        "## Limitaciones declaradas",
        "\n".join(f"- {lim}" for lim in res.limitaciones_declaradas),
    ]
    return "\n\n".join(secciones)
