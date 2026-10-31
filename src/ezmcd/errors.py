class EzMcdError(Exception):
    """A bounded, non-secret error code suitable for CLI/MCP responses."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
