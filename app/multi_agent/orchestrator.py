"""Compatibility facade: router and specialists are nodes of the shared Graph."""
from app.agent.chat import EcomAgent
from app.config.settings import settings


class MultiAgentOrchestrator(EcomAgent):
    def __init__(self, *args, router_strategy="hybrid", config=None, **kwargs):
        config=(config or settings).model_copy(update={"multi_agent_enabled":True,"router_strategy":router_strategy})
        super().__init__(*args, config=config, **kwargs)
