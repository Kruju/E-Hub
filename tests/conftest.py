def pytest_addoption(parser):
    parser.addoption("--dsn", action="store", required=True, help="PostgreSQL DSN to test against")
