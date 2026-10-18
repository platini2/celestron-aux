import pytest

from celestron_aux import catalog


@pytest.fixture(autouse=True)
def isolated_user_catalog(tmp_path):
    """Every test gets its own empty names file; nobody touches ~/.celestron-aux."""
    previous = catalog.user_catalog()
    user = catalog.UserCatalog(tmp_path / "catalog.json")
    catalog.use_user_catalog(user)
    yield user
    catalog.use_user_catalog(previous)
