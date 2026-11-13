"""Attribute shapes captured from real instrumentation libraries (OpenInference for OpenAI, smolagents and LangChain;
OpenLLMetry for OpenAI), run against a scripted OpenAI-compatible server. They pin the mapper to what other people's code emits,
not only to spans written for these tests."""
from test_otlp_import import build, span

A = "a" * 16
B = "b" * 16
C = "c" * 16
D = "d" * 16
E = "e" * 16
Q = "How long do refunds take?"


def only(runs):
    assert len(runs) == 1
    return runs[0]


def test_openinference_openai_lone_model_call_takes_messages_not_raw_json():
    raw_in = '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "How long do refunds take?"}]}'
    raw_out = '{"id":"c1","choices":[{"message":{"content":"Refunds take 5 days.","role":"assistant"}}]}'
    run = only(build([span(A, "ChatCompletion", ms=80, attrs={
        "openinference.span.kind": "LLM", "llm.system": "openai", "input.value": raw_in, "input.mime_type": "application/json",
        "output.value": raw_out, "output.mime_type": "application/json", "llm.model_name": "gpt-4o-mini",
        "llm.input_messages.0.message.role": "system", "llm.input_messages.0.message.content": "Be brief.",
        "llm.input_messages.1.message.role": "user", "llm.input_messages.1.message.content": Q,
        "llm.output_messages.0.message.role": "assistant", "llm.output_messages.0.message.content": "Refunds take 5 days.",
        "llm.token_count.prompt": 50, "llm.token_count.completion": 12})]))
    assert run.input == Q and run.output == "Refunds take 5 days."
    assert run.tags["model"] == "gpt-4o-mini"
    assert [s.kind for s in run.steps] == ["llm"]          # the lone root is also the run's one step
    assert run.steps[0].attributes["input_tokens"] == 50


def test_openllmetry_openai_genai_parts_messages_are_read():
    run = only(build([span(A, "openai.chat", ms=80, attrs={
        "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": "gpt-4o-mini",
        "gen_ai.input.messages": '[{"role": "system", "parts": [{"content": "Be brief.", "type": "text"}]},'
                                 ' {"role": "user", "parts": [{"content": "How long do refunds take?", "type": "text"}]}]',
        "gen_ai.output.messages": '[{"role": "assistant", "parts": [{"content": "Refunds take 5 days.", "type": "text"}], "finish_reason": "stop"}]',
        "gen_ai.usage.input_tokens": 50, "gen_ai.usage.output_tokens": 12})]))
    assert run.input == Q and run.output == "Refunds take 5 days."
    assert run.tags["model"] == "gpt-4o-mini" and len(run.steps) == 1


def test_smolagents_run_takes_the_task_from_the_call_arguments_and_keeps_model_and_tool_steps():
    spans = [
        span(A, "ToolCallingAgent.run", ms=500, attrs={
            "openinference.span.kind": "AGENT", "input.value": '{"task": "How long do refunds take?", "stream": false, "reset": true, "images": null}',
            "output.value": "Refunds take 5 days."}),
        span(B, "Step 1", parent=A, start=5, ms=200, attrs={"openinference.span.kind": "CHAIN", "input.value": '{"memory_step": "ActionStep(step_number=1)"}',
                                                            "output.value": "Refunds are issued within 5 days."}),
        span(C, "OpenAIModel.generate", parent=B, start=10, ms=60, attrs={
            "openinference.span.kind": "LLM", "llm.model_name": "gpt-4o-mini", "llm.provider": "openai", "llm.token_count.prompt": 50, "llm.token_count.completion": 12}),
        span(D, "SimpleTool", parent=B, start=80, ms=20, attrs={"openinference.span.kind": "TOOL", "tool.name": "get_policy",
                                                                "input.value": '{"args": [], "kwargs": {"topic": "refunds"}}',
                                                                "output.value": "Refunds are issued within 5 days."}),
    ]
    run = only(build(spans))
    assert run.input == Q and run.output == "Refunds take 5 days."
    assert run.tags["model"] == "gpt-4o-mini"
    assert [s.kind for s in run.steps] == ["llm", "tool"]   # the "Step 1" chain span only groups its children


def test_langgraph_state_messages_give_the_question_and_the_last_ai_answer():
    state_in = '{"messages": [{"role": "user", "content": "How long do refunds take?"}]}'
    state_out = ('{"messages": [{"type": "human", "data": {"content": "How long do refunds take?"}},'
                 ' {"type": "ai", "data": {"content": ""}}, {"type": "tool", "data": {"content": "Refunds are issued within 5 days."}},'
                 ' {"type": "ai", "data": {"content": "Refunds take 5 days."}}]}')
    spans = [
        span(A, "LangGraph", ms=400, attrs={"openinference.span.kind": "CHAIN", "input.value": state_in, "output.value": state_out}),
        span(B, "ChatOpenAI", parent=A, start=10, ms=100, attrs={"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o-mini"}),
    ]
    run = only(build(spans))
    assert run.input == Q and run.output == "Refunds take 5 days."
    assert run.tags["model"] == "gpt-4o-mini"


def test_a_dict_without_a_recognised_field_is_kept_as_it_is():
    run = only(build([span(A, "x", attrs={"openinference.span.kind": "CHAIN", "input.value": '{"foo": 1}', "output.value": "ok"})]))
    assert run.input == {"foo": 1}
