from http.client import responses

from decouple import config
from langchain_openai import ChatOpenAI
from langchain_core.prompts.prompt import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import set_debug

set_debug(False)

class MyLogger(BaseCallbackHandler):
    def on_llm_start(self, serialized, prompts, **kwargs):
        print("LLM starting with prompt:", prompts)

    def on_llm_end(self, response, **kwargs):
        print("LLM finished, output:", response)


OPENAI_KEY = config("openAi")

title_prompt = PromptTemplate(
    input_variables=["topic"],
    template = """
    You are an expert journalist.
    you need to come up with an intresting tile  about topic {topic}.
    return 
    - One title.
    """
)

essay_prompt = PromptTemplate(
    input_variables=["title"],
    template = """
    You are an expert write on the topic:  {title}.
    return
    - a single paragraph on on the given topic.
    """
)


if __name__ == "__main__":
    question = "what is the currency of Thailand?"
    llm = ChatOpenAI(model="gpt-4o-mini", api_key=OPENAI_KEY)
    first_chain = title_prompt| llm | StrOutputParser()
    second_chain = essay_prompt| llm| StrOutputParser()
    overall_chain = first_chain| second_chain

    response = overall_chain.invoke(
        {"topic": "poverty"}
        # , config = {"callbacks": [MyLogger()]}
    )

    print(response)



