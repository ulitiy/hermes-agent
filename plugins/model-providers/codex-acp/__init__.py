"""Local Codex CLI ACP provider, registered through the upstream process seam."""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class CodexACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs: Any) -> Any:
        from agent.copilot_acp_client import CopilotACPClient
        from hermes_cli.auth import resolve_external_process_provider_credentials

        # Router-created and explicitly configured agents share the same launch spec.
        if not client_kwargs.get("command") and not client_kwargs.get("acp_command"):
            creds = resolve_external_process_provider_credentials(self.name)
            client_kwargs["command"] = creds["command"]
            if client_kwargs.get("args") is None and client_kwargs.get("acp_args") is None:
                client_kwargs["args"] = creds["args"]
        return CopilotACPClient(**client_kwargs)


register_provider(CodexACPProfile(
    name="codex-acp", aliases=("codex-acp-agent", "codex-cli-acp"),
    display_name="Codex CLI ACP", description="Codex CLI ACP (spawns codex-acp over stdio)",
    base_url="acp://codex", auth_type="external_process",
    env_vars=("CODEX_ACP_BASE_URL",),
    process_command="codex-acp", process_args=(),
    process_command_env_vars=("HERMES_CODEX_ACP_COMMAND",),
    process_args_env_var="HERMES_CODEX_ACP_ARGS",
    fallback_models=("codex-acp",), supports_model_listing=False,
    supports_health_check=False,
))
