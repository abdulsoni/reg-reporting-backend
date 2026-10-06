class AppError(Exception):
    """Base class for errors that map directly to an HTTP response."""

    status_code = 400


class NotFoundError(AppError):
    """Raised when an application resource does not exist."""

    status_code = 404


class PdfParseError(AppError):
    """Raised when an uploaded document cannot be read as a PDF."""


class LineageError(AppError):
    """Raised when a lineage connection or query cannot be completed."""


class SystemConnectionError(AppError):
    """Raised when a registered system cannot be reached or introspected."""


class ReportError(AppError):
    """Raised when a report or one of its datapoints cannot be resolved."""


class ReportNotFound(NotFoundError, ReportError):
    """Raised when a report id does not exist."""


class MetadataError(LineageError):
    """Raised when lineage metadata for a system/table/attribute is unusable."""


class AmbiguousMetadataError(MetadataError):
    """Raised when several metadata rows match a datapoint equally well."""


class TraceError(LineageError):
    """Raised when a lineage trace cannot be created or resumed."""


class SeedError(AppError):
    """Raised when the demo system databases cannot be (re)built."""