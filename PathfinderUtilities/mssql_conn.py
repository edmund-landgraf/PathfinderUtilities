"""SQL Server auth for local scripts.

Windows uses Trusted_Connection. Other platforms, including SQL Server in
Docker, use MSSQL_USER and MSSQL_PASSWORD.
"""

import os
import sys


def sql_auth(sql_login: bool = False) -> str:
    """Windows trusted connection, or a SQL login for Docker on Linux.

    Pass sql_login=True from a --sql-login switch when the script runs on
    Windows but the server is SQL Server in Docker. Trusted connection is
    not available there. Set MSSQL_USER (default sa) and MSSQL_PASSWORD.
    """
    if sys.platform == "win32" and not sql_login:
        return "Trusted_Connection=yes;"
    user = os.environ.get("MSSQL_USER", "sa")
    password = os.environ.get("MSSQL_PASSWORD", "YourStrong!Password123")
    return f"UID={user};PWD={password};"
