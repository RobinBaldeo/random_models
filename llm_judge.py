import pdb
from itertools import chain

from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from decouple import config
from numpy.f2py.crackfortran import groupname
from polars import Field
from pydantic import BaseModel, field_validator, Field
from pydantic.types import conlist
from langchain_core.prompts import ChatPromptTemplate
from langchain.tools import tool
from langchain_core.output_parsers import StrOutputParser
from functools import partial
import json
from jsonpath_ng.ext import parse
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

open_ai_key = config("openAi")


class Chunks(BaseModel):
    chunks_lst: conlist(str, max_length=5, min_length=5)


class JudgeObject(BaseModel):
    score: int = Field(..., ge=0, le=10)
    reason: str


llm = ChatOpenAI(model="gpt-4o-mini", api_key=config("openAi"), temperature=0)


def aggregation_agent(input):
    source = input["source"]
    answer = input["answer"]
    question = input["question"]
    tools = [answer_accuracy, answer_relevancy_tool, groundness_tool]
    bound_llm = llm.bind_tools(tools)
    message = [
            SystemMessage(
                content="""
        You are an Evaluation Aggregation Agent.
        **Important**
        - Call all 3 agents
    
        Your role is to:
        - Receive the individual evaluations from three specialized tools:
          • answer_relevancy_tool → how well the ANSWER addresses the QUESTION
          • answer_accuracy_tool   → how factually correct the ANSWER is
          • groundedness_tool      → how well the ANSWER is supported by the provided SOURCE
        - Carefully analyze all three scores and reasons
        - Produce a single composite overall score (0–10) and a clear overall justification
    
        Weighting guidance (use your judgment):
        - All three aspects are important for high-quality RAG responses
        - A great answer must be relevant AND accurate AND grounded
        - Therefore, the composite score should generally be close to the minimum or average of the three scores (never much higher than the lowest one)
    
        Output exactly in this JSON format:
        {
          "overall_score": <integer 0-10>,
          "overall_reason": "<clear 2–4 sentence explanation of the final score, referencing the three individual evaluations>"
        }
    """
            ),
            HumanMessage(
                content=f"""
                **Inputs** 
                question:
                {question}
            
                answer:
                {answer}
            
                source:
                {source}
            
                **The will receive the following, and must pass the following to the tools** :
                - question
                - answer
                - source
                
                Once you have all three evaluations, produce the final aggregated result showing the following:
                - **Score**
                - **Reason for score**
    """
            ),
        ]
    toolcall = bound_llm.invoke(message)
    message.append(AIMessage(
        content=toolcall.content,
        tool_calls=toolcall.tool_calls
    ))

    if not toolcall.tool_calls:
        return {"final_content": toolcall.content, "messages": message}


    for tools in toolcall.tool_calls:
        if tools['name'] == 'answer_relevancy_tool':
            art = answer_relevancy_tool.invoke(tools['args'])
            message.append(ToolMessage(
                content=json.dumps(art),
                tool_call_id=tools["id"]
            ))

        elif tools['name'] == 'answer_accuracy':
            aa = answer_accuracy.invoke(tools['args'])
            message.append(ToolMessage(
                content=json.dumps(aa),
                tool_call_id=tools["id"]
            ))
        elif tools['name'] == 'groundness_tool':
            gt = groundness_tool.invoke(tools['args'])
            message.append(ToolMessage(
                content=json.dumps(gt),
                tool_call_id=tools["id"]
            ))
        else:
            pass

    final = llm.invoke(message)
    return final


@tool
def answer_accuracy(input):
    "get the answer accuracy score and return a reason for assigning the score."
    source = input["source"]
    answer = input["answer"]
    question = input["question"]
    message = {
        'source': source,
        'answer': answer,
        'question': question,
    }
    answer_accuracy_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are an expert fact-checker and accuracy evaluator. Your sole task is to determine how factually correct "
                "and accurate the ANSWER is in response to the QUESTION.\n"
                "Judge only truthfulness and correctness — ignore relevance, style, verbosity, or politeness.\n"
                "Score 10 only if the answer is 100% factually accurate with no errors, omissions, or hallucinations.",
            ),
            (
                "user",
                """
                    QUESTION:
                    {question}
                
                    ANSWER:
                    {answer}
                    
                    SOURCE
                    {source}
                
                    Evaluate the factual accuracy of the ANSWER on a 0–10 integer scale:
                
                    0  – Completely wrong or fabricated  
                    1–3 – Mostly inaccurate (major errors or hallucinations)  
                    4–6 – Mixed: some correct parts, but significant factual mistakes  
                    7–9 – Mostly correct (only minor errors or very small omissions)  
                    10 – Perfectly accurate and complete (no errors at all)
                
                    Output:
                    - the score
                    - the reason
                """,
            ),
        ]
    )
    chain = answer_accuracy_prompt | llm.with_structured_output(JudgeObject)
    response = chain.invoke(message)
    return response.model_dump()


