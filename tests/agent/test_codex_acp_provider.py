"""Codex ACP survives the provider-registry migration without HTTP or Copilot argv."""
from types import SimpleNamespace
import sys


def test_codex_acp_real_resolver_and_agent_client_preserve_launch_spec(monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.auxiliary_client import resolve_provider_client
    from agent.agent_init import _explicit_client_kwargs
    from agent.agent_runtime_helpers import create_openai_client
    from agent.copilot_acp_client import CopilotACPClient

    # No subprocess/LLM calls: an existing executable is sufficient for resolution.
    monkeypatch.setenv("HERMES_CODEX_ACP_COMMAND", sys.executable)
    monkeypatch.delenv("HERMES_CODEX_ACP_ARGS", raising=False)
    monkeypatch.delenv("CODEX_ACP_BASE_URL", raising=False)
    runtime = resolve_runtime_provider(requested="codex-cli-acp", target_model="codex-acp")
    assert (runtime["provider"], runtime["base_url"], runtime["args"]) == ("codex-acp", "acp://codex", [])
    agent = SimpleNamespace(provider=runtime["provider"], acp_command=runtime["command"],
                            acp_args=runtime["args"], _client_log_context=lambda: "")
    kwargs = _explicit_client_kwargs(agent, runtime["api_key"], runtime["base_url"], None)
    client = create_openai_client(agent, kwargs, reason="test", shared=False)
    assert isinstance(client, CopilotACPClient)
    assert (client._acp_command, client._acp_args) == (sys.executable, [])
    client.close()
    client, model = resolve_provider_client("codex-acp", model="codex-acp")
    assert isinstance(client, CopilotACPClient)
    assert (model, client._acp_command, client._acp_args) == ("codex-acp", sys.executable, [])
    client.close()


def test_codex_acp_env_overrides_and_copilot_args_stay_distinct(monkeypatch):
    from hermes_cli.auth import resolve_external_process_provider_credentials
    from agent.copilot_acp_client import CopilotACPClient

    monkeypatch.setenv("HERMES_CODEX_ACP_COMMAND", sys.executable)
    monkeypatch.setenv("HERMES_CODEX_ACP_ARGS", '--bridge "two words"')
    monkeypatch.setenv("CODEX_ACP_BASE_URL", "acp+tcp://localhost:9876")
    creds = resolve_external_process_provider_credentials("codex-acp")
    assert creds["args"] == ["--bridge", "two words"]
    assert creds["base_url"] == "acp+tcp://localhost:9876"
    monkeypatch.delenv("HERMES_COPILOT_ACP_ARGS", raising=False)
    assert CopilotACPClient()._acp_args == ["--acp", "--stdio"]
    assert CopilotACPClient(args=[])._acp_args == []
