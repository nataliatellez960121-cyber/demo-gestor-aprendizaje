#!/usr/bin/env python3
"""Normaliza, almacena y consolida lotes de experiencias autorizadas."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

PUBLIC_FIELDS = ("titulo", "resumen", "historia", "tipo", "metodologia", "unidad", "autores", "correoContacto")
REQUIRED_FIELDS = ("titulo", "resumen", "historia")
_VALUE_PATTERN = re.compile(r"""['"]Value['"]\s*:\s*['"](.*?)['"]\s*[,}]""")

def unwrap(value):
    if isinstance(value, dict):
        return value.get("Value", value.get("value", ""))
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") and "Value" in s:
            try:
                parsed = json.loads(s)
            except (ValueError, json.JSONDecodeError):
                try:
                    parsed = ast.literal_eval(s)
                except (ValueError, SyntaxError):
                    parsed = None
            if isinstance(parsed, dict):
                return parsed.get("Value", parsed.get("value", ""))
            match = _VALUE_PATTERN.search(s)
            if match:
                return match.group(1)
    return value


def normalize_value(value):
    if value is None:
        return ""

    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    value = parsed
            except (ValueError, json.JSONDecodeError):
                pass

    if isinstance(value, list):
        return ", ".join(
            x for x in (normalize_value(v) for v in value) if x
        )

    value = unwrap(value)

    if isinstance(value, dict):
        value = value.get("Value", value.get("value", ""))

    return re.sub(
        r"\s+", " ", str(value).replace("\xa0", " ")
    ).strip().rstrip(";").strip()

def normalize_record(raw):
    return {field: normalize_value(raw.get(field, "")) for field in PUBLIC_FIELDS}

def record_key(raw, record):
    internal_id = raw.get("id") or raw.get("Id") or raw.get("ID")
    token = f"id:{internal_id}" if internal_id not in (None, "") else f"titulo:{record['titulo'].casefold()}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]

def clean_batch(raw_records):
    cleaned = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        record = normalize_record(raw)
        if all(record.get(field, "").strip() for field in REQUIRED_FIELDS):
            cleaned.append({"_key": record_key(raw, record), **record})
    return cleaned

def parse_payload(raw_text):
    data = json.loads(raw_text)
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = data.get("experiencias", [])
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        data = data.get("experiencias", [])
    if not isinstance(data, list):
        raise ValueError(f"Se esperaba una lista JSON de experiencias; llegó {type(data).__name__}.")
    return data

def save_batch(state_dir, lote_index, raw_records):
    state_dir.mkdir(parents=True, exist_ok=True)
    cleaned = clean_batch(raw_records)
    path = state_dir / f"lote_{lote_index:04d}.json"
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Lote {lote_index} guardado: {len(cleaned)} registros válidos.")

def finalize(state_dir, total_lotes, output):
    expected = [state_dir / f"lote_{i:04d}.json" for i in range(total_lotes)]
    missing = [p.name for p in expected if not p.exists()]
    if missing:
        raise ValueError("Faltan lotes: " + ", ".join(missing))
    merged = {}
    for path in expected:
        for record in json.loads(path.read_text(encoding="utf-8")):
            merged[record["_key"]] = {field: record.get(field, "") for field in PUBLIC_FIELDS}
    result = sorted(merged.values(), key=lambda r: r["titulo"].casefold())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Sincronización completa: {len(result)} historias públicas únicas.")

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_lote = sub.add_parser("lote")
    p_lote.add_argument("--state-dir", required=True)
    p_lote.add_argument("--lote-index", type=int, required=True)
    p_lote.add_argument("--payload-file", required=True)
    p_fin = sub.add_parser("finalizar")
    p_fin.add_argument("--state-dir", required=True)
    p_fin.add_argument("--total-lotes", type=int, required=True)
    p_fin.add_argument("--final-output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "lote":
            save_batch(Path(args.state_dir), args.lote_index,
                       parse_payload(Path(args.payload_file).read_text(encoding="utf-8")))
        else:
            finalize(Path(args.state_dir), args.total_lotes, Path(args.final_output))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
