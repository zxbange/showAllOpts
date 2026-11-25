"""Shared safe error vocabulary for relational collection writers."""


class WriterFailure(RuntimeError):
    """Safe public classification only; original driver message is not retained."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def writer_error_code(exc: object) -> str:
    original = getattr(exc, 'orig', exc)
    args = getattr(original, 'args', ())
    number = getattr(original, 'database_error_number', None)
    if number is None:
        number = args[0] if args and isinstance(args[0], int) else None
    if number in {1044, 1045, 1142, 1143, 1227, 1370}:
        return 'writer_permission_denied'
    if number == 1146:
        return 'writer_table_missing'
    if number == 1049:
        return 'writer_database_missing'
    if number in {2002, 2003, 2005, 2006, 2013, 2055}:
        return 'writer_connection_failed'
    return 'writer_write_failed'
