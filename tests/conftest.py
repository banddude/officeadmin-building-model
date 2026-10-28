"""Opt-in runtime fixtures; base engine checks do not require PDF conversion."""
import pytest


@pytest.fixture
def sheet_pdf_runtime():
    """Run real PDF checks only when their documented optional runtime exists.

    Missing-dependency/failure tests intentionally do not request this fixture.
    The pdf extra and native Cairo remain optional production dependencies.
    """
    from oabm.drawings.sheet_pdf import _dependencies
    try:
        return _dependencies()
    except ImportError as exc:
        pytest.skip(str(exc))