@tool
def answer_relevancy_tool(input):
    "get the answer relevancy score and return a reason for assigning the score."
    source = input["source"]
    answer = input["answer"]
    question = input["question"]
    message = {
        'source': source,
        'answer': answer,
        'question': question,
    }
    answer_relevancy_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system", """
            You are an expert evaluator tasked with measuring how relevant an ANSWER is to the original QUESTION.
            Relevance means: the ANSWER directly addresses the user's QUESTION, stays on topic, and provides information the user actually asked for.
            Ignore correctness/accuracy — only judge topical relevance and focus.
            
            """

            ),
            (
                "user",
                """
                QUESTION:
                {question}
    
                ANSWER:
                {answer}
    
                Scoring guidelines (0–10):
                - 0: Completely irrelevant or off-topic (e.g., answers a different question entirely)
                - 1–3: Barely relevant; only tangentially related or mentions the topic in passing
                - 4–6: Partially relevant; addresses some aspects but misses the core intent or goes off on unrelated tangents
                - 7–9: Mostly relevant; directly answers most of the question with only minor digressions
                - 10: Perfectly relevant; the entire answer is focused exactly on what was asked, with no extraneous content
                Respond with a single integer score from 0 to 10, followed by a brief justification (1–2 sentences)."""
            ),
        ]
    )
    chain = answer_relevancy_prompt | llm.with_structured_output(JudgeObject)
    response = chain.invoke(message)
    return response.model_dump()


@tool
def groundness_tool(input):
    "get the groundness score and return a reason for assigning the score."
    source = input["source"]
    answer = input["answer"]
    question = input["question"]
    message = {
        'source': source,
        'answer': answer,
        'question': question,
    }
    groundedness_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are an expert evaluator tasked with determining how well a given ANSWER is grounded in the provided SOURCE. "
                "Assess only whether the factual claims in the ANSWER are directly supported by or can be inferred from the SOURCE. "
                "Ignore style, rephrasing, or explanations that are logically implied but not explicitly contradicted by the SOURCE.",
            ),
            (
                "user",
                """Evaluate the following ANSWER against the provided SOURCE and assign a groundedness score from 0 to 10:

            ANSWER:
            {answer}
        
            SOURCE:
            {source}
        
            Scoring guidelines:
            - 0: The ANSWER contains claims that are completely unsupported or contradicted by the SOURCE.
            - 1–3: Almost none of the ANSWER is grounded; most claims are unsupported or invented.
            - 4–6: Some parts of the ANSWER are grounded, but significant portions are unsupported or go beyond the SOURCE.
            - 7–9: Most or all factual claims in the ANSWER are directly supported by the SOURCE, with only minor unsupported additions or rephrasing.
            - 10: The ANSWER is completely and strictly grounded — every factual claim is explicitly or directly inferable from the SOURCE with no additions or hallucinations.
        
            Respond with a single integer score (0–10) followed by a brief justification (1–2 sentences).
            """,
            ),
        ]
    )
    chain = groundedness_prompt | llm.with_structured_output(JudgeObject)
    response = chain.invoke(message)
    return response.model_dump()


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
        SystemMessage(system_prompt),
        HumanMessage(user_prompt),
    ]

    bound_llm = llm.bind_tools([get_chunks])
    tool_call = bound_llm.invoke(messages)
    messages.append(AIMessage(content=tool_call.content, tool_calls=tool_call.tool_calls or []))

    if not tool_call.tool_calls:
        return {"final_content": tool_call.content, "messages": messages[-1].content}

    for tc in tool_call.tool_calls:
        result = get_chunks.invoke(tc["args"])
        if result is None:
            continue
        messages.append(ToolMessage(
            content=json.dumps(result),
            tool_call_id=tc["id"]
        ))

    final = llm.invoke(messages)
    return {"question": question, "answer": final.content, "source": messages[-1].content}


@tool
def get_chunks(input):
    """This get snippets from an llm based on the question """
    question = input
    chat = ChatPromptTemplate.from_messages([
        ("system", "You return 5 made up facts, concise snippets related to the user question."),
        ("user", "Provide 5 different fact-based snippets about: {question}")
    ])

    chain = chat | llm.with_structured_output(Chunks)
    response = chain.invoke({"question": question})
    return json.dumps(response.chunks_lst)

if __name__ == "__main__":
    chain = RunnableLambda(lambda f: fake_answers(f)) | RunnableLambda(lambda f: aggregation_agent(f))|StrOutputParser()
    response = chain.invoke({"question": "How do you decide when to use an exponential distribution vs binomial distribution?"})
    print(response)
