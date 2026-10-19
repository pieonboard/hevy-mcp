class HevyError(Exception):
    pass


class AuthenticationError(HevyError):
    pass


class AuthorizationError(HevyError):
    pass


class NotFoundError(HevyError):
    pass


class ConflictError(HevyError):
    pass


class MutationIndeterminateError(HevyError):
    """A mutation received an auth/transport failure and was not replayed."""

    pass


class RateLimitError(HevyError):
    pass


class TransportError(HevyError):
    pass


class RetryableUpstreamError(HevyError):
    pass


class SchemaError(HevyError):
    pass


class EndpointChangedError(HevyError):
    pass


class BackendDriftError(EndpointChangedError):
    pass


class IdentityMismatchError(HevyError):
    pass


class SnapshotError(HevyError):
    pass


class ConfigurationError(HevyError):
    pass
