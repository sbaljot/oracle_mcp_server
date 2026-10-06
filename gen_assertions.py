import json
import os
from pathlib import Path

import oracledb
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

'''QUERY USED:
SELECT
    DBMS_METADATA.GET_DDL(
        CASE o.object_type
            WHEN 'PACKAGE'      THEN 'PACKAGE_SPEC'
            WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
            ELSE o.object_type
        END,
        o.object_name,
        o.owner
    ) AS ddl
FROM all_objects o
WHERE o.owner in ('NCRNICSERPCSPPRC','CMU')
  AND o.object_name in ('NATL_WORKDAY_ACC_GL_FILE_LOAD_PROC','NATL_WORKDAY_PAY_GL_DATA_PKG','NATL_WORKDAY_PAY_GL_FILE_LOAD_PROC','NCR_COMMON_MAPPING_UTILITY',
  'NCR_COMMON_UTILITY_PKG','NCR_CSV_PKG')
  AND o.object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY', 'VIEW', 'TRIGGER');
'''

load_dotenv()

# Set ORACLE_CLIENT_LIB_DIR only if the DB requires thick mode (e.g. older auth); omit for thin mode.
if os.getenv("ORACLE_CLIENT_LIB_DIR"):
    oracledb.init_oracle_client(lib_dir=os.environ["ORACLE_CLIENT_LIB_DIR"])

DDL_CACHE_PATH = Path(__file__).with_name("code_fetched.json")
TABLE_DDL_CACHE_PATH = Path(__file__).with_name("table_code_fetched.json")
ASSERTIONS_OUTPUT_PATH = Path(__file__).with_name("assertions_generated.json")

MAP_USER_PROMPT = """You are analyzing Oracle PL/SQL source code for object {object_name}.
Based only on the code shown below, identify the business rules this
code enforces: status transitions, table updates, and error-handling
behavior. Do not speculate about code you cannot see.

Return each rule as a plain-English assertion a non-programmer could
review and approve or reject — not as test code in any framework.

For each assertion, include:
- a confidence level (high / medium / low)
- a category (happy_path / error_handling / data_validation / edge_case)
- a trace object that records the branch outcomes required for the assertion.
    Each trace key must be a short, stable branch-point identifier from the
    source code, and each value must be that branch's outcome. For example:
    {{"inserted_record_count": "nonzero"}}. Use an empty object when the
    assertion does not depend on a conditional branch.

Return ONLY a JSON array with fields: assertion, source_lines,
confidence, category, trace."""

llm = AzureChatOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    temperature=0,
)


def generate_assertions_for_object(object_name: str, ddl: str, user_prompt: str = MAP_USER_PROMPT) -> str:
    """Map step: one LLM call per object, scoped to that object's own DDL."""
    prompt = user_prompt.format(object_name=object_name)

    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=ddl),
    ])
    return response.content


def parse_assertions_response(response_text: str, object_name: str) -> list[dict]:
    response_text = response_text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("\n```", 1)[0]

    assertions = json.loads(response_text)
    if not isinstance(assertions, list):
        raise ValueError(f"LLM response for {object_name} must be a JSON array")

    return assertions


def traces_compatible(trace_a: dict, trace_b: dict) -> bool:
    """Return whether two assertion branch traces can share one execution path."""
    shared_points = set(trace_a) & set(trace_b)
    return all(trace_a[point] == trace_b[point] for point in shared_points)


def assign_path_ids(assertions: list[dict], object_name: str) -> list[dict]:
    """Assign each assertion to a greedy clique of mutually compatible traces."""
    groups: list[list[dict]] = []

    for assertion in assertions:
        trace = assertion.get("trace", {})
        if not isinstance(trace, dict):
            raise ValueError(f"Assertion trace for {object_name} must be a JSON object")

        for group in groups:
            if all(traces_compatible(trace, member["trace"]) for member in group):
                group.append({"assertion": assertion, "trace": trace})
                break
        else:
            groups.append([{"assertion": assertion, "trace": trace}])

    for group_number, group in enumerate(groups, start=1):
        path_id = f"{object_name}:path_{group_number}"
        for member in group:
            member["assertion"]["path_id"] = path_id

    return assertions


def load_ddl_cache(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _clob_output_type_handler(cursor, metadata):
    """Fetch CLOB columns as plain str instead of LOB objects."""
    if metadata.type_code is oracledb.DB_TYPE_CLOB:
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)


