from http.client import responses

from decouple import config
from langchain_openai import ChatOpenAI
from langchain_core.prompts.prompt import PromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import set_debug
from langchain_core.runnables import RunnableLambda, RunnableMap
from langchain_core.runnables import RunnablePassthrough

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
    input_variables=["title", "emotion"],
    template = """
    You are an expert on the topic:  {title}.
    -write paragraph on the given topic based on the {emotion}..
    return
    - a json with the following:
        - title
        - emotion
        - topic
        - paragraph
    """
)



if __name__ == "__main__":
    question = "what is the currency of Thailand?"
    llm = ChatOpenAI(model="gpt-4o-mini", api_key=OPENAI_KEY)
    first_chain = title_prompt| llm | StrOutputParser()
    second_chain = essay_prompt| llm| JsonOutputParser()
    overall_chain = (RunnablePassthrough.assign(title=first_chain) | second_chain )

    response = overall_chain.invoke(
        {"topic": "poverty", "emotion": "despair"}
        # , config = {"callbacks": [MyLogger()]}
    )
    print(response)



