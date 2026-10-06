"""Use Azure OpenAI and SQLcl MCP to clone an Oracle object into a test schema."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DEFAULT_SQLCL_PATH = ROOT / "sqlcl" / "bin" / "sql.exe"
DEFAULT_GRAPH_PATH = ROOT / "graph_snapshot.json"
DEFAULT_DEPENDENCY_GRAPH_PATH = ROOT / "dependency_graph.json"
TEST_SCHEMA = "mcp_test_user"
DEFAULT_ASSERTIONS_PATH = ROOT / "assertions_generated.json"
MAX_TOOL_ROUNDS = 36

SYSTEM_INSTRUCTIONS = """You are executing an Oracle schema-isolated cloning task using
MCP tools. You MUST use tools to take every action -- never describe a plan in
prose instead of calling a tool. Do not stop, summarize, or declare completion
until every table, sequence, and procedure/package listed in the graph
snapshot has actually been created in the mcp_test_user schema.

After creating each object in isolated testing schema, verify it exists (e.g. re-run schema_information
or an appropriate query tool) before moving to the next. If a DDL statement
fails because of a missing dependency (e.g. a foreign key referencing a table
not yet cloned), create that dependency first, then retry -- do not skip it
or report partial success as if it were complete.

Only return a final plain-text response once you have verified every object
from the graph snapshot exists in mcp_test_user. Your final response must
list exactly what was created and confirm it was verified, not just
attempted."""

USER_PROMPT_TEMPLATE = """Object to clone: {object_name}

To test this procedure, create a clone of this procedure and all the other data
objects that are required to execute queries.
To create a clone, use the testing schema command with the name
"mcp_test_user./*" when supported. This may create the schema and grant it
access to required objects.

Use the dependency graph to identify linked database objects and their owners
and types. Use the control-flow graph to understand execution paths.

Create the data objects identified in the dependency graph within the testing schema.

Using the assertions generated, create suitable dummy values that should be inserted in the table
so that it is ready to be executed and tested.

Azure deployment for MCP `model` arguments: {model}

Graph snapshot JSON:
{graph_json}

Dependency graph JSON:
{dependency_graph_json}

Source SQL:
{source_sql}

Assertions generated:
{assertions}
"""

TEST_EXECUTION_INSTRUCTIONS = """You are executing Oracle tests only in the isolated schema `mcp_test_user`.
The SQLcl session is connected using the explicitly supplied saved connection.
The test schema has already been selected and inspected. Use the available
`sql_run`, `sqlcl_run`, and `schema_information` tools to prepare test-only
fixtures, create any required test input files using test-only resources, invoke
the cloned procedure, and verify results against the supplied assertions.

Never write to or execute a procedure/package in the source schema
`NCRNICSERPCSPPRC`. Qualify test objects with `mcp_test_user` where appropriate.
Do not claim a test passed unless the execution and verification tool results
support that conclusion. If external-table or directory setup is unavailable,
report the blocker instead of using a production file or directory.

