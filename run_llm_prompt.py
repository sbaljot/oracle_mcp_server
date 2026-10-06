"""Run an Azure OpenAI system prompt and print the model output.

Examples:
    python run_llm_prompt.py --system-prompt "Return JSON only" --user-input "Hello"
    python run_llm_prompt.py --system-file prompt.txt --user-file input.txt
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import AzureChatOpenAI


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

REFERENCE_FILES = {
    "source_sql": ROOT / "NATL_WORKDAY_AAPAY_GL_DATA_PKG.sql",
    "graph_snapshot": ROOT / "graph_snapshot.json",
    "assertions": ROOT / "assertions_generated.json",
    "code_fetched": ROOT / "code_fetched.json",
}

# Edit these values directly when running the script without command-line inputs.
SYSTEM_PROMPT = """
You are an Oracle PL/SQL test-generation expert.
Return only valid Oracle SQL.
Do not generate explanations.
Use :file_name as the input bind variable.
"""
USER_INPUT = """
Create the SQL test case for this assertion based on the fetched code but can refer to source sql, graph snapshot, and assertions to make it more accurate:
The procedure loads data from an external source file into the main GL table, setting the status of each inserted record to 'I' (indicating inserted).
Use the referenced source code, graph, assertions, and fetched code below.

Source SQL:
{source_sql}

Graph snapshot:
{graph_snapshot}

Assertions:
{assertions}

Fetched code:
{code_fetched}

Use AAA approach to implement this.
In the arrange block make temporary environment and variables to set it up.
In act call the procedure being tested.
In assert verify the result.
"""


def read_value(value: str | None, file_path: str | None, name: str, default: str) -> str:
    if value is not None and file_path is not None:
        raise ValueError(f"Use either --{name} or --{name}-file, not both")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    if value is not None:
        return value
    return default


def load_reference_files() -> dict[str, str]:
    missing = [str(path) for path in REFERENCE_FILES.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing reference file(s): " + ", ".join(missing))
    return {
        name: path.read_text(encoding="utf-8")
        for name, path in REFERENCE_FILES.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an Azure OpenAI prompt")
    parser.add_argument("--system-prompt")
    parser.add_argument("--system-file")
    parser.add_argument("--user-input")
    parser.add_argument("--user-file")
    args = parser.parse_args()

    system_prompt = read_value(
        args.system_prompt,
        args.system_file,
        "system-prompt",
        SYSTEM_PROMPT,
    )
    user_input = read_value(
        args.user_input,
        args.user_file,
        "user-input",
        USER_INPUT,
    )
    references = load_reference_files()
    user_input = user_input.format(**references)

    required = (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))

    model = AzureChatOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        temperature=0,
    )

    response = model.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_input),
    ])
    print(response.content)


if __name__ == "__main__":
    main()