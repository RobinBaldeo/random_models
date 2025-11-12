from langchain_openai import ChatOpenAI
from decouple import config
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain.tools import tool
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel
from typing import List, Dict, Any
from langchain_core.callbacks import BaseCallbackHandler

class MyLogger(BaseCallbackHandler):
    def on_llm_start(self, serialized, prompts, **kwargs):
        print("LLM starting with prompt:", prompts)

    def on_llm_end(self, response, **kwargs):
        print("LLM finished, output:", response)



class Output(BaseModel):
    city: str
    condition: str
    temp: str
    summary: str


@tool
def get_weather(city: str) -> str:
    """Return fake weather data."""
    data = {"Miami": "28°C and sunny", "London": "15°C and rainy"}
    return data.get(city, "Unknown city")


@tool
def get_running_conditions(city: str) -> str:
    """Return the running condition for that city."""
    if city == "Miami":
        return "Not ideal — hot and humid. Hydrate well and run early morning."
    if city == "London":
        return "Cool and comfortable — great for long-distance runs."
    return "No data available."


TOOLS = {t.name: t for t in [get_weather, get_running_conditions]}

llm = ChatOpenAI(model="gpt-4o-mini", api_key=config("openAi"), temperature=0)
llm_tool = llm.bind_tools([get_weather, get_running_conditions])

prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template("""
    You are a weather genius with extensive knowledge of running.
    Call the weather tool and the running condition tool to get city running info.
    After gathering the data, provide a nice conversational summary.
    """),
    HumanMessagePromptTemplate.from_template("""
    What is the city, condition, and temperature in {city}?
    """)
])


def execute_tool_calls(messages: List) -> Dict[str, Any]:

    max_iterations = 3

    for _ in range(max_iterations):
        ai_response = llm_tool.invoke(messages)
        messages.append(ai_response)
        if not ai_response.tool_calls:
            return {
                "final_content": ai_response.content,
                "messages": messages
            }

        for tool_call in ai_response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]

            tool = TOOLS.get(tool_name)
            if tool:
                tool_result = tool.invoke(tool_args)

                tool_message = ToolMessage(
                    content=tool_result,
                    tool_call_id=tool_call["id"]
                )
                messages.append(tool_message)


    final_response = llm.invoke(messages)
    return {
        "final_content": final_response.content,
        "messages": messages + [final_response]
    }



simple_chain = (
        {"city": RunnableLambda(lambda x: x["city"])}
        | prompt
        | RunnableLambda(lambda msgs: execute_tool_calls(msgs.to_messages()))
        | RunnableLambda(lambda result: result["final_content"])
)

if __name__ == "__main__":

    summary = simple_chain.invoke({"city": "London"},config = {"callbacks": [MyLogger()]})
    print(summary)