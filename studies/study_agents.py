"""Two agents written by other people's frameworks (smolagents and LangGraph via LangChain), each with the same two tools.
Nothing here knows about Agent Auditor: the spans come from the frameworks' own OpenInference instrumentation."""
from __future__ import annotations

from typing import Callable

from taskset import calculate as _calculate
from taskset import lookup_country as _lookup_country

Agent = Callable[[str], str]
AGENTS = ("smolagents", "langgraph")


def build_smolagents(model_id: str, base_url: str, api_key: str) -> Agent:
    from smolagents import OpenAIServerModel, ToolCallingAgent, tool

    @tool
    def lookup_country(country: str, field: str) -> str:
        """Look up a fact about a country.

        Args:
            country: the country name, for example Kenya
            field: either capital or currency_code
        """
        return _lookup_country(country, field)

    @tool
    def calculate(expression: str) -> str:
        """Evaluate an arithmetic expression using + - * / ** and numbers.

        Args:
            expression: for example 0.17 * 2340
        """
        return _calculate(expression)

    agent = ToolCallingAgent(tools=[lookup_country, calculate], model=OpenAIServerModel(model_id=model_id, api_base=base_url, api_key=api_key),
                             max_steps=6, verbosity_level=0)
    return lambda question: str(agent.run(question, reset=True))


def build_langgraph(model_id: str, base_url: str, api_key: str) -> Agent:
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    @tool
    def lookup_country(country: str, field: str) -> str:
        """Look up a fact about a country. field is either capital or currency_code."""
        return _lookup_country(country, field)

    @tool
    def calculate(expression: str) -> str:
        """Evaluate an arithmetic expression using + - * / ** and numbers, for example 0.17 * 2340."""
        return _calculate(expression)

    graph = create_agent(ChatOpenAI(model=model_id, base_url=base_url, api_key=api_key, temperature=0, max_retries=2),
                         tools=[lookup_country, calculate])

    def ask(question: str) -> str:
        out = graph.invoke({"messages": [{"role": "user", "content": question}]}, config={"recursion_limit": 12})
        content = out["messages"][-1].content
        return content if isinstance(content, str) else " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ask


def build(agent: str, model_id: str, base_url: str, api_key: str) -> Agent:
    if agent == "smolagents":
        return build_smolagents(model_id, base_url, api_key)
    if agent == "langgraph":
        return build_langgraph(model_id, base_url, api_key)
    raise ValueError(f"unknown agent {agent!r}; choose from {', '.join(AGENTS)}")


def instrument(agent: str, tracer_provider) -> Callable[[], None]:
    """Turn on the framework's own OpenInference instrumentation; returns a function that turns it off again."""
    if agent == "smolagents":
        from openinference.instrumentation.smolagents import SmolagentsInstrumentor as Inst
    else:
        from openinference.instrumentation.langchain import LangChainInstrumentor as Inst
    inst = Inst()
    inst.instrument(tracer_provider=tracer_provider, skip_dep_check=True) if agent == "smolagents" else inst.instrument(tracer_provider=tracer_provider)
    return inst.uninstrument
