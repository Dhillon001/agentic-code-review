def mean(values):
    if not values:
        raise ValueError("mean() of empty sequence")
    return sum(values) / len(values)
