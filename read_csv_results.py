"""Expose Oracle test-environment and test-case tools over MCP stdio."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import oracledb
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from sqlcl_mcp_agent import (
    DEFAULT_GRAPH_PATH,
    run_clone,
    run_test_cases,
)


ROOT = Path(__file__).resolve().parent
server = MCPServer(
    name="oracle-workflow",
    instructions=(
        "Create an isolated Oracle test environment and execute assertion-based "
        "tests in the fixed mcp_test_user schema."
    ),
)

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")
_QUALIFIED_IDENTIFIER = re.compile(
    r"[A-Za-z][A-Za-z0-9_$#]{0,127}(?:\.[A-Za-z][A-Za-z0-9_$#]{0,127})*"
)
TEST_SCHEMA = "mcp_test_user"
CLEANUP_SQL_PATH = ROOT / "cleanup.sql"
load_dotenv(ROOT / ".env")
ORACLE_USER = "mcp_test_user"
ORACLE_PASSWORD = os.getenv("ORACLE_TEST_PASSWORD")
ORACLE_DSN = os.getenv("ORACLE_DSN")
ORACLE_CLIENT_LIB_DIR = os.getenv("ORACLE_CLIENT_LIB_DIR")

if ORACLE_CLIENT_LIB_DIR and oracledb.is_thin_mode():
    oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_LIB_DIR)


def validate_object_name(object_name: str) -> str:
    if not _QUALIFIED_IDENTIFIER.fullmatch(object_name):
        raise ValueError("object_name must be a simple or schema-qualified Oracle identifier")
    return object_name


def validate_test_schema(test_schema: str) -> str:
    if test_schema != TEST_SCHEMA:
        raise ValueError(f"test_schema is fixed to {TEST_SCHEMA!r}")
    return TEST_SCHEMA


@server.tool(
    name="create_test_environment",
    description=(
        "Clone a database procedure and its required dependent objects into an "
        "isolated test schema using SQLcl MCP. Returns the execution result. "
        "This can create a schema and grant privileges; only use an intended "
        "development or test database connection."
    ),
)
async def create_test_environment(
    object_name: str,
    connection_name: str,
    test_schema: str = TEST_SCHEMA,
) -> str:
    """Clone the selected database object and dependencies into a test schema."""
    object_name = validate_object_name(object_name)
    test_schema = validate_test_schema(test_schema)
    if not connection_name.strip():
        raise ValueError("connection_name must be the exact saved SQLcl connection name")

    return await run_clone(
        connection_name=connection_name,
        object_name=object_name,
        graph_path=DEFAULT_GRAPH_PATH,
        test_schema=test_schema,
    )


@server.tool(
    name="generate_test_cases",
    description=(
        "Execute assertion-based tests for a database object in mcp_test_user. "
        "This connects to the supplied saved connection and may run SQL/PLSQL, "
        "insert test fixture data, or create test-only files. It disconnects "
        "when finished."
    ),
)
async def generate_test_cases(
    object_name: str,
    connection_name: str,
    test_schema: str = TEST_SCHEMA,
    test_goal: str = "",
) -> str:
    """Execute assertion-based scenarios and return tool-backed results."""
    object_name = validate_object_name(object_name)
    test_schema = validate_test_schema(test_schema)
    if not connection_name.strip():
        raise ValueError("connection_name must be the exact saved SQLcl connection name")

    return await run_test_cases(
        connection_name=connection_name,
        object_name=object_name,
        graph_path=DEFAULT_GRAPH_PATH,
        test_schema=test_schema,
        test_goal=test_goal,
    )


def _load_cleanup_block() -> str:
    script_lines = CLEANUP_SQL_PATH.read_text(encoding="utf-8").splitlines()
    script_lines = [
        line
        for line in script_lines
        if line.strip().upper().rstrip(";") != "SET SERVEROUTPUT ON"
    ]
    while script_lines and script_lines[-1].strip() == "/":
        script_lines.pop()

    block = "\n".join(script_lines).strip()
    if not block.upper().startswith("DECLARE"):
        raise ValueError("cleanup.sql must contain the expected anonymous PL/SQL block")
    return block


def _execute_cleanup(expected_schema: str) -> str:
    missing = []
    if not ORACLE_DSN:
        missing.append("ORACLE_DSN")
    if not ORACLE_PASSWORD:
        missing.append("ORACLE_TEST_PASSWORD in .env")
    if missing:
        raise RuntimeError("Missing .env setting(s): " + ", ".join(missing))

    plsql_block = _load_cleanup_block()
    output_lines = []
    with oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT USER FROM dual")
            connected_user = cursor.fetchone()[0]
            if connected_user.upper() != expected_schema.upper():
                raise PermissionError(
                    f"Refusing cleanup: connected as {connected_user}, "
                    f"expected {expected_schema}."
                )

            cursor.callproc("DBMS_OUTPUT.ENABLE", [None])
            cursor.execute(plsql_block)

            line_var = cursor.var(oracledb.DB_TYPE_VARCHAR, size=32767)
            status_var = cursor.var(oracledb.DB_TYPE_NUMBER)
            while True:
                cursor.callproc("DBMS_OUTPUT.GET_LINE", [line_var, status_var])
                if status_var.getvalue() != 0:
                    break
                output_lines.append(line_var.getvalue())

    if not output_lines:
        return f"Cleanup block completed for {expected_schema}; it produced no DBMS_OUTPUT."
    failures = [line for line in output_lines if line.startswith("Failed:")]
    summary = "Cleanup completed" if not failures else f"Cleanup completed with {len(failures)} failure(s)"
    return f"{summary} for {expected_schema}.\n" + "\n".join(output_lines)


@server.tool(
    name="cleanup",
    description=(
        "Destructively drop the supported user objects listed by cleanup.sql "
        "from MCP_TEST_USER. Requires confirm=true and a direct Oracle connection "
        "whose session user is MCP_TEST_USER. Tables are purged."
    ),
)
async def cleanup(expected_schema: str, confirm: bool = False) -> str:
    """Run cleanup.sql only after explicit confirmation and schema identity check."""
    if expected_schema.upper() != TEST_SCHEMA.upper():
        raise ValueError(f"Cleanup is restricted to {TEST_SCHEMA!r}")
    if confirm is not True:
        raise ValueError("Set confirm=true to authorize destructive schema cleanup")
    return await asyncio.to_thread(_execute_cleanup, expected_schema)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()