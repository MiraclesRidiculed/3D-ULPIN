"""API routers.

Included by ``app.main`` in the order the original monolith declared its
routes, so route precedence (notably ``/ulpin/{ulpin}`` before
``/ulpin/generate``) is unchanged.

``reviews`` and ``audit`` are appended last: they are additive and must not
perturb the precedence of anything that came before.
"""
from app.api import (
    analytics,
    audit,
    buildings,
    changes,
    imports,
    infrastructure,
    parcels,
    point_clouds,
    processing,
    provenance,
    properties,
    reviews,
    ulpin,
    validation,
)

#: Registration order matters for overlapping paths.
ROUTERS = (
    parcels.router,
    buildings.router,
    properties.router,
    infrastructure.router,
    ulpin.router,
    processing.router,
    validation.router,
    analytics.router,
    imports.router,
    point_clouds.router,
    reviews.router,
    audit.router,
    changes.router,
    provenance.router,
)

__all__ = ["ROUTERS"]
