from langgraph.prebuilt import ToolNode
from pandas.core.computation.common import result_type_many
from typing_extensions import TypedDict, Annotated
from typing_extensions import TypedDict
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from decouple import config
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages

from langchain.agents.middleware import wrap_tool_call
from langchain.tools.tool_node import ToolCallRequest







class State(TypedDict):
    messages: Annotated[list, add_messages]
    # messages: list
    question:str
    results: dict



# Tool input schemas
class TwoNumbersInput(BaseModel):
    a: float = Field(..., description="First number")
    b: float = Field(..., description="Second number")




@tool(args_schema=TwoNumbersInput)
def add_numbers(a: float, b: float) -> int:
    """Add two numbers together and return the result."""
    return a + b


@tool(args_schema=TwoNumbersInput)
def divide_numbers(a: float, b: float) -> int:
    """Divide the first number by the second number and return the result."""
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a/b


tools = [add_numbers, divide_numbers]


system_message = SystemMessage(
    content=
"""
    You are a strict math tool-using assistant.
    
    Rules:
    
    1. You must generate exactly two integers.
    2. You must call BOTH tools:
       - add_numbers(a, b)
       - divide_numbers(a, b)
    3. Do NOT compute results yourself. Use only tool outputs.
    4. After both tool calls return results:
       - If BOTH results are odd → final outcome = "odd"
       - If BOTH results are even → final outcome = "even"
       - If one result is odd and the other is even → final outcome = "neither"
    5. If division produces a non-integer result, treat it as "neither".
    6. Return ONLY one word as the final answer: odd, even, or neither.
    7. Do not explain your reasoning.
"""
)

human_message = HumanMessage(
    content="""
Generate two integers.
Call the add_numbers tool with those numbers.
Call the divide_numbers tool with those numbers.
Then determine whether the overall outcome is odd, even, or neither.
"""
)

messages = [system_message, human_message]


llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=.8,
    api_key=config("openAi")
).bind_tools(tools)


def wrapper_function(request, handler):
    print()
    args = request.tool_call['args']
    if args['b'] and args['b'] < 1:
        request.tool_call['args']['b'] = 10
    if args['a'] and args['a'] < 4:
        request.tool_call['args']['a'] = 2

    return handler(request)


tool_node = ToolNode(tools=tools, wrap_tool_call=wrapper_function)


def llm_node(state: State):
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


def tool_router(state: State):
    last = state.get('messages')[-2:]
    if len(last) == 2:
        try:
            tool1 = float(last[0].content)
            tool2 = float(last[1].content)

            if (tool1 + tool2) > 100:
                return 'tool1_greater_than_tool2'

            if tool1 > tool2:
                return 'llm'
        except Exception as e:
            pass

    return 'end'

def fake_message(state: State):
    print("dead end")



def llm_router(state: State):
    response = llm.invoke(state["messages"])









if __name__ == "__main__":
    graph = StateGraph(State)
    graph.add_node("llm", llm_node)
    graph.add_node("tool", tool_node)
    graph.add_node("fake", fake_message)




    graph.add_edge(START, "llm")
    graph.add_edge("llm", "tool")
    graph.add_conditional_edges("tool",
                                tool_router,
                                {
                                    'tool1_greater_than_tool2': 'fake',
                                    'llm': 'llm',
                                    'end': END
                                }
                                )
    # graph.add_edge(START, "tool")
    # graph.add_edge("tool" , END)
    compile_graph  = graph.compile()
    result = compile_graph.invoke({
        "messages": messages,
        "results": {},
        "question": ""
    })
    print()




