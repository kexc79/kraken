"""Offline readiness check. Never connects to Oracle, OpenAI or Google."""
import importlib
import sys
from pathlib import Path

from google.oauth2.service_account import Credentials
import oracledb

from app_config import load_config


def main():
    try:
        settings = load_config()
        for name in ("gspread", "openai", "oracledb", "dotenv"):
            importlib.import_module(name)
        import os
        oracledb.init_oracle_client(lib_dir=os.environ["ORACLE_CLIENT"])
        print("Python dependencies and Oracle Thick client: OK", flush=True)
        path = Path(settings["GSERVICE_JSON"])
        if not path.is_file():
            print(f"NOT READY: add the Google service account JSON: {path}", flush=True)
            return 2
        Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        print("Configuration and Google private key: OK (offline check)", flush=True)
        return 0
    except Exception as error:
        # Do not print exception text: parsers may include credential values.
        print(f"NOT READY: invalid configuration or dependencies ({type(error).__name__})", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
