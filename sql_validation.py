
from typing import List

from openai import api_key
from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableLambda, RunnableParallel, RunnablePassthrough
from langchain_core.prompts import  ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from decouple import config
from langchain_openai import ChatOpenAI
import sqlglot
from sqlglot.errors import ParseError
from langchain_core.tools import tool
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.callbacks import BaseCallbackHandler
import json

#
class MyLogger(BaseCallbackHandler):
    def on_llm_start(self, serialized, prompts, **kwargs):
        print("LLM starting with prompt:", prompts)

    def on_llm_end(self, response, **kwargs):
        print("LLM finished, output:", response)



api_key = config("openAi")

class VectorSearchInput(BaseModel):
    query: str = Field(..., description="Natural language request to search")

def vector_search_tool( question):
    return '\n'.join([
        "SELECT customer_id, total_deposits FROM finance.customers ORDER BY total_deposits DESC LIMIT 10;",
        "SELECT * FROM finance.transactions WHERE amount > 5000;",
        "SELECT account_id, balance FROM finance.accounts WHERE balance < 100;"
    ])



class SchemaLookupInput(BaseModel):
    table_name: str = Field(..., description="Name of a table to get schema for")

def schema_lookup_tool(question):

    schemas = {
        "finance.customers": {
            "customer_id": "INT",
            "name": "STRING",
            "total_deposits": "DECIMAL",
            "status": "STRING"
        },
    }
    return schemas


prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(
        """
You are a SQL generator. Your purpose is to create SQL, call the sql_validation tool,
and correct errors until the SQL is fully valid.
"""
    ),

    HumanMessagePromptTemplate.from_template(
        """
You are given:

1. The user question:
   {question}

2. Example SQL patterns retrieved from vector search:
   {search}

3. The database schema:
   {schema}

Instructions:

IMPORTANT:
- Your FIRST generated SQL MUST be syntactically invalid.
- If your first SQL is valid, the evaluation will fail.
- Do NOT correct or format the initial SQL.
- Your FIRST output MUST be a tool call to `sql_validation` that contains the invalid SQL.
- You must ALWAYS call the sql_validation tool after generating SQL.

Error correction loop:
1. Generate broken SQL (first turn only).
2. Call `sql_validation`.
3. When the tool returns an error:
     - Fix ONLY the reported error.
     - Call `sql_validation` again.
4. Repeat until the tool returns no errors.
5. When no errors remain, output the final corrected SQL.

Examples of intentionally broken SQL (for guidance):
- SELECT id name :age FROM users;
- SELET * FROM users;
- SELEC, id, age FROM users;
- SEECT id, age FROM;

Output:
Return sql string that begins with a valid sql syntax. No Markdown, no comments, no backticks. in format:

"""
    )
])


@tool
def sql_validation_tool(inputs):
    """This is a validation tool to check if a sql has any syntax error"""

    sql = inputs['sql']
    print(sql)
    try:
        sqlglot.parse_one(sql)
        dic =  {
            "is_valid": True,
            "error": None
        }
        # print(dic)
        return None
    except ParseError as e:

        dic =  {
            "is_valid": False,
            "error": str(e)
        }
        # print(dic)
        return dic
    except Exception as e:
        dic = {
            "is_valid": False,
            "error": str(e)
        }
        # print(dic)
        return dic


llm = ChatOpenAI(model  = "gpt-4.1", api_key = api_key)
llm_tool = llm.bind_tools([sql_validation_tool])
TOOLS = {t.name: t for t in [sql_validation_tool]}


def execute_tool_calls(messages):
    max_iterations = 3

    for _ in range(max_iterations):

        ai_response = llm_tool.invoke(messages)

        messages.append(
            AIMessage(
                content=ai_response.content,
                tool_calls=ai_response.tool_calls,
            )
        )

        if not ai_response.tool_calls:
            return {
                "final_content": ai_response.content,
                "messages": messages
            }

        for tc in ai_response.tool_calls:
            name = tc["name"]
            args = tc["args"]

            tool = TOOLS.get(name)
            result = tool.invoke(args)
            print(result)
            if result is None:
                return {
                    "final_content": ai_response.content,
                    "messages": messages
                }


            tool_msg = ToolMessage(
                content=json.dumps(result),
                tool_call_id=tc["id"]
            )
            messages.append(tool_msg)

    final = llm.invoke(messages)
    messages.append(final)
    return {
        "final_content": final.content,
        "messages": messages
    }


if __name__ == "__main__":
    chain = (RunnableParallel(
            search = RunnableLambda(lambda f: vector_search_tool(f)),
            schema = RunnableLambda(lambda f: schema_lookup_tool(f)),
            question = RunnableLambda(lambda d: d["user_question"])
            )|
            prompt|
            RunnableLambda(lambda f: execute_tool_calls(f.to_messages()))
    )
    response = chain.invoke(
        {"user_question": "what is the top deposits ?"},
        # config={"callbacks": [MyLogger()]}
    )

    print(response['final_content'])