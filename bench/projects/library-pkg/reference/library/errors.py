class LibraryError(Exception):
    pass


class NotFound(LibraryError):
    pass


class NotAvailable(LibraryError):
    pass


class LimitReached(LibraryError):
    pass
