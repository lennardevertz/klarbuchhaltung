#!/usr/bin/env python3
"""
MCP-Server für die Buchhaltung.

Stellt dieselben Tools wie Chat und CLI über das Model Context Protocol bereit,
damit Agenten-Werkzeuge (Claude Code, OpenAI Codex CLI, Cursor …) die
Buchhaltung direkt bedienen können. Transport: stdio.

Die Tools werden aus assistant.tools_for() erzeugt – dadurch ist der Funktions-
umfang von Chat, `./bb call` und MCP zwangsläufig identisch: ein neues Tool in
assistant.py steht hier automatisch zur Verfügung.

Registrieren, z. B. in Claude Code (Pfad zum eigenen Klon):
    claude mcp add buchhaltung -- /pfad/zum/repo/bb-mcp

Bei mehreren Profilen das gewünschte über die Umgebung wählen:
    BB_PROFILE=<slug> ./bb-mcp
"""
from __future__ import annotations

import inspect

from mcp.server.fastmcp import FastMCP

import assistant
import core

mcp = FastMCP("Buchhaltung")

# 'present' rendert Karten in der Weboberfläche und ergibt außerhalb des Chats keinen Sinn.
# Die Entwickler-Tools (Shell/Datei) bleiben absichtlich der lokalen Oberfläche vorbehalten.
EXCLUDED = {"present", "run_shell", "read_file", "write_file"}

_TYPES = {"string": str, "integer": int, "number": float,
          "boolean": bool, "array": list, "object": dict}


def _build(name: str, description: str, schema: dict):
    """Aus einem JSON-Schema eine typisierte Funktion bauen, die an assistant.dispatch weiterreicht."""
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    params, annotations = [], {}
    for pname in sorted(props, key=lambda p: (p not in required, p)):
        ptype = _TYPES.get((props[pname] or {}).get("type"), str)
        if pname in required:
            params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                            annotation=ptype))
            annotations[pname] = ptype
        else:
            params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                            annotation=ptype | None, default=None))
            annotations[pname] = ptype | None

    def impl(**kwargs) -> str:
        # nicht gesetzte optionale Felder rauswerfen (False/0 bleiben erhalten)
        return assistant.dispatch(name, {k: v for k, v in kwargs.items()
                                         if v is not None and v != ""})

    impl.__name__ = name
    impl.__qualname__ = name
    impl.__doc__ = description
    impl.__signature__ = inspect.Signature(params, return_annotation=str)
    impl.__annotations__ = dict(annotations, **{"return": str})
    return impl


def register_tools(server: FastMCP = mcp) -> list[str]:
    """Alle Buchhaltungs-Tools am Server registrieren. Gibt die Namen zurück."""
    names = []
    for t in assistant.tools_for():
        fn = t["function"]
        if fn["name"] in EXCLUDED:
            continue
        server.add_tool(_build(fn["name"], fn["description"], fn.get("parameters", {})),
                        name=fn["name"], description=fn["description"])
        names.append(fn["name"])
    return names


register_tools()


if __name__ == "__main__":
    # Erst beim Start binden, nicht beim Import: sonst bricht schon das
    # blosse Importieren des Moduls ab, wenn mehrere Profile existieren.
    core.profil_aus_umgebung()
    mcp.run()
