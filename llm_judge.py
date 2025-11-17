import pdb
from itertools import chain

from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from decouple import config
from pydantic import BaseModel
from pydantic.types import conlist
from langchain_core.prompts import ChatPromptTemplate
from langchain.tools import tool
from functools import partial
import json
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from jsonpath_ng.ext import parse

open_ai_key = config("openAi")

class Chunks(BaseModel):
    chunks_lst: conlist(str, max_length=5, min_length=5)



llm = ChatOpenAI(model="gpt-4o-mini", api_key=config("openAi"), temperature=0)

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage  # Add these imports


def fake_answers(input):
    question = input["question"]
    system_prompt = """You are an expert summarizer. Your job is to generate a concise, factual summary based on the question and the snippets returned by the `get_chunks` tool. You must ALWAYS call the `get_chunks` tool first, using the question as input. After receiving the snippets, produce a final summary based strictly on those snippets."""
    user_prompt = f"""You are given the following question: {question}
                        Instructions:
                        1. Call the `get_chunks` tool using the question.
                        2. The tool will return 5 factual snippets.
                        3. Using ONLY those snippets, produce a clear and concise summary.
                  """


    messages = [
        SystemMessage(system_prompt ),
        HumanMessage(user_prompt ),
    ]

    bound_llm = llm.bind_tools([get_chunks])
    tool_call = bound_llm.invoke(messages)
    messages.append(AIMessage(content=tool_call.content, tool_calls=tool_call.tool_calls or []))

    if not tool_call.tool_calls:
        return {"final_content": tool_call.content, "messages":messages[-1].content}

    for tc in tool_call.tool_calls:
        result = get_chunks.invoke(tc["args"])
        if result is None:
            continue
        messages.append(ToolMessage(
            content=json.dumps(result),
            tool_call_id=tc["id"]
        ))

    final = llm.invoke(messages)
    return {"final_content": final.content, "messages": messages}



@tool
def get_chunks(input):
    """This get snippets from an llm based on the question """
    question = input
    chat = ChatPromptTemplate.from_messages([
        ("system", "You return 5 factual, concise snippets related to the user question."),
        ("user", "Provide 5 different fact-based snippets about: {question}")
    ])

    chain = chat |llm.with_structured_output(Chunks)
    response = chain.invoke({"question": question})
    return json.dumps(response.chunks_lst)


if __name__ == "__main__":

    chain = RunnableLambda(lambda f: fake_answers(f))
    response = chain.invoke({"question": "How did SG1 defeat the ORI?"})
    print(response)







