"""
Gestión de carga por lote completo y persistencia local rápida de lotes analizados
para el Caso 03 (Comercio Electrónico Jacaranda S. A.).

Características:
1. Carga de lote completo en un solo paso:
   - Múltiples archivos CSV seleccionados o arrastrados juntos.
   - Archivo .ZIP con los 4 CSV (incluso dentro de una subcarpeta).
   - Carpeta local en disco (estrictamente en modo solo lectura).
2. Almacenamiento local persistente optimizado en `lotes_guardados/`:
   - Índice ligero `_indice_lotes.json` para listar lotes en < 1 ms sin leer
     todos los lotes completos en cada interacción.
   - Archivo de chat separado (`<slug>_chat.json`) para guardar mensajes sin
     reescribir el archivo completo del lote.
   - Compatibilidad total con lotes guardados previamente en versión 3.0.
   - Nunca modifica los archivos CSV originales.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from auditoria_motor import ESQUEMA_TABLAS, ResultadoAuditoria

MAPA_ARCHIVO_A_TABLA: Dict[str, str] = {
    spec["archivo_esperado"].lower(): tabla
    for tabla, spec in ESQUEMA_TABLAS.items()
}

INDICE_FILENAME = "_indice_lotes.json"
SUFIJO_CHAT = "_chat.json"


# ---------------------------------------------------------------------------
# Carga de lote completo (múltiples CSV, ZIP o carpeta local en solo lectura)
# ---------------------------------------------------------------------------

def _es_archivo_oculto_sistema(ruta_interna: str) -> bool:
    partes = ruta_interna.replace("\\", "/").split("/")
    for p in partes:
        if p == "__MACOSX" or p.startswith("._") or p.startswith("."):
            return True
    return False


def extraer_fuentes_de_archivos_subidos(
    archivos: List[Tuple[str, bytes]],
) -> Tuple[Dict[str, Optional[bytes]], Dict[str, Any]]:
    fuentes: Dict[str, Optional[bytes]] = {tabla: None for tabla in ESQUEMA_TABLAS}
    detectados: Dict[str, str] = {}
    ignorados: List[str] = []
    duplicados: List[str] = []
    errores_lectura: List[str] = []

    def _registrar_csv(nombre_origen: str, contenido: bytes) -> None:
        base = os.path.basename(nombre_origen.replace("\\", "/")).strip().lower()
        tabla = MAPA_ARCHIVO_A_TABLA.get(base)
        if tabla is None:
            ignorados.append(nombre_origen)
            return
        if fuentes[tabla] is not None:
            duplicados.append(
                f"{nombre_origen} (reemplazó a {detectados[tabla]} para tabla '{tabla}')"
            )
        fuentes[tabla] = contenido
        detectados[tabla] = nombre_origen

    for nombre, raw in archivos:
        nombre_norm = nombre.strip()
        if nombre_norm.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir() or _es_archivo_oculto_sistema(info.filename):
                            continue
                        if info.filename.lower().endswith(".csv"):
                            _registrar_csv(
                                f"{nombre_norm} :: {info.filename}",
                                zf.read(info.filename),
                            )
                        else:
                            ignorados.append(f"{nombre_norm} :: {info.filename}")
            except zipfile.BadZipFile:
                errores_lectura.append(
                    f"El archivo '{nombre_norm}' no es un archivo ZIP válido o está dañado."
                )
            except Exception as exc:
                errores_lectura.append(f"Error al abrir '{nombre_norm}': {exc}")
        elif nombre_norm.lower().endswith(".csv"):
            _registrar_csv(nombre_norm, raw)
        else:
            ignorados.append(nombre_norm)

    faltantes = [
        ESQUEMA_TABLAS[t]["archivo_esperado"]
        for t in ESQUEMA_TABLAS
        if fuentes[t] is None
    ]

    diagnostico = {
        "detectados": detectados,
        "faltantes": faltantes,
        "ignorados": ignorados,
        "duplicados": duplicados,
        "errores_lectura": errores_lectura,
        "completo": len(faltantes) == 0 and len(errores_lectura) == 0,
    }
    return fuentes, diagnostico


def extraer_fuentes_de_carpeta(
    ruta_carpeta: str,
) -> Tuple[Dict[str, Optional[bytes]], Dict[str, Any]]:
    fuentes: Dict[str, Optional[bytes]] = {tabla: None for tabla in ESQUEMA_TABLAS}
    detectados: Dict[str, str] = {}
    ignorados: List[str] = []
    errores_lectura: List[str] = []

    ruta_limpia = os.path.expanduser(ruta_carpeta.strip().strip('"').strip("'"))
    if not ruta_limpia or not os.path.isdir(ruta_limpia):
        errores_lectura.append(f"La carpeta '{ruta_carpeta}' no existe o no es un directorio válido.")
        return fuentes, {
            "detectados": detectados,
            "faltantes": [spec["archivo_esperado"] for spec in ESQUEMA_TABLAS.values()],
            "ignorados": ignorados,
            "duplicados": [],
            "errores_lectura": errores_lectura,
            "completo": False,
        }

    try:
        entradas = sorted(os.listdir(ruta_limpia))
    except Exception as exc:
        errores_lectura.append(f"No se pudo listar la carpeta: {exc}")
        return fuentes, {
            "detectados": detectados,
            "faltantes": [spec["archivo_esperado"] for spec in ESQUEMA_TABLAS.values()],
            "ignorados": ignorados,
            "duplicados": [],
            "errores_lectura": errores_lectura,
            "completo": False,
        }

    for entry in entradas:
        ruta_completa = os.path.join(ruta_limpia, entry)
        if not os.path.isfile(ruta_completa) or entry.startswith("."):
            continue
        tabla = MAPA_ARCHIVO_A_TABLA.get(entry.lower())
        if tabla is None:
            if entry.lower().endswith(".csv"):
                ignorados.append(entry)
            continue
        try:
            with open(ruta_completa, "rb") as f:
                fuentes[tabla] = f.read()
            detectados[tabla] = ruta_completa
        except Exception as exc:
            errores_lectura.append(f"Error al leer '{entry}': {exc}")

    faltantes = [
        ESQUEMA_TABLAS[t]["archivo_esperado"]
        for t in ESQUEMA_TABLAS
        if fuentes[t] is None
    ]
    return fuentes, {
        "detectados": detectados,
        "faltantes": faltantes,
        "ignorados": ignorados,
        "duplicados": [],
        "errores_lectura": errores_lectura,
        "completo": len(faltantes) == 0 and len(errores_lectura) == 0,
    }


# ---------------------------------------------------------------------------
# Serialización compacta y almacenamiento local persistente por lote
# ---------------------------------------------------------------------------

def _slug_seguro(nombre_lote: str) -> str:
    limpio = re.sub(r"[^A-Za-z0-9_-]+", "_", nombre_lote.strip())
    limpio = limpio.strip("_")
    return limpio or "Lote_sin_etiqueta"


def _df_a_dict_serializable(df: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    if df is None:
        return None
    df_clean = df.where(pd.notna(df), None)
    return {
        "columns": list(df_clean.columns),
        "records": df_clean.to_dict(orient="records"),
    }


def _dict_a_df(
    data: Optional[Dict[str, Any]],
    cols_int: Optional[List[str]] = None,
    cols_bool: Optional[List[str]] = None,
) -> Optional[pd.DataFrame]:
    if data is None:
        return None
    cols = data.get("columns", [])
    recs = data.get("records", [])
    df = pd.DataFrame(recs, columns=cols)
    if cols_int:
        for c in cols_int:
            if c in df.columns and not df.empty:
                df[c] = pd.to_numeric(df[c], errors="coerce")
    if cols_bool:
        for c in cols_bool:
            if c in df.columns and not df.empty:
                df[c] = df[c].astype(bool)
    return df


def _extraer_kpis(res: ResultadoAuditoria) -> Dict[str, Any]:
    reglas_aptas = (
        int(res.etapa_a_aptitud["habilitada_etapa_b"].sum())
        if not res.etapa_a_aptitud.empty
        else 0
    )
    hallazgos_a = int(len(res.etapa_a_hallazgos))
    alertas_b = (
        int(
            res.etapa_b_alertas["categoria"]
            .isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"])
            .sum()
        )
        if not res.etapa_b_alertas.empty
        else 0
    )
    no_eval_b = (
        int((res.etapa_b_alertas["categoria"] == "NO_EVALUABLE_CALIDAD").sum())
        if not res.etapa_b_alertas.empty
        else 0
    )
    monto_alerta = (
        int(res.etapa_b_resumen_reglas["monto_en_alerta_crc"].sum())
        if not res.etapa_b_resumen_reglas.empty
        else 0
    )
    senales_exp = int(len(res.etapa_b_senales_exploratorias))

    return {
        "nombre_lote": res.nombre_lote,
        "ejecutado_en": res.ejecutado_en,
        "reglas_aptas": reglas_aptas,
        "hallazgos_etapa_a": hallazgos_a,
        "alertas_etapa_b": alertas_b,
        "no_evaluables_calidad": no_eval_b,
        "monto_en_alerta_crc": monto_alerta,
        "senales_exploratorias": senales_exp,
    }


def serializar_lote(
    res: ResultadoAuditoria,
    chat_historial: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    return {
        "version_formato": "3.1",
        "nombre_lote": res.nombre_lote,
        "ejecutado_en": res.ejecutado_en,
        "resumen_kpis": _extraer_kpis(res),
        "metadatos_carga": res.metadatos_carga,
        "etapa_a_resumen_tablas": _df_a_dict_serializable(res.etapa_a_resumen_tablas),
        "etapa_a_hallazgos": _df_a_dict_serializable(res.etapa_a_hallazgos),
        "etapa_a_aptitud": _df_a_dict_serializable(res.etapa_a_aptitud),
        "etapa_b_resumen_reglas": _df_a_dict_serializable(res.etapa_b_resumen_reglas),
        "etapa_b_alertas": _df_a_dict_serializable(res.etapa_b_alertas),
        "etapa_b_senales_exploratorias": _df_a_dict_serializable(res.etapa_b_senales_exploratorias),
        "limitaciones_declaradas": list(res.limitaciones_declaradas),
        "tablas_consulta": {
            k: _df_a_dict_serializable(v)
            for k, v in (res.tablas_consulta or {}).items()
        },
        "chat_historial": chat_historial or [],
    }


def deserializar_lote(payload: Dict[str, Any]) -> Tuple[ResultadoAuditoria, List[Dict[str, str]]]:
    df_res_tablas = _dict_a_df(
        payload.get("etapa_a_resumen_tablas"),
        cols_int=["filas", "columnas_faltantes", "pk_duplicadas", "errores_tipo"],
        cols_bool=["cargada"],
    )
    df_hall_a = _dict_a_df(
        payload.get("etapa_a_hallazgos"),
        cols_int=["cantidad_afectada"],
    )
    df_apt = _dict_a_df(
        payload.get("etapa_a_aptitud"),
        cols_bool=["habilitada_etapa_b"],
    )
    df_res_reg = _dict_a_df(
        payload.get("etapa_b_resumen_reglas"),
        cols_int=[
            "evaluados",
            "conformes",
            "alertas_empresariales",
            "no_evaluables_calidad",
            "monto_en_alerta_crc",
        ],
    )
    df_alertas = _dict_a_df(
        payload.get("etapa_b_alertas"),
        cols_int=["monto_crc"],
    )
    df_senales = _dict_a_df(
        payload.get("etapa_b_senales_exploratorias"),
        cols_int=["cantidad_devoluciones", "suma_crc"],
    )

    tablas_consulta_raw = payload.get("tablas_consulta") or {}
    tablas_consulta = {k: _dict_a_df(v) for k, v in tablas_consulta_raw.items()}

    res = ResultadoAuditoria(
        nombre_lote=str(payload["nombre_lote"]),
        ejecutado_en=str(payload["ejecutado_en"]),
        metadatos_carga=payload.get("metadatos_carga", {}),
        etapa_a_resumen_tablas=df_res_tablas if df_res_tablas is not None else pd.DataFrame(),
        etapa_a_hallazgos=df_hall_a if df_hall_a is not None else pd.DataFrame(),
        etapa_a_aptitud=df_apt if df_apt is not None else pd.DataFrame(),
        etapa_b_resumen_reglas=df_res_reg if df_res_reg is not None else pd.DataFrame(),
        etapa_b_alertas=df_alertas if df_alertas is not None else pd.DataFrame(),
        etapa_b_senales_exploratorias=df_senales if df_senales is not None else pd.DataFrame(),
        limitaciones_declaradas=list(payload.get("limitaciones_declaradas", [])),
        tablas_consulta=tablas_consulta,
    )
    chat_hist = payload.get("chat_historial") or []
    return res, chat_hist


def _escribir_json_atomico(ruta: str, objeto: Any, compacto: bool = True) -> None:
    ruta_tmp = ruta + ".tmp"
    with open(ruta_tmp, "w", encoding="utf-8") as f:
        if compacto:
            json.dump(objeto, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(objeto, f, ensure_ascii=False, indent=2)
    os.replace(ruta_tmp, ruta)


class AlmacenLotes:
    """
    Repositorio local en disco (`lotes_guardados/`) con índice rápido y
    persistencia separada de chats por lote.
    """

    def __init__(self, directorio: str = "lotes_guardados") -> None:
        self.directorio = directorio
        os.makedirs(self.directorio, exist_ok=True)

    def _ruta_lote(self, nombre_lote: str) -> str:
        return os.path.join(self.directorio, f"{_slug_seguro(nombre_lote)}.json")

    def _ruta_chat(self, nombre_lote: str) -> str:
        return os.path.join(self.directorio, f"{_slug_seguro(nombre_lote)}{SUFIJO_CHAT}")

    def _ruta_indice(self) -> str:
        return os.path.join(self.directorio, INDICE_FILENAME)

    def _es_archivo_lote(self, fname: str) -> bool:
        return (
            fname.endswith(".json")
            and fname != INDICE_FILENAME
            and not fname.endswith(SUFIJO_CHAT)
        )

    def _leer_indice(self) -> Dict[str, Dict[str, Any]]:
        ruta_idx = self._ruta_indice()
        if not os.path.isfile(ruta_idx):
            return {}
        try:
            with open(ruta_idx, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _guardar_indice(self, indice: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(self.directorio, exist_ok=True)
        _escribir_json_atomico(self._ruta_indice(), indice, compacto=False)

    def guardar_lote(
        self,
        res: ResultadoAuditoria,
        chat_historial: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        os.makedirs(self.directorio, exist_ok=True)
        payload = serializar_lote(res, chat_historial=chat_historial)
        ruta = self._ruta_lote(res.nombre_lote)
        _escribir_json_atomico(ruta, payload, compacto=True)

        # Guardar chat en archivo liviano separado
        _escribir_json_atomico(self._ruta_chat(res.nombre_lote), chat_historial or [], compacto=True)

        # Actualizar índice ligero
        fname = os.path.basename(ruta)
        indice = self._leer_indice()
        kpis = dict(payload["resumen_kpis"])
        kpis["archivo"] = fname
        kpis["mtime"] = os.path.getmtime(ruta)
        indice[fname] = kpis
        self._guardar_indice(indice)
        return ruta

    def actualizar_chat(
        self,
        nombre_lote: str,
        chat_historial: List[Dict[str, str]],
    ) -> bool:
        ruta_lote = self._ruta_lote(nombre_lote)
        if not os.path.isfile(ruta_lote):
            # Verificar por nombre en índice
            encontrado = None
            for info in self.listar_lotes():
                if info["nombre_lote"] == nombre_lote:
                    encontrado = info["ruta_archivo"]
                    break
            if encontrado is None:
                return False
            base_sin_ext = encontrado[:-5]
            ruta_chat = base_sin_ext + SUFIJO_CHAT
        else:
            ruta_chat = self._ruta_chat(nombre_lote)

        try:
            _escribir_json_atomico(ruta_chat, chat_historial, compacto=True)
            return True
        except Exception:
            return False

    def cargar_lote(
        self,
        nombre_lote: str,
    ) -> Optional[Tuple[ResultadoAuditoria, List[Dict[str, str]]]]:
        ruta = self._ruta_lote(nombre_lote)
        if not os.path.isfile(ruta):
            for info in self.listar_lotes():
                if info["nombre_lote"] == nombre_lote:
                    ruta = info["ruta_archivo"]
                    break
            else:
                return None

        try:
            with open(ruta, "r", encoding="utf-8") as f:
                payload = json.load(f)
            res, chat_embebido = deserializar_lote(payload)

            # Si existe archivo separado de chat, tiene prioridad por ser el más reciente
            ruta_chat = ruta[:-5] + SUFIJO_CHAT
            if os.path.isfile(ruta_chat):
                try:
                    with open(ruta_chat, "r", encoding="utf-8") as fc:
                        chat_sep = json.load(fc)
                    if isinstance(chat_sep, list):
                        return res, chat_sep
                except Exception:
                    pass
            return res, chat_embebido
        except Exception:
            return None

    def listar_lotes(self) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.directorio):
            return []

        archivos_actuales = {
            fname
            for fname in os.listdir(self.directorio)
            if self._es_archivo_lote(fname)
        }
        indice = self._leer_indice()
        indice_modificado = False

        # Quitar del índice archivos que ya no existen en disco
        for fname in list(indice.keys()):
            if fname not in archivos_actuales:
                del indice[fname]
                indice_modificado = True

        # Releer únicamente archivos nuevos o modificados desde la última indexación
        for fname in sorted(archivos_actuales):
            ruta = os.path.join(self.directorio, fname)
            try:
                mtime_actual = os.path.getmtime(ruta)
            except OSError:
                continue

            entrada = indice.get(fname)
            if entrada is not None and abs(float(entrada.get("mtime", -1)) - mtime_actual) < 1e-6:
                continue

            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                kpis = dict(payload.get("resumen_kpis", {}))
                if not kpis:
                    kpis = {
                        "nombre_lote": payload.get("nombre_lote", fname[:-5]),
                        "ejecutado_en": payload.get("ejecutado_en", "-"),
                    }
                kpis["archivo"] = fname
                kpis["mtime"] = mtime_actual
                indice[fname] = kpis
                indice_modificado = True
            except Exception:
                continue

        if indice_modificado:
            try:
                self._guardar_indice(indice)
            except Exception:
                pass

        lotes: List[Dict[str, Any]] = []
        for fname, kpis in indice.items():
            item = dict(kpis)
            item["ruta_archivo"] = os.path.join(self.directorio, fname)
            lotes.append(item)

        lotes.sort(key=lambda x: str(x.get("nombre_lote", "")).lower())
        return lotes

    def eliminar_lote(self, nombre_lote: str) -> bool:
        ruta = self._ruta_lote(nombre_lote)
        if not os.path.isfile(ruta):
            ruta_encontrada = None
            for info in self.listar_lotes():
                if info["nombre_lote"] == nombre_lote:
                    ruta_encontrada = info["ruta_archivo"]
                    break
            if ruta_encontrada is None or not os.path.isfile(ruta_encontrada):
                return False
            ruta = ruta_encontrada

        os.remove(ruta)
        ruta_chat = ruta[:-5] + SUFIJO_CHAT
        if os.path.isfile(ruta_chat):
            os.remove(ruta_chat)

        fname = os.path.basename(ruta)
        indice = self._leer_indice()
        if fname in indice:
            del indice[fname]
            self._guardar_indice(indice)
        return True
