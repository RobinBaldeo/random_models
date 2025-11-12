from http.client import responses

from decouple import config
from langchain_openai import ChatOpenAI
from langchain_core.prompts.prompt import PromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import set_debug
from langchain_core.runnables import RunnableLambda, RunnableMap
from langchain_core.runnables import RunnablePassthrough
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.chat_history import InMemoryChatMessageHistory


set_debug(False)


chat_template = ChatPromptTemplate.from_messages(
    [
        ("system", "You are a helpful AI bot. Your name is {name}"),
        ("human",  "Hello, how are you doing?"),
        ("ai", "I'm doing well, thanks!"),
        ("human", "{user_input")
    ]
)

prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are an AI chatbot having a conversation with a human. "
        "Use the following context to understand the human question. "
        "Do not include emojis in your answer."
    ),
    MessagesPlaceholder(variable_name="chat_history"),
    ("human", "{input}"),
])

class MyLogger(BaseCallbackHandler):
    def on_llm_start(self, serialized, prompts, **kwargs):
        print("LLM starting with prompt:", prompts)

    def on_llm_end(self, response, **kwargs):
        print("LLM finished, output:", response)


OPENAI_KEY = config("openAi")


store = {}

store = {}

def get_history(session_id: str) -> InMemoryChatMessageHistory:
    if session_id not in store:
        store[session_id] = InMemoryChatMessageHistory()
    return store[session_id]

# ---- 3️⃣ Wrap the chain with message history ----


if __name__ == "__main__":
    question = "what is the currency of Thailand?"
    llm = ChatOpenAI(model="gpt-4o-mini", api_key=OPENAI_KEY)
    chain = prompt| llm
    chain_with_history = RunnableWithMessageHistory(
        chain,
        get_history,  # returns a ChatMessageHistory
        input_messages_key="input",  # corresponds to {input} in the prompt
        history_messages_key="chat_history"  # corresponds to MessagesPlaceholder
    )

    question = "When was the last fifa world cup held?"

    if question:
        response = chain_with_history.invoke(
            {"input": question},
            config={"configurable": {"session_id": "any"}}
        )

        print(response)


