from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END, START
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from langchain_core.messages import  ToolMessage
import json
import sqlglot
from langchain_core.tools import tool
import sqlglot.errors
from decouple import config
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field




class State(TypedDict):
    question: str
    messages: list
    results: dict
    final_sql: str
    counter: int


prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(
        """
        You are an assistant used to test a SQL validation router.

        Your workflow ALWAYS has two phases:

        PHASE 1 — ERROR TRIGGERING (LLM → TOOL CALL)
        When the user provides an input string describing an error category or keyword
        (e.g., "syntax error", "hallucinated field", "select *", "invalid date", etc.),
        you MUST:

        1. Pass message as is to the tool.
        2. Immediately call the tool: fake_sql_validator with the parameter "question"
        set to the message you generated.

        PHASE 2 — ERROR RESOLUTION (AFTER TOOL RETURNS)
        After receiving the tool’s output containing error_type and error_details,
        you must generate a corrected SQL-related message that avoids triggering ANY error.

        IMPORTANT:
        - PHASE 2 MUST return ONLY the corrected SQL string.
        - Do NOT output the validation dictionary.
        - Do NOT output explanations.
        - Do NOT wrap the SQL in JSON.
        - Output ONLY the corrected SQL.

        RULES:
        - ALWAYS call the tool during Phase 1.
        - The tool call MUST use this key: "question": "<your generated message>".
        - Do NOT explain your reasoning.
        - Output only the required raw SQL string during Phase 2.
        """
    ),

    HumanMessagePromptTemplate.from_template(
        """
        Based on the following user input, generate an ERROR-TRIGGERING SQL-related message
        and then call fake_sql_validator with that message.

        After the tool result is returned, output ONLY the corrected SQL string.

        {question}
        """
    )
])


class FakeError(BaseModel):
    error_present: bool
    error_type: str
    error_details: str


class SqlValidationOutput(BaseModel):
    sql: str = Field(..., description="The generated SQL")

@tool(args_schema=SqlValidationOutput)
def fake_sql_validator(sql: str) -> FakeError:
    """
    A simplified SQL validator used ONLY for LangGraph router testing.
    It mimics every error type visible in your screenshots.
    """
    q = sql.lower()
    print(q)
    # 1. SYNTAX ERROR
    if "syntax" in q or "parse" in q:
        return FakeError(
            error_present=True,
            error_type="syntax_error",
            error_details="SQL syntax error detected"
        )

    # 2. INVALID FIELD
    if "hallucinated" in q or "hullicated" in q or "invalid field" in q:
        return FakeError(
            error_present=True,
            error_type="invalid_field",
            error_details="Field does not exist in schema"
        )

    # 3. SELECT STAR
    if "select *" in q or "star" in q:
        return FakeError(
            error_present=True,
            error_type="select_star",
            error_details="SELECT * is not allowed"
        )

    # 4. BAD DATE / PLACEHOLDER DATE
    if "placeholder date" in q or "bad date" in q or "invalid date" in q:
        return FakeError(
            error_present=True,
            error_type="invalid_date",
            error_details="Date format invalid or placeholder date detected"
        )

    # 5. LIMIT EXCEEDS ALLOWED
    if "limit too high" in q or "limit error" in q or "limit" in q:
        return FakeError(
            error_present=True,
            error_type="limit_error",
            error_details="Query LIMIT exceeds allowed threshold"
        )

    # 6. ILLEGAL / RESTRICTED OPS
    if any(op in q for op in [
        "insert", "update", "delete", "merge", "grant", "create", "drop", "alter"
    ]):
        return FakeError(
            error_present=True,
            error_type="restricted_op",
            error_details="Restricted SQL operation detected"
        )

    # 7. DEFAULT — NO ERROR
    return FakeError(
        error_present=False,
        error_type="none",
        error_details="No errors found"
    )


llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0, api_key=config("openAi"))
agent = llm.bind_tools([fake_sql_validator])


def create_prompt(state: State):
    dic = {'question': state['question']}
    user_provided_prompt = prompt.format_messages(**dic)
    return {
        'messages': user_provided_prompt,
        # 'question': state['question'],
        # 'results': state['results']
    }

def llm_output(state: State):
    response = agent.invoke(state['messages'])
    counter = state['counter'] + 1
    return {
        'messages': state['messages'] + [response],
        'question': state['question'],
        'results': state['results'],
        'counter': counter
    }

def sql_value(state: State):
    if state['counter'] >=3:
        final_sql = "exhaust tool limit"
    else:
        final_sql = state['messages'][-1].content
    return {
        'final_sql': final_sql
    }


def tool_node(state: State):
    last = state["messages"][-1]
    call = last.tool_calls[0]


    results = fake_sql_validator.invoke(call['args'])
    tool_msg = ToolMessage(content=json.dumps(results.model_dump()),
                           name = call["name"],
                           tool_call_id=call["id"]
                           )

    return {"messages": state["messages"] + [tool_msg],
            "question": state['question'],
            "results": results
           }

def should_call_tool(state: State):
    if state['counter'] < 3:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "has_tool_call"

    return "no_tool_call"



def tool_router_node(state: State):
    result = state["results"]
    print(result)
    if result.error_type == "restricted_op":
        return "stop"

    if state["counter"] >= 3:  # Add this check
        return "done"

    if result.error_present:
        return "continue"
    return "done"


if __name__ == '__main__':
    graph = StateGraph(State)
    graph.add_node("create_prompt", create_prompt)
    graph.add_node("llm_output", llm_output)
    graph.add_node("tool_node", tool_node)
    graph.add_node("sql_value", sql_value)
    # graph.add_node("tool_node", tool_router_node)

    graph.add_edge(START, "create_prompt")
    graph.add_edge( "create_prompt", "llm_output")
    graph.add_conditional_edges(
        "llm_output",
        should_call_tool,
        {
            "has_tool_call": "tool_node",
            "no_tool_call": "sql_value"
        }
    )
    graph.add_conditional_edges("tool_node",
                                tool_router_node,
                                {"done": "llm_output",
                                 'continue': 'llm_output',
                                 'stop': END
                                 }
                                )
    graph.add_edge( "sql_value", END)
    compiled_graph = graph.compile()
    temp = compiled_graph.invoke({'question': 'This SQL has a parse error',
                                  'messages': [],
                                  'results': {},
                                  'final_sql': '',
                                  'counter': 0,
                                  })
    print(compiled_graph.get_graph().draw_mermaid())
    print(temp['messages'][-1].content)
