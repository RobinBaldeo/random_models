"""
SQL Validation — LangGraph POC
 
Key patterns:
  - ChatPromptTemplate.partial() for the human message
  - bind_tools([sql_validate]) so the model decides when to call validation
  - ToolNode executes sql_validate when the model emits a tool call
 
Flow:
  START → generate_sql → should_call_tool →
    ├─ tool call → tool_node → END
    └─ no tool call → END
"""
 
import os
import json
import operator
from typing import Annotated, TypedDict, Literal
 
from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    SystemMessage, HumanMessage, AIMessage,
    ToolMessage, BaseMessage, AnyMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
import httpx
 
# ─── Imports for your real setup ─────────────────────────────────────────────
# from model_call import llm_model, header
# from validation import sql_validate  # your real @tool
 
 
# ─── Prompt Template ─────────────────────────────────────────────────────────
# The human message has a {feedback} variable, starts as "" via .partial().
 
PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a SQL expert. Given a user's question about data, generate a SQL query "
        "and then validate it by calling the sql_validate tool."
    )),
    ("human", "{question}{feedback}"),
]).partial(feedback="")
 
 
# ─── State ───────────────────────────────────────────────────────────────────
 
class GraphState(TypedDict):
    question: str                                           # the original user question
    messages: Annotated[list[AnyMessage], operator.add]     # full message history for the model
 
 
# ─── Tool ────────────────────────────────────────────────────────────────────
from validation import sql_validate
 
 
# ─── Tools list ──────────────────────────────────────────────────────────────
 
tools = [sql_validate]
 
 
# ─── LLM with tools bound ───────────────────────────────────────────────────
# Replace with: llm = llm_model(header=header(), model="gemini-2.5-flash")
 
def get_llm():
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    return llm.bind_tools(tools)
 
 
# ─── Nodes ───────────────────────────────────────────────────────────────────
 
def generate_sql(state: GraphState) -> dict:
    """Model node: formats prompt with .partial(), invokes LLM with tools bound."""
    messages = state.get("messages", [])
 
    # First run — build messages from prompt template
    if not messages:
        messages = PROMPT.format_messages(question=state["question"])
 
    llm = get_llm()
    response = llm.invoke(messages)
 
    return {
        "messages": messages + [response],
    }
 
 
# ─── Router ──────────────────────────────────────────────────────────────────
 
def should_call_tool(state: GraphState) -> Literal["tool_node", "end"]:
    """After model responds, check if it wants to call a tool."""
    messages = state["messages"]
    last_msg = messages[-1]
 
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        return "tool_node"
    return "end"
 
 
# ─── Build Graph ─────────────────────────────────────────────────────────────
 
def build_graph():
    graph = StateGraph(GraphState)
 
    # Nodes
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("tool_node", ToolNode(tools))
 
    # Edges
    graph.add_edge(START, "generate_sql")
 
    # After model: did it call a tool?
    graph.add_conditional_edges(
        "generate_sql",
        should_call_tool,
        {
            "tool_node": "tool_node",
            "end": END,
        },
    )
 
    # After tool execution: done
    graph.add_edge("tool_node", END)
 
    return graph.compile()
 
 
# ─── Run ─────────────────────────────────────────────────────────────────────
 
if __name__ == "__main__":
    app = build_graph()
 
    initial_state: GraphState = {
        "question": "Show me all employees in the engineering department",
        "messages": [],
    }
 
    print("=" * 60)
    print("  SQL Validation (bind_tools + ToolNode + .partial())")
    print("=" * 60)
 
    for event in app.stream(initial_state):
        for node_name, node_output in event.items():
            print(f"\n--- {node_name} ---")
 
            if node_name == "generate_sql":
                last = node_output["messages"][-1]
                if hasattr(last, "tool_calls") and last.tool_calls:
                    for tc in last.tool_calls:
                        print(f"  Tool call: {tc['name']}({tc['args']})")
                else:
                    print(f"  Response: {last.content[:150]}")
 
            elif node_name == "tool_node":
                for msg in node_output.get("messages", []):
                    if isinstance(msg, ToolMessage):
                        try:
                            r = json.loads(msg.content)
                            print(f"  Valid: {r.get('is_valid')}")
                            for e in r.get("errors", []):
                                print(f"    ✗ {e}")
                        except json.JSONDecodeError:
                            print(f"  Raw: {msg.content[:150]}")
 
    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)