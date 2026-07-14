"""Carga testimonios reales de landing desde JSON.

Uso:
  & "c:/Users/USUARIO/Desktop/stock ultimate/.venv/Scripts/python.exe" scripts/seed_landing_testimonials.py --file data/landing_testimonials.json

Formato JSON esperado (lista):
[
  {
    "author_name": "Nombre real",
    "company_name": "Empresa real",
    "quote": "Testimonio real",
    "active": true
  }
]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _validate_row(row: dict, index: int) -> tuple[bool, str]:
    author = str(row.get("author_name") or "").strip()
    quote = str(row.get("quote") or "").strip()
    if not author:
        return False, f"Fila {index}: author_name es obligatorio"
    if not quote:
        return False, f"Fila {index}: quote es obligatorio"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed de testimonios reales para landing")
    parser.add_argument("--file", required=True, help="Ruta al JSON con testimonios reales")
    parser.add_argument("--replace", action="store_true", help="Elimina testimonios existentes antes de cargar")
    args = parser.parse_args()

    json_path = Path(args.file)
    if not json_path.exists():
        print(f"Archivo no encontrado: {json_path}")
        return 1

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        print("El JSON debe ser una lista de testimonios")
        return 1

    for idx, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            print(f"Fila {idx}: cada elemento debe ser objeto JSON")
            return 1
        ok, message = _validate_row(row, idx)
        if not ok:
            print(message)
            return 1

    from app import LandingTestimonial, app, db

    with app.app_context():
        if args.replace:
            LandingTestimonial.query.delete()

        inserted = 0
        for row in payload:
            db.session.add(
                LandingTestimonial(
                    author_name=str(row.get("author_name") or "").strip()[:120],
                    company_name=(str(row.get("company_name") or "").strip()[:160] or None),
                    quote=str(row.get("quote") or "").strip(),
                    active=bool(row.get("active", True)),
                )
            )
            inserted += 1

        db.session.commit()

    print(f"Testimonios cargados: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
