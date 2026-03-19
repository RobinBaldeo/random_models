"""
SQL Validation — LangGraph POC

Key patterns:
  - llm_wrapper(agent) returns a generation node (your header pattern)
  - ChatPromptTemplate.partial(feedback="") for the human message
  - Model generates SQL as plain text (no bind_tools)
  - validate_sql node calls sql_validate directly as a function
  - Router checks errors: if present → .partial(feedback=errors) → back to model
                           if clean   → END

Flow:
  START → generate_sql → validate_sql → should_retry →
    ├─ errors  → prepare_retry (.partial) → generate_sql (loop)
    └─ no errors → END
"""

import os
import json
import operator
from typing import Annotated, TypedDict, Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    SystemMessage, HumanMessage, AIMessage,
    BaseMessage, AnyMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
import httpx

# ─── Your real imports ───────────────────────────────────────────────────────
# from model_call import llm_model, header
from validation import sql_validate


# ─── Prompt Template ─────────────────────────────────────────────────────────
# {feedback} starts as "" via .partial(), gets populated with validation
# errors on retry so the human message grows with error context.

PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a SQL expert. Given a user's question about data, generate a valid SQL query.\n"
        "Return ONLY the raw SQL query — no markdown fencing, no explanation.\n"
        "If you receive validation error feedback, fix the SQL based on the errors "
        "and return only the corrected query."
    )),
    ("human", "{question}{feedback}"),
]).partial(feedback="")


# ─── State ───────────────────────────────────────────────────────────────────

class GraphState(TypedDict):
    question: str                                           # original user question
    messages: Annotated[list[AnyMessage], operator.add]     # message history
    generated_sql: str                                      # latest SQL from model
    validation_result: str                                  # JSON from sql_validate
    is_valid: bool                                          # validation passed?


# ─── LLM wrapper (your pattern from model_call.py) ──────────────────────────

def llm_wrapper(agent):
    """Wraps the LLM agent into a LangGraph node function.
    Mirrors your model_call.py pattern where header gets passed to llm_model.
    """
    def generation_sql(state: GraphState) -> dict:
        messages = state.get("messages", [])

        # First run — build from prompt template
        if not messages:
            messages = PROMPT.format_messages(question=state["question"])

        response = agent.invoke(messages)
        generated_sql = response.content.strip()

        return {
            "messages": messages + [AIMessage(content=generated_sql)],
            "generated_sql": generated_sql,
        }

    return generation_sql


# ─── Validation node ─────────────────────────────────────────────────────────

def validate_sql(state: GraphState) -> dict:
    """Calls your sql_validate tool directly as a function (not via ToolNode).
    Passes just sql_query — InjectedToolArg defaults kick in."""
    result_json = sql_validate.invoke({"sql_query": state["generated_sql"]})

    result = json.loads(result_json)

    return {
        "validation_result": result_json,
        "is_valid": result.get("is_valid", False),
    }


# ─── Prepare retry node ─────────────────────────────────────────────────────

def prepare_retry(state: GraphState) -> dict:
    """Uses PROMPT.partial() to inject validation errors into the human message.
    Rebuilds messages from the updated prompt so the model sees:
      SystemMessage: "You are a SQL expert..."
      HumanMessage:  "{question}\n\n<error feedback>"
    """
    result = json.loads(state["validation_result"])
    errors = result.get("errors", [])

    error_text = (
        "\n\nYour previous SQL attempt had these validation errors:\n"
        + "\n".join(f"  - {e}" for e in errors)
        + "\n\nPlease fix these errors and return only the corrected SQL query."
    )

    # Re-partial the prompt with feedback, rebuild messages from scratch
    updated_prompt = PROMPT.partial(feedback=error_text)
    new_messages = updated_prompt.format_messages(question=state["question"])

    return {
        "messages": new_messages,
    }


# ─── Router ──────────────────────────────────────────────────────────────────

def should_retry(state: GraphState) -> Literal["prepare_retry", "end"]:
    """If errors present → retry. If clean → end."""
    if state["is_valid"]:
        return "end"
    return "prepare_retry"


# ─── Build Graph ─────────────────────────────────────────────────────────────

def build_graph(agent):
    graph = StateGraph(GraphState)

    # Nodes
    graph.add_node("generate_sql", llm_wrapper(agent))
    graph.add_node("validate_sql", validate_sql)
    graph.add_node("prepare_retry", prepare_retry)

    # Edges
    graph.add_edge(START, "generate_sql")
    graph.add_edge("generate_sql", "validate_sql")
    graph.add_conditional_edges(
        "validate_sql",
        should_retry,
        {
            "prepare_retry": "prepare_retry",
            "end": END,
        },
    )
    graph.add_edge("prepare_retry", "generate_sql")

    return graph.compile()


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # ── Create the LLM (swap for your Apigee-backed model) ──────────────
    # llm = llm_model(header=header(), model="gemini-2.5-flash")
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    app = build_graph(agent=llm)

    initial_state: GraphState = {
        "question": "Show me all employees in the engineering department",
        "messages": [],
        "generated_sql": "",
        "validation_result": "",
        "is_valid": False,
    }

    print("=" * 60)
    print("  SQL Validation (.partial() + llm_wrapper)")
    print("=" * 60)

    for event in app.stream(initial_state):
        for node_name, node_output in event.items():
            print(f"\n--- {node_name} ---")

            if node_name == "generate_sql":
                print(f"  SQL: {node_output.get('generated_sql', '')}")

            elif node_name == "validate_sql":
                print(f"  Valid: {node_output.get('is_valid')}")
                if not node_output.get("is_valid"):
                    errors = json.loads(
                        node_output.get("validation_result", "{}")
                    ).get("errors", [])
                    for e in errors:
                        print(f"    ✗ {e}")

            elif node_name == "prepare_retry":
                print("  Rebuilding prompt with .partial(feedback=errors)")

    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)