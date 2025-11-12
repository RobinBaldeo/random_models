from langchain_openai import ChatOpenAI
from decouple import config
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, SystemMessagePromptTemplate
from langchain.tools import tool
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel



class Output(BaseModel):
    city: str
    condition: str
    temp: str


llm = ChatOpenAI(model="gpt-4.1-mini", api_key=config('openAi'))




@tool
def get_weather(city: str) -> str:
    """Return fake weather data."""
    data = {"Miami": "28°C and sunny", "London": "15°C and rainy"}
    return data.get(city, "Unknown city")

@tool
def get_running_conditions(city: str) -> str:
    """Return the running condition for that city selected."""
    if city == "Miami":
        return "Not ideal — hot and humid. Hydrate well and run early morning."
    if city == "London":
        return "Cool and comfortable — great for long-distance runs."
    return "No data available."


prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template("""
    you are a weather genius with extensive knowledge of running, call the weather tool and the  running condition tool to get city running info.
    return 
    use the tool data to return a nice summary. 
    """),
    HumanMessagePromptTemplate.from_template("""
    What is the city, condition, and temperature in {city}? 
    """
     )
])


TOOLS = {t.name: t for t in [get_weather, get_running_conditions]}

def get_response(response):
    lst = []
    if response.tool_calls:
        if len(response.tool_calls)> 0:
            for i in response.tool_calls:
                call = i['name']
                lst.append(TOOLS.get(call).invoke(i['args']))

    if len(lst) > 0:
        return '\n'.join(lst)
    else:
        return response


llm_tool = llm.bind_tools([get_weather, get_running_conditions])

if __name__ == "__main__":
    chain = prompt| llm_tool| RunnableLambda(get_response)
    response = chain.invoke({"city": "Miami"})
    print(response)