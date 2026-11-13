"""Explicit budgets for one official news scan; no daemon dependencies."""


def validate_budget(arguments):
    fields = {"max_pages": 100, "max_articles": 1000, "failure_backoff": 86400}
    if (
        not isinstance(arguments, dict)
        or set(arguments) != set(fields)
        or any(
            type(arguments[k]) is not int or not 1 <= arguments[k] <= maximum
            for k, maximum in fields.items()
        )
    ):
        raise ValueError(
            "news 需要显式 max_pages(1..100)、max_articles(1..1000)、failure_backoff(1..86400 秒)"
        )
    return arguments
