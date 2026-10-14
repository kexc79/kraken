"""Runtime settings; secrets stay in mounted files, never in the image."""
import ast
import os
import re
from pathlib import Path

from dotenv import dotenv_values


WORKSHEET_KEYS = ("WORKSHEET_NAME_MAIN", "WORKSHEET_NAME_UNMATCH", "WORKSHEET_NAME_AMBIG")


def resolve_worksheet_names(values):
    """Resolve worksheet references, without evaluating code or touching secrets."""
    resolved = {}
    active = set()

    def resolve(key):
        if key in resolved:
            return resolved[key]
        if key in active:
            raise RuntimeError(f"Circular worksheet reference: {key}")
        active.add(key)
        value = values[key]
        # Only worksheet names support interpolation; passwords remain literal.
        value = re.sub(
            r"\$\{(WORKSHEET_NAME_(?:MAIN|UNMATCH|AMBIG))\}",
            lambda match: resolve(match[1]), value,
        )
        if re.match(r"^\s*WORKSHEET_NAME_(?:MAIN|UNMATCH|AMBIG)\s*\+", value):
            def concatenate(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value
                if isinstance(node, ast.Name) and node.id in WORKSHEET_KEYS:
                    return resolve(node.id)
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    return concatenate(node.left) + concatenate(node.right)
                raise RuntimeError(f"Unsupported worksheet expression: {key}")

            try:
                value = concatenate(ast.parse(value.strip(), mode="eval").body)
            except SyntaxError:
                raise RuntimeError(f"Invalid worksheet expression: {key}") from None
        active.remove(key)
        resolved[key] = value
        return value

    return {key: resolve(key) for key in WORKSHEET_KEYS}


def load_config():
    directory = Path(os.environ.get("APP_CONFIG_DIR", Path(__file__).parent)).resolve()
    values = {}
    for name in (".env", ".req"):
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f"Missing configuration file: {path}")
        # Literal passwords may contain $, # and ${...}; never expand them.
        values.update(dotenv_values(path, encoding="utf-8-sig", interpolate=False))
    required = (
        "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN", "OPENAI_API_KEY",
        "GSERVICE_JSON", "GSHEET_ID", "WORKSHEET_NAME_MAIN",
        "WORKSHEET_NAME_UNMATCH", "WORKSHEET_NAME_AMBIG",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError("Missing settings: " + ", ".join(missing))
    values.update(resolve_worksheet_names(values))
    credentials = Path(values["GSERVICE_JSON"])
    if not credentials.is_absolute():
        credentials = directory / credentials
    values["GSERVICE_JSON"] = str(credentials)
    return {key: values[key] for key in required}
