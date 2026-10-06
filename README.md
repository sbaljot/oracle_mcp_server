# Oracle MCP Server

This project provides a Model Context Protocol (MCP) workflow for Oracle database testing. It clones a target database object and its dependencies into an isolated test schema, executes assertion-based validations, and saves test results in an Excel workbook.

The solution combines:

- a Python MCP server that exposes workflow tools,
- a SQLcl MCP-backed agent that connects to a saved Oracle session and performs schema operations,
- Azure OpenAI for reasoning over graph metadata, source SQL, and generated assertions,
- a client wrapper for invoking the workflow from the command line.

## What the project does

The code in this repository is designed for a workflow like this:

1. Select an Oracle object such as a procedure, package, or function.
2. Inspect dependency and control-flow metadata from the project JSON snapshots.
3. Clone the object and its required dependencies into the isolated schema `mcp_test_user`.
4. Use generated assertions to drive test setup and verification.
5. Execute SQL/PLSQL in the test schema.
6. Return a markdown test table and export the results as an Excel file.

This is especially useful for validating Oracle logic in a controlled, non-production schema without modifying the original objects.

## Repository layout

- `mcp_server.py` — the MCP server exposing the workflow tools.
- `mcp_client.py` — command-line client used to call the server tools.
- `sqlcl_mcp_agent.py` — SQLcl + Azure OpenAI orchestration for cloning and executing test cases.
- `run_llm_prompt.py` — small helper for running a direct Azure OpenAI prompt with project reference data.
- `cleanup.sql` — destructive cleanup script for objects in the isolated test schema.
- `graph_snapshot.json` / `dependency_graph.json` — dependency and execution graph data used during cloning and test execution.
- `assertions_generated.json` — generated business-rule assertions for the target objects.
- `generate_test_results/` — output folder where generated Excel files are saved.

## MCP tools exposed

The MCP server exposes the following tools:

### `create_test_environment`

Creates a cloned Oracle object and all required dependent objects in the fixed test schema `mcp_test_user`.

Parameters:

- `object_name`: Oracle object name or schema-qualified identifier
- `connection_name`: exact saved SQLcl connection name
- `test_schema`: fixed to `mcp_test_user`

### `generate_test_cases`

Runs assertion-based tests against the object in `mcp_test_user` and returns a markdown test table.

Parameters:

- `object_name`
- `connection_name`
- `test_schema` (fixed to `mcp_test_user`)
- `test_goal` (optional prompt text)

The client automatically converts the markdown table into an `.xlsx` file under `generate_test_results/`.

### `cleanup`

Runs the destructive cleanup logic defined in `cleanup.sql` against `MCP_TEST_USER`.

Important:

- this is intentionally restricted,
- it requires `confirm=true`,
- it verifies that the active Oracle session user is the expected schema before proceeding.

## Workflow overview

### 1. Test-environment creation

The server calls `run_clone()` in `sqlcl_mcp_agent.py`, which:

- loads the graph snapshot and dependency graph,
- reads the source SQL and generated assertions,
- connects to SQLcl via the saved connection,
- uses Azure OpenAI with bound MCP tools to create the needed schema objects in `mcp_test_user`,
- validates that the target environment has been created before returning.

### 2. Test generation and execution

The server calls `run_test_cases()`, which:

- connects to the saved SQLcl session,
- inspects the `mcp_test_user` schema,
- uses the available SQL and SQLcl MCP tools to arrange fixtures, execute SQL, and validate assertions,
- requires every supplied assertion to be covered in order,
- returns a markdown table with columns:
  - `#`
  - `Assertion`
  - `Test input / method`
  - `Result`

The CLI client then parses the table and saves it to Excel.

## Required environment

This project expects a configured Oracle and Azure environment.

### Oracle

Set these in a `.env` file at the project root:

```env
ORACLE_DSN=<your_oracle_connect_string>
ORACLE_TEST_PASSWORD=<password_for_mcp_test_user>
ORACLE_CLIENT_LIB_DIR=<optional_oracle_client_lib_path>
```

The project also expects a saved SQLcl connection such as:

```bash
sqlcl
conn -save ncrdev -savepwd user/password@connect_string
```

### Azure OpenAI

Configure the model used by the orchestration layer:

```env
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT=
AZURE_OPENAI_API_VERSION=
```

### SQLcl

If SQLcl is not in the default location, set:

```env
SQLCL_PATH=/path/to/sqlcl/bin/sql.exe
```

## Python dependencies

This project relies on a Python environment with packages such as:

- `oracledb`
- `python-dotenv`
- `langchain-openai`
- `mcp`
- `openpyxl`

Install the project dependencies in your active Python environment before using the scripts.

## Usage

### List available MCP tools

```bash
python mcp_client.py --list-tools
```

### Create the isolated environment for a target object

```bash
python mcp_client.py \
  --tool create_test_environment \
  --arguments '{"object_name":"NATL_WORKDAY_ACC_GL_FILE_LOAD_PROC","connection_name":"ncrdev"}'
```

### Generate and execute test cases

```bash
python mcp_client.py \
  --tool generate_test_cases \
  --arguments '{"object_name":"NATL_WORKDAY_ACC_GL_FILE_LOAD_PROC","connection_name":"ncrdev","test_goal":"Validate the main GL load procedure using the generated assertions."}'
```

This creates an `.xlsx` report in `generate_test_results/`.

### Cleanup the isolated test schema

```bash
python mcp_client.py \
  --tool cleanup \
  --arguments '{"expected_schema":"mcp_test_user","confirm":true}'
```

> Warning: cleanup is destructive. It permanently removes supported objects from the test schema after explicit confirmation.

## Notes on safety and scope

- The application is intentionally restricted to the schema `mcp_test_user`.
- The cleanup tool checks the connected user before executing destructive SQL.
- The workflow uses Oracle metadata and generated assertion data to minimize accidental execution against production schemas.
- The project is a research and test-enablement tool rather than a general-purpose database migration framework.

## Typical use case

This project is meant for Oracle PL/SQL verification where you want to:

- clone a database procedure and its dependencies into a disposable environment,
- validate behavior against natural-language assertions derived from code,
- generate reproducible results in a spreadsheet-friendly format,
- keep the original production schema untouched.

## License

This repository does not currently include a license file. Review your organization’s policy before distributing or deploying it beyond the local environment.
