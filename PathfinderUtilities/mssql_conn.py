"""SQL Server auth for local scripts.

Windows uses Trusted_Connection. Other platforms, including SQL Server in
Docker, use MSSQL_USER and MSSQL_PASSWORD.
"""

import os
import sys


def sql_auth() -> str:
    if sys.platform == "win32":
        return "Trusted_Connection=yes;"
    user = os.environ.get("MSSQL_USER", "sa")
    password = os.environ.get("MSSQL_PASSWORD", "")
    return f"UID={user};PWD={password};"
