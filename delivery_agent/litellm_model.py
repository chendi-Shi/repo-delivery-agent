"""Pinned LiteLLM protocol with explicit unknown-price accounting metadata."""
import math

from minisweagent.models.litellm_model import LitellmModel


class CostAwareLitellmModel(LitellmModel):
    """Keep upstream tools/retries/usage; distinguish its zero fallback from price.

    In mini-swe-agent 2.4.6, ignore_errors returns zero when price calculation
    fails or produces a nonpositive amount. Default mode raises instead. The
    numeric fallback is required by DefaultAgent's additive budget bookkeeping;
    reported_cost is the user-facing amount and remains unknown in this case.
    """
    def _calculate_cost(self, response):
        result = super()._calculate_cost(response)
        cost = result["cost"]
        unknown = type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0
        if unknown and self.config.cost_tracking != "ignore_errors":
            raise RuntimeError("Provider monetary cost is not a finite positive amount")
        return {**result, "cost": 0.0 if unknown else cost, "cost_unknown": unknown,
                "reported_cost": None if unknown else result["cost"],
                "cost_source": "unknown_provider_pricing" if unknown else "provider_price_registry"}

    def serialize(self):
        result = super().serialize()
        result["info"]["cost_policy"] = {
            "mode": self.config.cost_tracking,
            "unknown_price": "continue with unknown monetary cost" if self.config.cost_tracking == "ignore_errors"
                             else "stop if monetary cost cannot be calculated",
            "cost_limit_scope": "known_costs_only; step and wall-time limits remain active"}
        return result
