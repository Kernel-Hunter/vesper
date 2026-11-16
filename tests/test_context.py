"""Context estimates, provider-reported usage and model window provenance (offline)."""

import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vesper import providers
from vesper.coder import SUMMARY_OPEN, Session, context_report
from vesper.ui import fmt_tokens
from vesper.workspace import Workspace


def test_documented_windows_are_model_and_provider_specific(monkeypatch):
    assert providers.context_window('openai', 'gpt-4.1-mini') == (1_047_576, 'documented')
    assert providers.context_window('openai', 'gpt-4o-mini-2024-07-18') == (128_000, 'documented')
    assert providers.context_window('anthropic', 'claude-sonnet-5-5') == (1_000_000, 'documented')
    assert providers.context_window('anthropic', 'claude-haiku-4-5-20251001') == (200_000, 'documented')
    assert providers.context_window('groq', 'llama-3.3-70b-versatile') == (131_072, 'documented')
    assert providers.context_window('openrouter', 'anthropic/claude-sonnet-5-5') == (120_000, 'assumed')
    assert providers.context_window('openai', 'gpt-4.1-custom') == (120_000, 'assumed')
    monkeypatch.setenv('VESPER_OLLAMA_CTX', '32768')
    assert providers.context_window('ollama', 'any') == (32768, 'requested')


def test_report_counts_prompt_tool_schemas_history_and_summary(tmp_path):
    ws = Workspace(tmp_path)
    messages = [HumanMessage(f'{SUMMARY_OPEN}\nprior decisions\n</summary>'),
                HumanMessage('question' * 20), AIMessage('answer'),
                ToolMessage('file content' * 20, tool_call_id='call')]
    report = context_report(messages, ws, 'openai', 'gpt-4.1')
    parts = report['breakdown']
    assert all(parts[k] > 0 for k in ('instructions', 'tools', 'conversation', 'tool_results', 'summary'))
    assert report['used'] == sum(parts.values())
    assert report['history'] == sum(parts[k] for k in ('conversation', 'tool_results', 'summary'))
    assert report['window'] == 1_047_576 and report['history_budget'] == 628_545
    assert 'prior decisions' not in str(report)
    assert context_report(messages, ws, 'openai', 'gpt-4.1', 1000)['window_source'] == 'assumed'


def test_usage_counts_each_call_even_after_trimming_and_keeps_partial_estimates(tmp_path):
    session = Session(None, Workspace(tmp_path))
    final = [HumanMessage('question'),
             AIMessage('', tool_calls=[{'name': 'read_file', 'args': {'path': 'a'}, 'id': 'x'}],
                       usage_metadata={'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                                       'input_token_details': {'cache_read': 75}}),
             ToolMessage('result', tool_call_id='x'), AIMessage('answer')]
    session.history = final
    # The old positional offset referred to a longer pre-trim history.
    usage = session._usage(final, 999, time.monotonic(), 1)
    assert usage['cached'] == 75 and usage['input'] > 100 and usage['output'] > 10
    assert usage['estimated'] and usage['last_call']['estimated']
    assert usage['context'] == usage['context_report']['used']
    assert usage['input'] > usage['last_call']['input']


def test_compact_token_formatting():
    assert fmt_tokens(999) == '999'
    assert fmt_tokens(1200) == '1.2K'
    assert fmt_tokens(1_234_567) == '1.23M'
    assert fmt_tokens(2_500_000_000) == '2.5B'
