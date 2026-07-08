from . import tax


def compute(value):
    # same-module bare call + dotted call through a `from . import` module
    return helper(value) + tax.compute(value)


def helper(value):
    return value
