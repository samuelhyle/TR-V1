"""Optional, versioned marketplace models. Dependencies load only when used."""

from importlib import import_module

_EXPORTS = {
    "sale_probability": ["SaleProbabilityModel", "train_sale_probability",
                         "predict_sale_probability", "persist_predictions", "evaluate_model"],
    "price_trajectory": ["train_price_trajectory", "predict_price_trajectories",
                         "persist_trajectories", "evaluate_price_trajectory", "price_trajectory_readiness"],
    "category_trends": ["compute_category_trends", "compute_trend_forecasts", "persist_trends",
                        "persist_forecasts"],
    "seller_archetypes": ["train_seller_archetypes", "persist_archetypes"],
    "market_concentration": ["compute_category_concentration", "persist_concentration"],
}
_ALIASES = {"trend_summary": "category_trends", "archetype_summary": "seller_archetypes",
            "concentration_summary": "market_concentration"}
__all__ = [name for names in _EXPORTS.values() for name in names] + list(_ALIASES)


def __getattr__(name):
    for module, names in _EXPORTS.items():
        if name in names:
            return getattr(import_module(f".{module}", __name__), name)
    if name in _ALIASES:
        return import_module(f".{_ALIASES[name]}", __name__).summary
    raise AttributeError(name)
