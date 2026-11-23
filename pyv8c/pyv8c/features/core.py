"""Eager feature implementation for page-essential Web APIs."""

from .runtime import FeatureImplementation
from .core_registry import core_implementations
from ..foundation.environment import BrowserEnvironment
from ..web.bom import navigation, screen, storage


def _local_storage(window):
    services = window._services
    area = services.context_services.local_storage_area(window._storage_origin) if services else None
    return storage.Storage(area=area)


FEATURE = FeatureImplementation(
    core_implementations(),
    {
        "location": lambda owner: navigation.Location(services=owner._services, window=owner),
        "screen": lambda owner: screen.Screen(environment=BrowserEnvironment.from_dict(getattr(getattr(owner._services, "realm_services", None), "environment", None))),
        "localStorage": _local_storage,
        "sessionStorage": lambda owner: storage.Storage(),
        "history": lambda owner: navigation.History(services=owner._services, initial_url=owner.location.href),
    },
)
