import os, yaml, argparse, re, sys, httpx
from apigee_header import dict_to_obj, header
import asyncio
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

# Local Fiddler proxy for HTTP traffic inspection
proxies = "http://127.0.0.1:8888"

# MCP server URL — replace with your own MCP server endpoint
MCP_SERVER_URL = "https://your-mcp-server.example.com/mcp"


def llm_model(header, yaml_file, model):
    access_token, request_headers = header
    llm = ChatOpenAI(
        api_key=f"{access_token}",
        base_url=yaml_file.api.base_url,
        model=model,
        default_headers=request_headers,
        http_async_client=httpx.AsyncClient(verify=False),
        http_client=httpx.Client(verify=False),
    )
    return llm


def custom_httpx_client_factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=False,
        timeout=300,
        proxy=proxies,
    )


async def main(yaml, llm):
    # =========================================================================
    # FIXED VERSION — uses client.session() to maintain a persistent MCP session
    # All tool calls within this block share the SAME session, eliminating
    # the chatter caused by MultiServerMCPClient's default stateless behavior.
    #
    # Reference: LangChain docs explicitly recommend client.session() for
    # stateful MCP usage. See GitHub issues #178 and #207 on
    # langchain-ai/langchain-mcp-adapters.
    # =========================================================================

    client = MultiServerMCPClient(
        {
            "mcp_server": {
                "url": MCP_SERVER_URL,
                "transport": "streamable_http",
                "httpx_client_factory": custom_httpx_client_factory,
            }
        }
    )

    # Open ONE persistent session — all tool calls reuse this connection
    async with client.session("mcp_server") as session:
        # Load MCP tools wired to the persistent session
        tools = await load_mcp_tools(session)

        # Build the agent with tools that share the session
        agent = create_react_agent(llm, tools)

        response = await agent.ainvoke({
            "messages": [
                ("system", yaml.prompt.system),
                ("user", yaml.prompt.user.format(question=yaml.prompt.question))
            ]
        })

    # Session auto-closes here when async with exits

    print(response['messages'][-1].content)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the YAML configuration file"
    )

    args = parser.parse_args()
    experiment_name = re.sub(".yaml$", "", os.path.basename(args.config))
    print(experiment_name)

    try:
        with open(args.config, 'r', encoding='utf-8') as file:
            config_dict = yaml.safe_load(file)
    except FileNotFoundError:
        print(f"Error: Config file '{args.config}' not found")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML: {e}")
        sys.exit(1)

    yaml_file = dict_to_obj(config_dict)
    llm_generation = llm_model(header=header(yaml_file), yaml_file=yaml_file, model=yaml_file.api.model)
    asyncio.run(main(yaml=yaml_file, llm=llm_generation))
    print()
