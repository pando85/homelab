def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "long: closed-loop simulations of the comfort controller (14-21 simulated days each, about "
        "2 min in total); deselect with -m 'not long'",
    )