def fetch_ddl_from_oracle(objects: list[tuple[str, str]]) -> dict:
    """Run DBMS_METADATA.GET_DDL for the given (name, owner) pairs directly against Oracle."""
    if not objects:
        return {}

    binds = {}
    pairs_sql = []
    for i, (name, owner) in enumerate(objects):
        pairs_sql.append(f"(:owner{i}, :name{i})")
        binds[f"owner{i}"] = owner
        binds[f"name{i}"] = name

    query = f"""
        SELECT
            o.object_name AS name,
            DBMS_METADATA.GET_DDL(
                CASE o.object_type
                    WHEN 'PACKAGE'      THEN 'PACKAGE_SPEC'
                    WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
                    ELSE o.object_type
                END,
                o.object_name,
                o.owner
            ) AS ddl
        FROM all_objects o
        WHERE (o.owner, o.object_name) IN ({", ".join(pairs_sql)})
          AND o.object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY', 'VIEW', 'TRIGGER')
    """

    with oracledb.connect(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=os.environ["ORACLE_DSN"],
    ) as connection:
        connection.outputtypehandler = _clob_output_type_handler
        with connection.cursor() as cursor:
            cursor.execute(query, binds)
            return dict(cursor.fetchall())


def get_ddl_for_packages(packages: list[tuple[str, str]], cache_path: Path) -> dict:
    """Load cached DDL, fetch anything missing from Oracle, and persist the merged cache."""
    ddl_cache = load_ddl_cache(cache_path)

    missing = [(name, owner) for name, owner in packages if name not in ddl_cache]
    if missing:
        ddl_cache.update(fetch_ddl_from_oracle(missing))
        cache_path.write_text(json.dumps(ddl_cache, indent=2), encoding="utf-8")

    return ddl_cache


def fetch_table_ddl_from_oracle(tables: list[tuple[str, str]]) -> dict:
    """Fetch table DDL for table nodes discovered in graph_snapshot.json."""
    if not tables:
        return {}

    binds = {}
    pairs_sql = []
    for i, (name, owner) in enumerate(tables):
        pairs_sql.append(f"(:owner{i}, :name{i})")
        binds[f"owner{i}"] = owner
        binds[f"name{i}"] = name

    query = f"""
        SELECT
            t.table_name AS name,
            DBMS_METADATA.GET_DDL(
                'TABLE',
                t.table_name,
                t.owner
            ) AS ddl
        FROM all_tables t
        WHERE (t.owner, t.table_name) IN ({", ".join(pairs_sql)})
    """

    with oracledb.connect(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=os.environ["ORACLE_DSN"],
    ) as connection:
        connection.outputtypehandler = _clob_output_type_handler
        with connection.cursor() as cursor:
            cursor.execute(query, binds)
            return dict(cursor.fetchall())


def get_table_ddl(tables: list[tuple[str, str]], cache_path: Path) -> dict:
    """Load cached table DDL, fetch missing graph tables, and persist the cache."""
    ddl_cache = load_ddl_cache(cache_path)

    missing = [(name, owner) for name, owner in tables if name not in ddl_cache]
    if missing:
        ddl_cache.update(fetch_table_ddl_from_oracle(missing))
        cache_path.write_text(json.dumps(ddl_cache, indent=2), encoding="utf-8")

    return ddl_cache

with open("graph_snapshot.json", "r", encoding="utf-8") as f:
    data = json.load(f)

if any("id" in node for node in data["nodes"]):
    packages = [
        (node["id"], node.get("owner", "NCRNICSERPCSPPRC"))
        for node in data["nodes"]
        if node.get("type") in ("PACKAGE", "PROCEDURE")
    ]
    tables = [
        (node["id"], node.get("owner", "NCRNICSERPCSPPRC"))
        for node in data["nodes"]
        if node.get("type") == "TABLE"
    ]
elif data.get("source"):
    packages = [(Path(data["source"]).stem, "NCRNICSERPCSPPRC")]
    tables = []
else:
    raise ValueError("graph_snapshot.json does not identify any database objects")

for name, owner in sorted(packages):
    print(name, owner)


def run_map_step(
    packages: list[tuple[str, str]],
    ddl_cache: dict,
    max_concurrency: int = 5,
) -> dict:
    requests = []
    object_names = []

    for name, owner in packages:
        ddl = ddl_cache.get(name)
        if ddl is None:
            print(f"Skipping {name} ({owner}): no DDL found in cache")
            continue

        requests.append(
            [
                SystemMessage(content=MAP_USER_PROMPT.format(object_name=name)),
                HumanMessage(content=ddl),
            ]
        )
        object_names.append(name)

    if not requests:
        raise ValueError("No cached DDL found for the selected database objects")

    responses = llm.batch(
        requests,
        config={"max_concurrency": max_concurrency},
    )

    return {
        name: assign_path_ids(parse_assertions_response(response.content, name), name)
        for name, response in zip(object_names, responses)
    }


if __name__ == "__main__":
    ddl_cache = get_ddl_for_packages(packages, DDL_CACHE_PATH)
    table_ddl_cache = get_table_ddl(tables, TABLE_DDL_CACHE_PATH)
    map_results = run_map_step(packages, ddl_cache)

    ASSERTIONS_OUTPUT_PATH.write_text(
        json.dumps(map_results, indent=2),
        encoding="utf-8",
    )

