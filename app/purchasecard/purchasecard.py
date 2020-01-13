from fastapi import FastAPI

from app.commons.api.exceptions import register_base_payment_exception_handler
from app.commons.auth.service_auth import ApiSecretRouteAuthorizer
from app.commons.config.app_config import AppConfig
from app.commons.context.app_context import AppContext, set_context_for_app
from app.commons.routing import default_payment_router_builder
from app.middleware.doordash_metrics import ServiceMetricsMiddleware
from app.purchasecard.api import (
    auth,
    card,
    user,
    webhook,
    jit_funding,
    transaction,
    exemption,
    store_metadata,
)


def make_purchasecard_v0_marqeta_app(context: AppContext, config: AppConfig) -> FastAPI:
    # Declare sub app
    app_v0 = FastAPI(
        openapi_prefix="/purchasecard/api/v0/marqeta",
        description="purchasecard service v0 for marqeta",
    )
    set_context_for_app(app_v0, context)

    # allow tracking of service-level metrics
    app_v0.add_middleware(
        ServiceMetricsMiddleware,
        application_name="purchasecard-v0",
        host=config.STATSD_SERVER,
        config=config.PAYOUT_STATSD_CONFIG,
    )

    # Mount routers
    default_payment_router_builder().add_common_dependencies(
        ApiSecretRouteAuthorizer(config.PURCHASECARD_SERVICE_ID)
    ).add_sub_routers_with_prefix(
        {
            "/user": user.v0.router,
            "/card": card.v0.router,
            "/webhook": webhook.v0.router,
            "/jit_funding": jit_funding.v0.router,
            "/transaction": transaction.v0.router,
            "/auth": auth.v0.router,
            "/exemption": exemption.v0.router,
            "/store_metadata": store_metadata.v0.router,
        }
    ).attach_to_app(
        app_v0
    )

    register_base_payment_exception_handler(app_v0)

    return app_v0