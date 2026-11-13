#!/usr/bin/env python3
"""Normaliza y consolida las experiencias autorizadas enviadas por Power Automate.

SharePoint es la fuente de verdad. Este módulo recibe lotes (posiblemente
varios por sincronización, para respetar el límite de tamaño de
client_payload) y reconstruye el conjunto público completo correspondiente
a una sola sincronización, sin acumular registros de sincronizaciones
anteriores.

Uso como librería (para pruebas) o como CLI desde el workflow de GitHub
Actions (ver .github/workflows/publicar-experiencias.yml).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

PUBLIC_FIELDS = ("titulo", "resumen", "historia", "tipo", "metodologia", "unidad")
REQUIRED_FIELDS = ("titulo", "resumen", "historia")

_VALUE_PATTERN = re.compile(r"""['"]Value['"]\s*:\s*['"](.*?)['"]\s*[,}]""")


class SyncMismatchError(RuntimeError):
    """El lote recibido no pertenece a la sincronización activa."""


def _unwrap_sharepoint_object(value):
    """Si value es (o parece ser) un objeto Choice/Lookup de SharePoint, devuelve su texto."""
    if isinstance(value, dict):
        return value.get("Value", value.get("value", ""))

    if isinstance(value, str):
        text = value.strip()
        looks_like_object = text.startswith("{") and "Value" in text
        if looks_like_object:
            # Power Automate a veces serializa el objeto como JSON válido...
            try:
                parsed = json.loads(text)
            except (ValueError, json.JSONDecodeError):
                parsed = None
            # ...y otras veces como repr de diccionario de Python (comillas simples).
            if parsed is None:
                try:
                    parsed = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    parsed = None
            if isinstance(parsed, dict):
                return parsed.get("Value", parsed.get("value", ""))
            match = _VALUE_PATTERN.search(text)
            if match:
                return match.group(1)

    return value


def normalize_value(value) -> str:
    """Convierte cualquier formato que pueda llegar de SharePoint a texto plano."""
    if value is None:
        return ""

    if isinstance(value, list):
        parts = [normalize_value(item) for item in value]
        return ", ".join(part for part in parts if part)

    value = _unwrap_sharepoint_object(value)
    if isinstance(value, dict):
        value = value.get("Value", value.get("value", ""))

    text = "" if value is None else str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Algunos campos Lookup de un solo valor llegan con un ";" final residual.
    text = re.sub(r"\s*;\s*$", "", text).strip()
    return text


def normalize_record(raw: dict) -> dict:
    """Devuelve únicamente los seis campos públicos, normalizados a texto plano."""
    return {field: normalize_value(raw.get(field, "")) for field in PUBLIC_FIELDS}


def is_valid(record: dict) -> bool:
    """Título, resumen e historia deben tener contenido (reglas 4, 5 y 6)."""
    return all(record.get(field, "").strip() for field in REQUIRED_FIELDS)


def record_key(raw: dict, record: dict) -> str:
    """Clave estable para deduplicar. Prefiere el id interno de SharePoint (sin publicarlo)."""
    internal_id = raw.get("id") or raw.get("Id") or raw.get("ID")
    if internal_id not in (None, ""):
        token = f"id:{internal_id}"
    else:
        token = f"titulo:{record['titulo'].casefold()}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def clean_batch(raw_records: list) -> tuple[list, int]:
    """Normaliza y valida una lista de registros crudos. Devuelve (limpios, omitidos)."""
    cleaned = []
    skipped = 0
    for raw in raw_records:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        record = normalize_record(raw)
        if not is_valid(record):
            skipped += 1
            continue
        key = record_key(raw, record)
        cleaned.append({"_key": key, **record})
    return cleaned, skipped


def consolidate(batches: list) -> list:
    """Fusiona varios lotes ya normalizados en el conjunto público final, sin duplicados."""
    merged = {}
    for batch in batches:
        for record in batch:
            merged[record["_key"]] = {field: record[field] for field in PUBLIC_FIELDS}
    return sorted(merged.values(), key=lambda r: r["titulo"].casefold())


# ---------------------------------------------------------------------------
# Manejo de estado de sincronización (usado por el CLI / workflow)
# ---------------------------------------------------------------------------

def _manifest_path(state_dir: Path) -> Path:
    return state_dir / "manifest.json"


def load_manifest(state_dir: Path):
    path = _manifest_path(state_dir)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_manifest(state_dir: Path, manifest: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _manifest_path(state_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def reset_state(state_dir: Path, sync_id: str, total_lotes: int) -> dict:
    """Inicia una nueva sincronización: descarta cualquier lote de una sincronización anterior."""
    if state_dir.exists():
        for stale in state_dir.glob("lote_*.json"):
            stale.unlink()
    manifest = {"sync_id": sync_id, "total_lotes": total_lotes, "lotes_recibidos": []}
    save_manifest(state_dir, manifest)
    return manifest


def apply_lote(state_dir: Path, sync_id: str, lote_index: int, total_lotes: int, raw_records: list):
    """Procesa un lote de la sincronización `sync_id` y lo persiste en el estado."""
    manifest = load_manifest(state_dir)
    if manifest is None or manifest.get("sync_id") != sync_id:
        raise SyncMismatchError(
            f"El lote pertenece a sync_id={sync_id!r}, pero la sincronización activa es "
            f"{manifest.get('sync_id') if manifest else None!r}. "
            "Debe enviarse primero el evento ge_iniciar_sincronizacion con este sync_id."
        )

    if total_lotes:
        manifest["total_lotes"] = total_lotes

    cleaned, skipped = clean_batch(raw_records)
    (state_dir / f"lote_{lote_index:04d}.json").write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    recibidos = sorted(set(manifest.get("lotes_recibidos", [])) | {lote_index})
    manifest["lotes_recibidos"] = recibidos
    save_manifest(state_dir, manifest)

    completed = bool(manifest["total_lotes"]) and len(recibidos) >= manifest["total_lotes"]
    return manifest, skipped, completed


def consolidate_state(state_dir: Path) -> list:
    """Lee todos los lotes almacenados en `state_dir` y devuelve el conjunto consolidado."""
    batches = []
    for lote_file in sorted(state_dir.glob("lote_*.json")):
        batches.append(json.loads(lote_file.read_text(encoding="utf-8")))
    return consolidate(batches)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_payload(raw_text: str):
    data = json.loads(raw_text)
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = data.get("experiencias", [])
    if not isinstance(data, list):
        raise SystemExit("El contenido del lote debe ser una lista JSON de experiencias.")
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("iniciar", help="Reinicia el estado para una nueva sincronización")
    p_init.add_argument("--state-dir", required=True)
    p_init.add_argument("--sync-id", required=True)
    p_init.add_argument("--total-lotes", type=int, default=0)

    p_lote = sub.add_parser("lote", help="Procesa un lote de experiencias")
    p_lote.add_argument("--state-dir", required=True)
    p_lote.add_argument("--sync-id", required=True)
    p_lote.add_argument("--lote-index", type=int, required=True)
    p_lote.add_argument("--total-lotes", type=int, default=0)
    p_lote.add_argument("--payload-file", required=True)
    p_lote.add_argument("--final-output")
    p_lote.add_argument("--completed-marker")

    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)

    if args.command == "iniciar":
        manifest = reset_state(state_dir, args.sync_id, args.total_lotes)
        print(f"Sincronización reiniciada: sync_id={manifest['sync_id']} total_lotes={manifest['total_lotes']}")
        return 0

    raw_records = _parse_payload(Path(args.payload_file).read_text(encoding="utf-8"))

    try:
        manifest, skipped, completed = apply_lote(
            state_dir, args.sync_id, args.lote_index, args.total_lotes, raw_records
        )
    except SyncMismatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"Lote {args.lote_index} recibido para sync_id={manifest['sync_id']}. "
        f"Registros válidos: {len(raw_records) - skipped}. Omitidos: {skipped}."
    )
    print(f"Lotes recibidos hasta ahora: {manifest['lotes_recibidos']} de {manifest['total_lotes']}")

    if completed:
        resultado = consolidate_state(state_dir)
        if args.final_output:
            Path(args.final_output).write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        if args.completed_marker:
            Path(args.completed_marker).write_text("1", encoding="utf-8")
        print(f"Sincronización completa. Historias públicas únicas: {len(resultado)}")
    else:
        print("Sincronización incompleta; en espera de más lotes. No se publica nada todavía.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