After executing the tests, return only a Markdown table with exactly these
columns: `#`, `Assertion`, `Test input / method`, and `Result`. Include one row
for every supplied assertion, in the supplied order. In `Result`, begin with
`PASS` or `FAIL`, then briefly state the observed evidence and why it does or
does not satisfy the assertion. Mark a test `FAIL` when setup, execution, or
verification is blocked or errors; do not omit blocked assertions or claim
success without supporting tool results."""


def get_sqlcl_parameters() -> StdioServerParameters:
    sqlcl_path = Path(os.getenv("SQLCL_PATH", str(DEFAULT_SQLCL_PATH)))
    if not sqlcl_path.is_file():
        raise FileNotFoundError(
            f"SQLcl executable not found: {sqlcl_path}. Set SQLCL_PATH to sql.exe."
        )
    return StdioServerParameters(
        command=str(sqlcl_path),
        args=["-mcp"],
        env={
            **os.environ,
            "NLS_LANG": os.getenv("NLS_LANG", "AMERICAN_AMERICA.AL32UTF8"),
        },
    )


def load_graph_context(
    graph_path: Path,
    object_name: str,
    test_schema: str = TEST_SCHEMA,
) -> tuple[str, str]:
    if test_schema != TEST_SCHEMA:
        raise ValueError(f"test_schema is fixed to {TEST_SCHEMA!r}")

    graph_data = json.loads(graph_path.read_text(encoding="utf-8"))
    graph_json = json.dumps(graph_data, indent=2)
    dependency_graph_data = json.loads(
        DEFAULT_DEPENDENCY_GRAPH_PATH.read_text(encoding="utf-8")
    )
    dependency_graph_json = json.dumps(dependency_graph_data, indent=2)

    assertions_data = json.loads(DEFAULT_ASSERTIONS_PATH.read_text(encoding="utf-8"))
    object_assertions = assertions_data.get(object_name)
    if object_assertions is None:
        raise ValueError(f"No generated assertions found for object {object_name!r}")
    assertions = json.dumps(object_assertions, indent=2)

    source_sql = "Source SQL file was not found."
    source = graph_data.get("source")
    if source:
        source_path = Path(source)
        if not source_path.is_file():
            source_path = ROOT / source_path.name
        if source_path.is_file():
            source_sql = source_path.read_text(encoding="utf-8")

    prompt = USER_PROMPT_TEMPLATE.format(
        object_name=object_name,
        test_schema=test_schema,
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        graph_json=graph_json,
        dependency_graph_json=dependency_graph_json,
        source_sql=source_sql,
        assertions=assertions,
    )
    return graph_json, prompt


def make_model() -> AzureChatOpenAI:
    required = (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
    )
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("Missing .env setting(s): " + ", ".join(missing))

    return AzureChatOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        temperature=0,
    )


def to_openai_tools(mcp_tools: list[Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    openai_tools = []
    schemas_by_name = {}
    for tool in mcp_tools:
        tool_data = tool.model_dump(mode="json", by_alias=True)
        name = tool_data["name"]
        parameters = tool_data.get("inputSchema") or {
            "type": "object",
            "properties": {},
        }
        schemas_by_name[name] = parameters
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool_data.get("description") or "",
                    "parameters": parameters,
                },
            }
        )
    return openai_tools, schemas_by_name


def mcp_result_text(result: Any) -> str:
    pieces = []
    for item in result.content:
        if getattr(item, "type", None) == "text":
            pieces.append(item.text)
        else:
            pieces.append(json.dumps(item.model_dump(mode="json"), ensure_ascii=False))
    if getattr(result, "isError", False):
        return "MCP tool returned an error: " + "\n".join(pieces)
    return "\n".join(pieces)


async def run_clone(
    connection_name: str,
    object_name: str,
    graph_path: Path,
    test_schema: str = TEST_SCHEMA,
) -> str:
    _, user_prompt = load_graph_context(graph_path, object_name, test_schema)
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    model = make_model()

    async with stdio_client(get_sqlcl_parameters()) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed_tools = await session.list_tools()
            tool_names = {tool.name for tool in listed_tools.tools}

            if "connect" not in tool_names:
                raise RuntimeError("SQLcl MCP does not advertise the `connect` tool")
            if "schema_information" not in tool_names:
                raise RuntimeError(
                    "SQLcl MCP does not advertise `schema_information`; "
                    "cannot follow the required connect-before-SQL workflow"
                )

            try:
                connect_result = await session.call_tool(
                    "connect",
                    {"connection_name": connection_name, "model": deployment},
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not connect using saved connection {connection_name!r}. "
                    "Create it in SQLcl with: "
                    f"conn -save {connection_name} -savepwd "
                    "<user>/<password>@<connect_string>"
                ) from exc
            if getattr(connect_result, "isError", False):
                raise RuntimeError(
                    mcp_result_text(connect_result)
                    + "\nIf this saved connection does not exist, create it in SQLcl with: "
                    + f"conn -save {connection_name} -savepwd "
                    + "<user>/<password>@<connect_string>"
                )

            schema_result = await session.call_tool(
                "schema_information",
                {"model": deployment, "level": "DETAILED"},
            )
            if getattr(schema_result, "isError", False):
                raise RuntimeError(mcp_result_text(schema_result))

            agent_tools = [tool for tool in listed_tools.tools if tool.name != "connect"]
            openai_tools, schemas_by_name = to_openai_tools(agent_tools)
            if not openai_tools:
                raise RuntimeError("SQLcl MCP advertised no callable tools")

            tool_model_forced = model.bind_tools(
                openai_tools,
                tool_choice="required",
                parallel_tool_calls=False,
            )
            tool_model_auto = model.bind_tools(
                openai_tools,
                parallel_tool_calls=False,
            )
            messages = [
                SystemMessage(content=SYSTEM_INSTRUCTIONS),
                HumanMessage(
                    content=(
                        user_prompt
                        + "\n\nConnected schema details from schema_information:\n"
                        + mcp_result_text(schema_result)
                    )
                ),
            ]

            for round_num in range(MAX_TOOL_ROUNDS):
                current_model = tool_model_forced if round_num == 0 else tool_model_auto
                response = await current_model.ainvoke(messages)
                messages.append(response)
                if not response.tool_calls:
                    return str(response.content)

                for tool_call in response.tool_calls:
                    name = tool_call["name"]
                    arguments = dict(tool_call["args"])
                    properties = schemas_by_name.get(name, {}).get("properties", {})
                    if "model" in properties:
                        arguments.setdefault("model", deployment)
                    try:
                        tool_result = await session.call_tool(name, arguments)
                        content = mcp_result_text(tool_result)
                        status = "error" if getattr(tool_result, "isError", False) else "success"
                    except Exception as exc:
                        content = f"MCP call failed: {exc}"
                        status = "error"
                    messages.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=tool_call["id"],
                            name=name,
                            status=status,
                        )
                    )

            raise RuntimeError(
                f"Agent stopped after {MAX_TOOL_ROUNDS} MCP tool-call rounds without a final response"
            )


async def run_test_cases(
    connection_name: str,
    object_name: str,
    graph_path: Path,
    test_schema: str = TEST_SCHEMA,
    test_goal: str = "",
) -> str:
    if test_schema != TEST_SCHEMA:
        raise ValueError(f"test_schema is fixed to {TEST_SCHEMA!r}")

    graph_data = json.loads(graph_path.read_text(encoding="utf-8"))
    graph_json = json.dumps(graph_data, indent=2)
    dependency_data = json.loads(
        DEFAULT_DEPENDENCY_GRAPH_PATH.read_text(encoding="utf-8")
    )
    dependency_json = json.dumps(dependency_data, indent=2)
    assertions_data = json.loads(DEFAULT_ASSERTIONS_PATH.read_text(encoding="utf-8"))
    assertions = assertions_data.get(object_name)
    if assertions is None:
        raise ValueError(f"No generated assertions found for object {object_name!r}")
    assertions_json = json.dumps(assertions, indent=2)

    source_sql = "Source SQL file was not found."
    source = graph_data.get("source")
    if source:
        source_path = Path(source)
        if not source_path.is_file():
            source_path = ROOT / source_path.name
        if source_path.is_file():
            source_sql = source_path.read_text(encoding="utf-8")

    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    model = make_model()

    async with stdio_client(get_sqlcl_parameters()) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed_tools = await session.list_tools()
            tools_by_name = {tool.name: tool for tool in listed_tools.tools}
            required_tools = {"connect", "disconnect", "schema_information", "sql_run", "sqlcl_run"}
            missing_tools = required_tools.difference(tools_by_name)
            if missing_tools:
                raise RuntimeError(
                    "SQLcl MCP is missing required test-execution tool(s): "
                    + ", ".join(sorted(missing_tools))
                )

            connect_result = await session.call_tool(
                "connect",
                {"connection_name": connection_name, "model": deployment},
            )
            if getattr(connect_result, "isError", False):
                raise RuntimeError(mcp_result_text(connect_result))

            try:
                schema_result = await session.call_tool(
                    "schema_information",
                    {
                        "model": deployment,
                        "schema": TEST_SCHEMA,
                        "level": "DETAILED",
                    },
                )
                if getattr(schema_result, "isError", False):
                    raise RuntimeError(mcp_result_text(schema_result))

                # The test agent may execute SQL and inspect the test schema, but
                # cannot switch connections or disconnect before cleanup.
                allowed_tools = [
                    tools_by_name[name]
                    for name in ("schema_information", "sql_run", "sqlcl_run")
                ]
                openai_tools, schemas_by_name = to_openai_tools(allowed_tools)
                tool_model_forced = model.bind_tools(
                    openai_tools,
                    tool_choice="required",
                    parallel_tool_calls=False,
                )
                tool_model_auto = model.bind_tools(
                    openai_tools,
                    parallel_tool_calls=False,
                )
                messages = [
                    SystemMessage(content=TEST_EXECUTION_INSTRUCTIONS),
                    HumanMessage(
                        content=(
                            f"Object under test: {object_name}\n"
                            f"Test schema: {TEST_SCHEMA}\n"
                            f"Test goal: {test_goal or 'Execute every supplied assertion'}\n\n"
                            f"Assertions:\n{assertions_json}\n\n"
                            f"Control-flow graph:\n{graph_json}\n\n"
                            f"Dependency graph:\n{dependency_json}\n\n"
                            f"Source SQL:\n{source_sql}\n\n"
                            "Initial test-schema inspection:\n"
                            + mcp_result_text(schema_result)
                        )
                    ),
                ]
                executed_sql_tools = 0
                tool_errors: list[str] = []

                for round_num in range(MAX_TOOL_ROUNDS):
                    current_model = tool_model_forced if round_num == 0 else tool_model_auto
                    response = await current_model.ainvoke(messages)
                    messages.append(response)
                    if not response.tool_calls:
                        if executed_sql_tools == 0:
                            raise RuntimeError(
                                "The test agent returned without executing any SQL or SQLcl test operation."
                            )

                        final_schema_result = await session.call_tool(
                            "schema_information",
                            {
                                "model": deployment,
                                "schema": TEST_SCHEMA,
                                "level": "DETAILED",
                            },
                        )
                        if getattr(final_schema_result, "isError", False):
                            raise RuntimeError(
                                "Final test-schema verification failed: "
                                + mcp_result_text(final_schema_result)
                            )
                        result_text = str(response.content)
                        if tool_errors:
                            result_text += "\n\nTool operations that returned errors:\n- "
                            result_text += "\n- ".join(tool_errors)
                        result_text += "\n\nFinal test-schema inspection completed."
                        return result_text

                    for tool_call in response.tool_calls:
                        name = tool_call["name"]
                        if name not in {"schema_information", "sql_run", "sqlcl_run"}:
                            raise RuntimeError(f"Test agent requested disallowed tool {name!r}")

                        arguments = dict(tool_call["args"])
                        properties = schemas_by_name.get(name, {}).get("properties", {})
                        if "model" in properties:
                            arguments.setdefault("model", deployment)
                        if name == "schema_information":
                            arguments["schema"] = TEST_SCHEMA
                        if "execution_type" in properties:
                            arguments.setdefault("execution_type", "SYNCHRONOUS")

                        try:
                            tool_result = await session.call_tool(name, arguments)
                            content = mcp_result_text(tool_result)
                            is_error = getattr(tool_result, "isError", False)
                            status = "error" if is_error else "success"
                            if name in {"sql_run", "sqlcl_run"} and not is_error:
                                executed_sql_tools += 1
                            if is_error:
                                tool_errors.append(f"{name}: {content}")
                        except Exception as exc:
                            content = f"MCP call failed: {exc}"
                            status = "error"
                            tool_errors.append(f"{name}: {content}")

                        messages.append(
                            ToolMessage(
                                content=content,
                                tool_call_id=tool_call["id"],
                                name=name,
                                status=status,
                            )
                        )

                raise RuntimeError(
                    f"Test agent stopped after {MAX_TOOL_ROUNDS} tool-call rounds without a final response"
                )
            finally:
                disconnect_result = await session.call_tool(
                    "disconnect",
                    {"model": deployment},
                )
                if getattr(disconnect_result, "isError", False):
                    raise RuntimeError(
                        "Test execution finished, but SQLcl disconnect failed: "
                        + mcp_result_text(disconnect_result)
                    )


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use Azure OpenAI and SQLcl MCP to clone an object into mcp_test_user."
    )
    parser.add_argument("--object", required=True, help="Procedure or data-object name to clone")
    parser.add_argument(
        "--test-schema",
        default=TEST_SCHEMA,
        choices=(TEST_SCHEMA,),
        help=f"Fixed destination test schema: {TEST_SCHEMA}",
    )
    parser.add_argument(
        "--connection",
        required=True,
        help="Exact, case-sensitive saved SQLcl connection name",
    )
    parser.add_argument(
        "--graph",
        type=Path,
        default=DEFAULT_GRAPH_PATH,
        help=f"Graph snapshot JSON (default: {DEFAULT_GRAPH_PATH.name})",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str]) -> None:
    args = parse_arguments(argv)
    print(await run_clone(args.connection, args.object, args.graph, args.test_schema))


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except Exception as exc:
        print(f"Clone request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

