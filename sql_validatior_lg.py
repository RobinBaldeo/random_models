import pdb

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END, START
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from langchain_core.messages import  ToolMessage
import json
import sqlglot
from langchain_core.tools import tool
import sqlglot.errors
from decouple import config
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

llm = ChatOpenAI(model="gpt-4.1-nano", temperature=0, api_key=config("openAi"))

class SqlValidationOutput(BaseModel):
    sql: str = Field(..., description="The generated SQL")


class FinalSQLOutput(BaseModel):
    sql: str = Field(..., description="The final validated SQL query")
    explanation: str = Field(..., description="Brief explanation of the errors detected and fixed")



@tool(args_schema=SqlValidationOutput)
def sql_validation_tool(sql: str):
    """This is a validation tool to check if a sql has any syntax error"""
    try:
        sqlglot.parse_one(sql)
        dic =  {
            "is_valid": True,
            "error": None
        }
        print(dic)
        return dic
    except sqlglot.errors.ParseError as e:

        dic =  {
            "is_valid": False,
            "error": str(e)
        }
        print(dic)
        return dic
    except Exception as e:
        dic = {
            "is_valid": False,
            "error": str(e)
        }
        print(dic)
        return dic

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
            - SELECT id name :age FROM finance.customers;
            - SELET * FROM finance.customers;
            - SELEC, id, age FROM finance.customers;
            - SEECT id, age FRO finance.customers;
            
            Output:
            Return sql string that begins with a valid sql syntax. No Markdown, no comments, no backticks. in format:
            

        """
    )
])


class State(TypedDict):
    question: str
    messages: list
    search: str
    schema: dict
    counter: int
    final_output: FinalSQLOutput| None



def vector_search_tool(state: State):
    few_shot = '\n'.join([
        "SELECT customer_id, total_deposits FROM finance.customers ORDER BY total_deposits DESC LIMIT 10;",
        "SELECT * FROM finance.customers WHERE amount > 5000;",
        "SELECT account_id, balance FROM finance.customers WHERE balance < 100;"
    ])

    return {"messages": state['messages'],
            "search": few_shot,
            "schema": state["schema"],
            "question": state['question'],
            "counter": state['counter']
            }


def mock_fetch_sql_schema(state: State):
    mock_schemas = {
        "finance.customers": {
            "customer_id": "INT",
            "balance": "DECIMAL",
            "account_id": "INT",
            "name": "STRING",
            "total_deposits": "DECIMAL",
            "status": "STRING",
            "amount": "DECIMAL"
        },
    }
    return {"messages": state['messages'],
            "search": state["search"],
            "schema": mock_schemas,
            "question": state['question'],
            "counter": state['counter']
            }


def construct_prompt(state: State):
    values = {
        'question': state['question'],
        'search': state['search'],
        'schema': json.dumps(state['schema'])
    }
    msg = prompt.format_messages(**values)
    # print(msg)
    return {"messages": msg,
            "search": state["search"],
            "schema":state["schema"],
            "question": state['question'],
            "counter": state['counter']
            }



tools = [sql_validation_tool]
agent = llm.bind_tools(tools)
llm_structural_output = llm.with_structured_output(FinalSQLOutput)

def llm_node(state: State):

    response = agent.invoke(state['messages'])
    counter = state['counter'] + 1
    return {"messages": state["messages"] + [response],
            "search": state["search"],
            "schema": state["schema"],
            "question": state['question'],
            "counter": counter

           }




def tool_node(state: State):
    last = state["messages"][-1]
    call = last.tool_calls[0]

    results = sql_validation_tool.invoke(call['args'])
    # print(results)
    tool_msg = ToolMessage(content=json.dumps(results),
                           name = call["name"],
                           tool_call_id=call["id"]
                           )

    return {"messages": state["messages"] + [tool_msg],
            "search": state["search"],
            "schema": state["schema"],
            "question": state['question'],
            "counter": state['counter']
           }

def final_output_node(state: State):
    prompt = f"""
                Based on the conversation below, extract the final validated SQL and summarize the various error detected by the tool.
                Conversation:
                {state['messages']}
                Return: 
                - validated SQL
                - brief explanation.
            """
    result = llm_structural_output.invoke(prompt)
    return {
        "messages": state["messages"],
        "search": state["search"],
        "schema": state["schema"],
        "question": state['question'],
        "counter": state['counter'],
        "final_output": result
    }


def router_node(state: State):
    last = state["messages"][-1]
    print(state['counter'])
    if state['counter'] < 3:
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tool"
    return "done"



if __name__ == '__main__':

    graph = StateGraph(State)
    graph.add_node("vector_search", vector_search_tool)
    graph.add_node("fetch_schema", mock_fetch_sql_schema)
    graph.add_node("construct_prompt", construct_prompt)
    graph.add_node("llm", llm_node)
    graph.add_node("tool", tool_node)
    graph.add_node("final_output", final_output_node)

    graph.set_entry_point("vector_search")


    graph.add_edge(START, "vector_search")
    graph.add_edge("vector_search", "fetch_schema")
    graph.add_edge("fetch_schema", "construct_prompt")
    graph.add_edge("construct_prompt", "llm")
    graph.add_conditional_edges("llm",
                                router_node,
                                {
                                    'tool': 'tool',
                                    'done': 'final_output'
                                })

    graph.add_edge("tool", 'llm')
    graph.add_edge("final_output", END)
    temp = graph.compile().invoke({
                            "messages": [],
                            "search": '',
                            "schema":{},
                            "question": "what is the top deposits ?",
                            "counter": 0,
                            "final_output": None
                            })
    print(temp['messages'][-1].content)
    # pdb.set_trace()
