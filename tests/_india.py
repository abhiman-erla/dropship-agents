from dropship.config import Settings

# The pre-US-demo (amazon.in, Keepa) defaults, kept explicit for tests written against them.
INDIA = dict(
    data_source="keepa",
    keepa_domain=10, price_min=300.0, price_max=3000.0, max_sales_rank=50000,
    shipping_cost=60.0, max_order_value=5000.0, daily_spend_cap=15000.0,
    max_asins_per_category=100,
)


def india(**kw) -> Settings:
    return Settings(**{**INDIA, **kw})
