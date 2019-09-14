import uuid
from asyncio import gather
from dataclasses import dataclass
from datetime import datetime, timedelta
from structlog.stdlib import BoundLogger
from typing import Tuple, List, Optional, Callable, Dict, Any
from typing_extensions import final

from fastapi import Depends
from stripe.error import StripeError, InvalidRequestError
from stripe.util import convert_to_stripe_object

from app.commons import tracing
from app.commons.context.app_context import AppContext, get_global_app_context
from app.commons.context.req_context import (
    ReqContext,
    get_context_from_req,
    get_logger_from_req,
    get_stripe_async_client_from_req,
)
from app.commons.providers.stripe.stripe_client import StripeAsyncClient
from app.commons.providers.stripe.stripe_models import (
    CapturePaymentIntent,
    CreatePaymentIntent,
    CancelPaymentIntent,
    RefundCharge,
    PaymentIntent as ProviderPaymentIntent,
    Refund as ProviderRefund,
)
from app.commons.types import CountryCode, LegacyCountryId, CurrencyType
from app.commons.utils.types import PaymentProvider
from app.payin.core.cart_payment.model import (
    CartPayment,
    CorrelationIds,
    LegacyPayment,
    LegacyConsumerCharge,
    LegacyStripeCharge,
    LegacyCorrelationIds,
    PaymentIntent,
    PgpPaymentIntent,
    PaymentCharge,
    PgpPaymentCharge,
)
from app.payin.core.cart_payment.types import (
    CaptureMethod,
    ChargeStatus,
    ConfirmationMethod,
    IntentStatus,
    LegacyStripeChargeStatus,
)
from app.payin.core.exceptions import (
    PayinErrorCode,
    CartPaymentCreateError,
    CartPaymentReadError,
    PaymentIntentCancelError,
    PaymentIntentRefundError,
    PaymentChargeRefundError,
    PaymentIntentCouldNotBeUpdatedError,
    PaymentIntentConcurrentAccessError,
    PaymentIntentNotInRequiresCaptureState,
    InvalidProviderRequestError,
    ProviderError,
)
from app.payin.core.payer.processor import PayerClient
from app.payin.core.payment_method.processor import PaymentMethodClient
from app.payin.core.types import PayerIdType, PaymentMethodIdType
from app.payin.repository.cart_payment_repo import CartPaymentRepository


@final
@dataclass
class PaymentResourceIds:
    provider_payment_resource_id: str
    provider_customer_resource_id: str


class LegacyPaymentInterface:
    DEFAULT_COUNTRY_ID = LegacyCountryId.US

    def __init__(
        self,
        app_context: AppContext = Depends(get_global_app_context),
        req_context: ReqContext = Depends(get_context_from_req),
        payment_repo: CartPaymentRepository = Depends(
            CartPaymentRepository.get_repository
        ),
        stripe_async_client: StripeAsyncClient = Depends(
            get_stripe_async_client_from_req
        ),
    ):
        self.app_context = app_context
        self.req_context = req_context
        self.payment_repo = payment_repo
        self.stripe_async_client = stripe_async_client

    def get_country_id_by_code(self, country: str) -> int:
        if country == CountryCode.US.value:
            return LegacyCountryId.US
        elif country == CountryCode.CA.value:
            return LegacyCountryId.CA
        elif country == CountryCode.AU.value:
            return LegacyCountryId.AU
        else:
            self.req_context.log.warn(f"Invalid country {country} provided")
            return self.DEFAULT_COUNTRY_ID

    def _get_legacy_stripe_charge_status_from_provider_status(
        self, provider_status: str
    ) -> LegacyStripeChargeStatus:
        return LegacyStripeChargeStatus(provider_status)

    async def get_associated_cart_payment_id(
        self, charge_id: int
    ) -> Optional[uuid.UUID]:
        # Based on legacy charge_id, look up legacy stripe_charges.  From one, grab the stripe id and use it to find the associated intent pair
        # (we store the same stripe id in the pgp_payment_intent).  One the intent pair is in hand we can return the payment intent's cart_payment_id.
        # This is a round about way to get the associated cart_payment id, but avoids the overhead and complexity of (a) persisting associated info
        # in main db table,s (b) persisting legacy charge info in intents and recovering from partial failures when writing this info to multiple dbs.
        # We keep the higher volume path (regular order creation) simpler at the expense of this more complicated lookup here.
        # TODO: This is probably going away in favor of a direct relationship between payment_intent and legacy_charge_id, which will be introduced
        # for command mode purposes (tbd).
        self.req_context.log.debug(
            f"Looking up stripe charges for charge_id {charge_id}"
        )
        legacy_stripe_charges = await self.payment_repo.get_legacy_stripe_charges_by_charge_id(
            charge_id
        )
        if not legacy_stripe_charges:
            self.req_context.log.warn(f"No stripe charges for charge_id {charge_id}")
            return None

        stripe_charge_id = legacy_stripe_charges[0].stripe_id
        self.req_context.log.debug(
            f"Looking up intent pair for stripe_charge_id {stripe_charge_id}"
        )
        payment_intent, pgp_payment_intent = await self.payment_repo.get_intent_pair_by_provider_charge_id(
            stripe_charge_id
        )
        if not payment_intent:
            return None

        self.req_context.log.debug(
            f"Found cart payment id {payment_intent.cart_payment_id}"
        )
        return payment_intent.cart_payment_id

    async def create_charge_after_payment_submitted(
        self,
        legacy_payment: LegacyPayment,
        correlation_ids: CorrelationIds,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_payment_intent: ProviderPaymentIntent,
    ) -> Tuple[Optional[LegacyConsumerCharge], LegacyStripeCharge]:
        self.req_context.log.debug(
            "[create_charge_after_payment_submitted] Creating charge records in legacy system"
        )
        provider_charges = provider_payment_intent.charges
        provider_charge = provider_charges.data[0]
        is_stripe_connect_based = (
            True if provider_charge.application_fee_amount else False
        )
        country_id = legacy_payment.dd_country_id
        if not country_id:
            country_id = self.get_country_id_by_code(payment_intent.country)

        if legacy_payment.stripe_charge_id:
            # For legacy model, when an adjustment is made, only a stripe charge is inserted, under the
            # existing charge.
            existing_stripe_charge = await self.payment_repo.get_legacy_stripe_charge_by_stripe_id(
                legacy_payment.stripe_charge_id
            )
            if not existing_stripe_charge:
                self.req_context.log.error(
                    "Failed to find legacy stripe charge based on stripe_id {legacy_payment.stripe_charge_id}"
                )
                raise PaymentIntentRefundError(
                    error_code=PayinErrorCode.PAYMENT_INTENT_ADJUST_REFUND_ERROR,
                    retryable=False,
                )
            consumer_charge_id = existing_stripe_charge.charge_id
            # Nothing inserted so None will be returned.  Saves us from another query to maindb to get the full record.
            legacy_consumer_charge = None

            # TODO check if legacy_charge amount needs updating

            self.req_context.log.debug(
                f"Adding new stripe charge under charge {consumer_charge_id}"
            )
        else:
            # Brand new payment, create new consumer charge
            self.req_context.log.debug(
                f"[create_charge_after_payment_submitted] Creating new charge"
            )
            legacy_consumer_charge = await self.payment_repo.insert_legacy_consumer_charge(
                target_ct_id=int(correlation_ids.reference_type),
                target_id=int(correlation_ids.reference_id),
                consumer_id=legacy_payment.dd_consumer_id,
                idempotency_key=payment_intent.idempotency_key,
                is_stripe_connect_based=is_stripe_connect_based,
                country_id=country_id,
                currency=payment_intent.currency,
                # stripe_customer_id=pgp_payment_intent.customer_resource_id,
                stripe_customer_id=None,
                total=payment_intent.amount,
                original_total=payment_intent.amount,
            )
            consumer_charge_id = legacy_consumer_charge.id

        legacy_stripe_charge = await self.payment_repo.insert_legacy_stripe_charge(
            # TODO: additional_payment_info, description
            stripe_id=provider_charge.id,
            card_id=legacy_payment.dd_stripe_card_id,
            charge_id=consumer_charge_id,
            amount=provider_charge.amount,
            amount_refunded=provider_charge.amount_refunded,
            currency=provider_charge.currency,
            status=self._get_legacy_stripe_charge_status_from_provider_status(
                provider_charge.status
            ),
            idempotency_key=payment_intent.idempotency_key,
        )

        return legacy_consumer_charge, legacy_stripe_charge

    async def update_charge_after_payment_captured(
        self, provider_intent: ProviderPaymentIntent
    ) -> LegacyStripeCharge:
        charge = provider_intent.charges.data[0]
        return await self.payment_repo.update_legacy_stripe_charge_status(
            stripe_charge_id=charge.id, status=charge.status
        )

    async def update_charge_after_payment_refunded(
        self, provider_refund: ProviderRefund
    ) -> LegacyStripeCharge:
        return await self.payment_repo.update_legacy_stripe_charge(
            stripe_charge_id=provider_refund.charge,
            amount_refunded=provider_refund.amount,
            refunded_at=datetime.now(),
        )


@tracing.track_breadcrumb(processor_name="cart_payments", only_trackable=False)
class CartPaymentInterface:
    ENABLE_NEW_CHARGE_TABLES = False
    CAPTURE_DELAY_IN_HOURS = 24 * 1

    def __init__(
        self,
        app_context: AppContext = Depends(get_global_app_context),
        req_context: ReqContext = Depends(get_context_from_req),
        payment_repo: CartPaymentRepository = Depends(
            CartPaymentRepository.get_repository
        ),
        payer_client: PayerClient = Depends(PayerClient),
        payment_method_client: PaymentMethodClient = Depends(PaymentMethodClient),
        stripe_async_client: StripeAsyncClient = Depends(
            get_stripe_async_client_from_req
        ),
    ):
        self.app_context = app_context
        self.req_context = req_context
        self.payment_repo = payment_repo
        self.payer_client = payer_client
        self.payment_method_client = payment_method_client
        self.stripe_async_client = stripe_async_client

    def get_most_recent_intent(self, intent_list: List[PaymentIntent]) -> PaymentIntent:
        intent_list.sort(key=lambda x: x.created_at)
        return intent_list[-1]

    async def _get_most_recent_pgp_payment_intent(self, payment_intent: PaymentIntent):
        pgp_intents = await self.payment_repo.find_pgp_payment_intents(
            payment_intent.id
        )
        pgp_intents.sort(key=lambda x: x.created_at)
        return pgp_intents[-1]

    async def get_cart_payment_submission_pgp_intent(
        self, payment_intent: PaymentIntent
    ) -> PgpPaymentIntent:
        # Get pgp intents for this specific intent
        pgp_intents = await self.payment_repo.find_pgp_payment_intents(
            payment_intent.id
        )

        # Since cart_payment/payment_intent/pgp_payment_intent are first created in one transaction,
        # we will have at least one.  Find the first one, since this is an attempt to recreate the
        # cart_payment.
        return pgp_intents[0]

    def _filter_payment_intents_by_state(
        self, intents: List[PaymentIntent], status: IntentStatus
    ) -> List[PaymentIntent]:
        return list(filter(lambda intent: intent.status == status.value, intents))

    def filter_payment_intents_by_idempotency_key(
        self, intents: List[PaymentIntent], idempotency_key: str
    ) -> Optional[PaymentIntent]:
        matched_intents = list(
            filter(lambda intent: intent.idempotency_key == idempotency_key, intents)
        )

        return matched_intents[0] if matched_intents else None

    def get_capturable_payment_intents(self, payment_intents: List[PaymentIntent]):
        return self._filter_payment_intents_by_state(
            payment_intents, IntentStatus.REQUIRES_CAPTURE
        )

    def get_refundable_payment_intents(self, payment_intents: List[PaymentIntent]):
        return self._filter_payment_intents_by_function(
            payment_intents, self.can_payment_intent_be_refunded
        )

    def get_submitted_or_captured_intents(self, payment_intents: List[PaymentIntent]):
        return list(
            filter(
                lambda intent: intent.status
                in [IntentStatus.REQUIRES_CAPTURE, IntentStatus.SUCCEEDED],
                payment_intents,
            )
        )

    def _filter_payment_intents_by_function(
        self,
        payment_intents: List[PaymentIntent],
        filter_function: Callable[[PaymentIntent], bool],
    ) -> List[PaymentIntent]:
        return list(filter(lambda intent: filter_function(intent), payment_intents))

    def is_payment_intent_submitted(self, payment_intent: PaymentIntent) -> bool:
        return payment_intent.status != IntentStatus.INIT

    def can_payment_intent_be_cancelled(self, payment_intent: PaymentIntent) -> bool:
        # Not yet captured.  SCA related states will be added here later.
        return payment_intent.status in [IntentStatus.REQUIRES_CAPTURE]

    def can_payment_intent_be_refunded(self, payment_intent: PaymentIntent) -> bool:
        return payment_intent.status == IntentStatus.SUCCEEDED

    def does_intent_require_capture(self, payment_intent: PaymentIntent) -> bool:
        return payment_intent.status == IntentStatus.REQUIRES_CAPTURE

    def _get_intent_status_from_provider_status(
        self, provider_status: str
    ) -> IntentStatus:
        return IntentStatus(provider_status)

    def _get_charge_status_from_intent_status(
        self, intent_status: IntentStatus
    ) -> ChargeStatus:
        # Charge status is a subset of Intent status
        return ChargeStatus(intent_status)

    def is_amount_adjusted_higher(self, cart_payment: CartPayment, amount: int) -> bool:
        return amount > cart_payment.amount

    def is_amount_adjusted_lower(self, cart_payment: CartPayment, amount: int) -> bool:
        return amount < cart_payment.amount

    def _transform_method_for_stripe(self, method_name: str) -> str:
        if method_name == "auto":
            return "automatic"
        return method_name

    def _get_provider_capture_method(
        self, pgp_payment_intent: PgpPaymentIntent
    ) -> CreatePaymentIntent.CaptureMethod:
        target_method = self._transform_method_for_stripe(
            pgp_payment_intent.capture_method
        )
        return CreatePaymentIntent.CaptureMethod(target_method)

    def _get_provider_confirmation_method(
        self, pgp_payment_intent: PgpPaymentIntent
    ) -> CreatePaymentIntent.ConfirmationMethod:
        target_method = self._transform_method_for_stripe(
            pgp_payment_intent.confirmation_method
        )
        return CreatePaymentIntent.ConfirmationMethod(target_method)

    def _get_provider_future_usage(self, payment_intent: PaymentIntent) -> str:
        if payment_intent.capture_method == CaptureMethod.AUTO:
            return CreatePaymentIntent.SetupFutureUsage.ON_SESSION

        return CreatePaymentIntent.SetupFutureUsage.OFF_SESSION

    async def find_existing_payment(
        self, payer_id: Optional[uuid.UUID], idempotency_key: str
    ) -> Tuple[Optional[CartPayment], Optional[LegacyPayment], Optional[PaymentIntent]]:
        # TODO support legacy payment case, where there is no payer_id
        payment_intent = await self.payment_repo.get_payment_intent_for_idempotency_key(
            idempotency_key
        )

        if not payment_intent:
            return None, None, None

        cart_payment, legacy_payment = await self.payment_repo.get_cart_payment_by_id(
            payment_intent.cart_payment_id
        )

        return cart_payment, legacy_payment, payment_intent

    async def get_cart_payment(
        self, cart_payment_id: uuid.UUID
    ) -> Tuple[Optional[CartPayment], Optional[LegacyPayment]]:
        return await self.payment_repo.get_cart_payment_by_id(cart_payment_id)

    async def get_cart_payment_intents(self, cart_payment) -> List[PaymentIntent]:
        return await self.payment_repo.get_payment_intents_for_cart_payment(
            cart_payment.id
        )

    def is_accessible(
        self,
        cart_payment: CartPayment,
        request_payer_id: Optional[uuid.UUID],
        credential_owner: str,
    ) -> bool:
        # TODO verify the caller (as identified by the provided credentials for this request) owns the cart payment
        # From credential_owner, get payer_id
        # return cart_payment.payer_id == payer_id and cart_payment.payer_id == request_payer_id
        return True

    def is_capture_immediate(self, payment_intent: PaymentIntent) -> bool:
        # TODO control percentage of intents for delayed capture here with a config parameter
        return False
        # return True

    async def create_new_payment(
        self,
        request_cart_payment: CartPayment,
        legacy_payment: LegacyPayment,
        provider_payment_resource_id: str,
        provider_customer_resource_id: str,
        provider_metadata: Optional[Dict[str, Any]],
        idempotency_key: str,
        country: str,
        currency: str,
        client_description: Optional[str],
    ) -> Tuple[CartPayment, PaymentIntent, PgpPaymentIntent]:
        # Create a new cart payment, with associated models
        self.req_context.log.info(
            f"Creating new payment for payer {request_cart_payment.payer_id}, idempotency_key {idempotency_key}"
        )

        # Capture after
        capture_after = None
        if request_cart_payment.delay_capture:
            capture_after = datetime.utcnow() + timedelta(
                hours=self.CAPTURE_DELAY_IN_HOURS
            )

        # Capture Method
        capture_method = (
            CaptureMethod.MANUAL
            if request_cart_payment.delay_capture
            else CaptureMethod.AUTO
        )

        async with self.payment_repo.payment_database_transaction():
            # Create CartPayment

            cart_payment = await self.payment_repo.insert_cart_payment(
                id=request_cart_payment.id,
                payer_id=request_cart_payment.payer_id,
                client_description=request_cart_payment.client_description,
                reference_id=request_cart_payment.correlation_ids.reference_id,
                reference_type=request_cart_payment.correlation_ids.reference_type,
                amount_original=request_cart_payment.amount,
                amount_total=request_cart_payment.amount,
                delay_capture=request_cart_payment.delay_capture,
                metadata=request_cart_payment.metadata,
                # Legacy fields are associated with the cart_payment instance to support idempotency and
                # adjusting amount without the client having to provide full payment info again.  But these
                # fields are considered deprecated and will be removed once clients upgrade to new payin API.
                legacy_consumer_id=getattr(legacy_payment, "dd_consumer_id", None),
                legacy_stripe_card_id=getattr(
                    legacy_payment, "dd_stripe_card_id", None
                ),
                legacy_provider_customer_id=getattr(
                    legacy_payment, "stripe_customer_id", None
                ),
                legacy_provider_payment_method_id=getattr(
                    legacy_payment, "stripe_payment_method_id", None
                ),
                legacy_provider_card_id=getattr(legacy_payment, "stripe_card_id", None),
            )

            payment_intent, pgp_payment_intent = await self._create_new_intent_pair(
                cart_payment=request_cart_payment,
                idempotency_key=idempotency_key,
                payment_method_id=request_cart_payment.payment_method_id,
                provider_payment_resource_id=provider_payment_resource_id,
                provider_customer_resource_id=provider_customer_resource_id,
                provider_metadata=provider_metadata,
                amount=request_cart_payment.amount,
                country=country,
                currency=currency,
                capture_method=capture_method,
                payer_statement_description=request_cart_payment.payer_statement_description,
                capture_after=capture_after,
            )

        self.req_context.log.debug(
            f"[submit_new_payment] insert payment_intent objects completed"
        )

        return cart_payment, payment_intent, pgp_payment_intent

    async def _create_new_charge_pair(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_intent: ProviderPaymentIntent,
        status: ChargeStatus,
    ) -> Tuple[PaymentCharge, PgpPaymentCharge]:

        payment_charge = await self.payment_repo.insert_payment_charge(
            id=uuid.uuid4(),
            payment_intent_id=payment_intent.id,
            provider=PaymentProvider.STRIPE.value,
            idempotency_key=str(uuid.uuid4()),  # TODO handle idempotency key
            status=status,
            currency=payment_intent.currency,
            amount=payment_intent.amount,
            amount_refunded=0,
            application_fee_amount=payment_intent.application_fee_amount,
            payout_account_id=pgp_payment_intent.payout_account_id,
        )

        provider_charges = provider_intent.charges
        if len(provider_charges.data) > 1:
            # Upon creation, there is expected to be one provider charge.
            self.req_context.log.warn(
                f"Multiple pgp charges at time of creation for intent {payment_intent.id}, pgp intent {pgp_payment_intent.id}"
            )
        provider_charge = provider_charges.data[0]
        pgp_payment_charge = await self.payment_repo.insert_pgp_payment_charge(
            id=uuid.uuid4(),
            payment_charge_id=payment_charge.id,
            provider=PaymentProvider.STRIPE.value,
            idempotency_key=payment_charge.idempotency_key,
            status=status,
            currency=provider_charge.currency,
            amount=provider_charge.amount,
            amount_refunded=provider_charge.amount_refunded,
            application_fee_amount=provider_charge.application_fee_amount,
            payout_account_id=provider_charge.on_behalf_of,
            resource_id=provider_charge.id,
            intent_resource_id=provider_charge.payment_intent,
            invoice_resource_id=provider_charge.invoice,
            payment_method_resource_id=provider_charge.payment_method,
        )

        return payment_charge, pgp_payment_charge

    async def _create_new_intent_pair(
        self,
        cart_payment: CartPayment,
        idempotency_key: str,
        payment_method_id: Optional[uuid.UUID],
        provider_payment_resource_id: str,
        provider_customer_resource_id: str,
        provider_metadata: Optional[Dict[str, Any]],
        amount: int,
        country: str,
        currency: str,
        capture_method: str,
        capture_after: Optional[datetime],
        payer_statement_description: Optional[str] = None,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        # Create PaymentIntent
        payment_intent = await self.payment_repo.insert_payment_intent(
            id=uuid.uuid4(),
            cart_payment_id=cart_payment.id,
            idempotency_key=idempotency_key,
            amount_initiated=amount,
            amount=amount,
            application_fee_amount=getattr(
                cart_payment.split_payment, "application_fee_amount", None
            ),
            country=country,
            currency=currency,
            capture_method=capture_method,
            confirmation_method=ConfirmationMethod.MANUAL,
            status=IntentStatus.INIT,
            statement_descriptor=payer_statement_description,
            capture_after=capture_after,
            payment_method_id=payment_method_id,
            metadata=provider_metadata,
        )

        # Create PgpPaymentIntent
        pgp_payment_intent = await self.payment_repo.insert_pgp_payment_intent(
            id=uuid.uuid4(),
            payment_intent_id=payment_intent.id,
            idempotency_key=idempotency_key,
            provider=PaymentProvider.STRIPE.value,
            payment_method_resource_id=provider_payment_resource_id,
            customer_resource_id=provider_customer_resource_id,
            currency=currency,
            amount=amount,
            application_fee_amount=getattr(
                cart_payment.split_payment, "application_fee_amount", None
            ),
            payout_account_id=getattr(
                cart_payment.split_payment, "payout_account_id", None
            ),
            capture_method=capture_method,
            confirmation_method=ConfirmationMethod.MANUAL,
            status=IntentStatus.INIT,
            statement_descriptor=payer_statement_description,
        )

        return payment_intent, pgp_payment_intent

    async def submit_payment_to_provider(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_payment_resource_id: str,
        provider_customer_resource_id: str,
    ) -> ProviderPaymentIntent:
        # Call to stripe payment intent API
        try:
            intent_request = CreatePaymentIntent(
                amount=pgp_payment_intent.amount,
                currency=pgp_payment_intent.currency,
                application_fee_amount=pgp_payment_intent.application_fee_amount,
                capture_method=self._get_provider_capture_method(pgp_payment_intent),
                confirm=True,
                confirmation_method=self._get_provider_confirmation_method(
                    pgp_payment_intent
                ),
                on_behalf_of=pgp_payment_intent.payout_account_id,
                setup_future_usage=self._get_provider_future_usage(payment_intent),
                payment_method=provider_payment_resource_id,
                customer=provider_customer_resource_id,
                statement_descriptor=payment_intent.statement_descriptor,
                metadata=payment_intent.metadata,
            )

            self.req_context.log.debug(
                f"[submit_payment_to_provider] Calling provider to create payment intent"
            )
            response = await self.stripe_async_client.create_payment_intent(
                country=CountryCode(payment_intent.country),
                request=intent_request,
                idempotency_key=pgp_payment_intent.idempotency_key,
            )
            return response
        except StripeError as e:
            self.req_context.log.error(
                f"Error invoking provider to create a payment intent: {e}"
            )
            raise CartPaymentCreateError(
                error_code=PayinErrorCode.PAYMENT_INTENT_CREATE_STRIPE_ERROR,
                retryable=False,
            )

    async def update_payment_after_submission_to_provider(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_payment_intent: ProviderPaymentIntent,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        self.req_context.log.debug(
            f"Updating state for payment with intent id {payment_intent.id}"
        )
        target_intent_status = self._get_intent_status_from_provider_status(
            provider_payment_intent.status
        )
        async with self.payment_repo.payment_database_transaction():
            # Update the records we created to reflect that the provider has been invoked.
            # Cannot gather calls here because of shared connection/transaction
            updated_intent = await self.payment_repo.update_payment_intent_status(
                id=payment_intent.id,
                new_status=target_intent_status,
                previous_status=payment_intent.status,
            )
            updated_pgp_intent = await self.payment_repo.update_pgp_payment_intent(
                id=pgp_payment_intent.id,
                status=target_intent_status,
                resource_id=provider_payment_intent.id,
                charge_resource_id=provider_payment_intent.charges.data[0].id,
            )
            if self.ENABLE_NEW_CHARGE_TABLES:
                await self._create_new_charge_pair(
                    payment_intent=payment_intent,
                    pgp_payment_intent=pgp_payment_intent,
                    provider_intent=provider_payment_intent,
                    status=self._get_charge_status_from_intent_status(
                        target_intent_status
                    ),
                )

        return updated_intent, updated_pgp_intent

    async def acquire_for_capture(self, payment_intent: PaymentIntent):
        # Throws exception if current state is not same as intent, meaning another request/process has already transitioned
        # intent to a different state.
        return await self.payment_repo.update_payment_intent_status(
            id=payment_intent.id,
            new_status=IntentStatus.CAPTURING,
            previous_status=payment_intent.status,
        )

    async def submit_capture_to_provider(
        self, payment_intent: PaymentIntent, pgp_payment_intent: PgpPaymentIntent
    ) -> ProviderPaymentIntent:
        # Call to stripe payment intent API
        try:
            intent_request = CapturePaymentIntent(sid=pgp_payment_intent.resource_id)

            self.req_context.log.info(
                f"Capturing payment intent: {payment_intent.country}, key: {pgp_payment_intent.idempotency_key}"
            )
            # Make call to Stripe
            provider_intent = await self.stripe_async_client.capture_payment_intent(
                country=CountryCode(payment_intent.country),
                request=intent_request,
                idempotency_key=str(uuid.uuid4()),  # TODO handle idempotency key
            )
        except InvalidRequestError as e:
            provider_intent = convert_to_stripe_object(
                e.json_body["error"]["payment_intent"]
            )
            # Payment intent has already been captured
            if (
                e.code == "payment_intent_unexpected_state"
                and provider_intent.status == "succeeded"
            ):
                pass
            else:
                raise InvalidProviderRequestError(e)
        except StripeError as e:
            # All other Stripe errors (ie. not InvalidRequestError) can be considered retryable errors
            # Re-setting the state back to REQUIRES_CAPTURE allows it to be picked up again by the regular cron
            await self.payment_repo.update_payment_intent_status(
                id=payment_intent.id,
                new_status=IntentStatus.REQUIRES_CAPTURE,
                previous_status=payment_intent.status,
            )
            self.req_context.log.exception(f"Provider error: {e}")
            raise ProviderError(e)
        except Exception as e:
            await self.payment_repo.update_payment_intent_status(
                id=payment_intent.id,
                new_status=IntentStatus.CAPTURE_FAILED,
                previous_status=payment_intent.status,
            )
            self.req_context.log.error(
                f"Unknown error capturing intent with provider: {e}"
            )
            raise e

        return provider_intent

    async def update_payment_after_capture_with_provider(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_payment_intent: ProviderPaymentIntent,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        new_intent_status = self._get_intent_status_from_provider_status(
            provider_payment_intent.status
        )
        new_charge_status = self._get_charge_status_from_intent_status(
            new_intent_status
        )
        self.req_context.log.debug(
            f"Updating intent {payment_intent.id}, pgp intent {pgp_payment_intent.id} to status {new_intent_status}"
        )

        # Update state
        async with self.payment_repo.payment_database_transaction():
            # TODO try gather
            updated_payment_intent = await self.payment_repo.update_payment_intent(
                id=payment_intent.id,
                status=new_intent_status,
                amount_received=payment_intent.amount,
                captured_at=datetime.utcnow(),
            )
            updated_pgp_payment_intent = await self.payment_repo.update_pgp_payment_intent_status(
                pgp_payment_intent.id, new_intent_status
            )

            if self.ENABLE_NEW_CHARGE_TABLES:
                await self._update_charge_pair_after_capture(
                    payment_intent, new_charge_status, provider_payment_intent
                )

        return updated_payment_intent, updated_pgp_payment_intent

    async def _update_charge_pair_after_capture(
        self,
        payment_intent: PaymentIntent,
        status: ChargeStatus,
        provider_intent: ProviderPaymentIntent,
    ) -> Tuple[PaymentCharge, PgpPaymentCharge]:
        # Assumption: this is called within a transaction already
        payment_charge = await self.payment_repo.update_payment_charge_status(
            payment_intent.id, status.value
        )
        pgp_charge = await self._update_pgp_charge_from_provider(
            payment_charge.id, status, provider_intent
        )
        return payment_charge, pgp_charge

    async def cancel_provider_payment_charge(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        reason,
    ) -> str:
        try:
            intent_request = CancelPaymentIntent(
                sid=pgp_payment_intent.resource_id, cancellation_reason=reason
            )

            self.req_context.log.info(
                f"Cancelling payment intent: {payment_intent.id}, key: {pgp_payment_intent.idempotency_key}"
            )
            response = await self.stripe_async_client.cancel_payment_intent(
                country=CountryCode(payment_intent.country),
                request=intent_request,
                idempotency_key=str(uuid.uuid4()),  # TODO handle idempotency key
            )
            self.req_context.log.debug(f"Provider response: {response}")
            return response
        except Exception as e:
            self.req_context.log.error(f"Error refunding payment with provider: {e}")
            raise PaymentChargeRefundError(
                error_code=PayinErrorCode.PAYMENT_INTENT_ADJUST_REFUND_ERROR,
                retryable=False,
            )

    async def refund_provider_payment(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        reason: str,
        refund_amount: int,
    ) -> ProviderRefund:
        try:
            refund_request = RefundCharge(
                charge=pgp_payment_intent.charge_resource_id,
                amount=refund_amount,
                reason=reason,
            )

            self.req_context.log.info(
                f"Refunding charge {pgp_payment_intent.charge_resource_id}"
            )
            response = await self.stripe_async_client.refund_charge(
                country=CountryCode(payment_intent.country),
                request=refund_request,
                idempotency_key=str(uuid.uuid4()),  # TODO handle idempotency key
            )
            self.req_context.log.debug(f"Provider response: {response}")
            return response
        except Exception as e:
            self.req_context.log.error(f"Error cancelling charge with provider: {e}")
            raise PaymentIntentCancelError(
                error_code=PayinErrorCode.PAYMENT_INTENT_ADJUST_REFUND_ERROR,
                retryable=False,
            )

    async def _update_pgp_charge_from_provider(
        self,
        payment_charge_id: uuid.UUID,
        status: ChargeStatus,
        provider_intent: ProviderPaymentIntent,
    ):
        charge = provider_intent.charges.data[0]
        return await self.payment_repo.update_pgp_payment_charge(
            payment_charge_id=payment_charge_id,
            status=status.value,
            amount=charge.amount,
            amount_refunded=charge.amount_refunded,
        )

    async def _update_charge_pair_after_refund(
        self, payment_intent: PaymentIntent, provider_refund: ProviderRefund
    ) -> Tuple[PaymentCharge, PgpPaymentCharge]:
        # Assumption: this is called within a transaction already
        status = ChargeStatus(provider_refund.status)
        payment_charge = await self.payment_repo.update_payment_charge(
            payment_intent_id=payment_intent.id,
            status=status.value,
            amount_refunded=provider_refund.amount,
        )
        pgp_charge = await self.payment_repo.update_pgp_payment_charge(
            payment_charge_id=payment_charge.id,
            status=status.value,
            amount=payment_intent.amount,
            amount_refunded=provider_refund.amount,
        )
        return payment_charge, pgp_charge

    async def _update_charge_pair_after_amount_reduction(
        self, payment_intent: PaymentIntent, amount: int
    ) -> Tuple[PaymentCharge, PgpPaymentCharge]:
        # Assumption: this is called within a transaction already
        payment_charge = await self.payment_repo.update_payment_charge_amount(
            payment_intent_id=payment_intent.id, amount=amount
        )
        pgp_charge = await self.payment_repo.update_pgp_payment_charge_amount(
            payment_charge_id=payment_charge.id, amount=amount
        )
        return payment_charge, pgp_charge

    async def _update_charge_pair_after_cancel(
        self, payment_intent: PaymentIntent, status: ChargeStatus
    ) -> Tuple[PaymentCharge, PgpPaymentCharge]:
        # Assumption: this is called within a transaction already
        payment_charge = await self.payment_repo.update_payment_charge_status(
            payment_intent_id=payment_intent.id, status=status.value
        )
        pgp_payment_charge = await self.payment_repo.update_pgp_payment_charge_status(
            payment_charge_id=payment_charge.id, status=status.value
        )
        return payment_charge, pgp_payment_charge

    async def update_payment_after_cancel_with_provider(
        self, payment_intent: PaymentIntent, pgp_payment_intent: PgpPaymentIntent
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        async with self.payment_repo.payment_database_transaction():
            updated_intent = await self.payment_repo.update_payment_intent_status(
                id=payment_intent.id,
                new_status=IntentStatus.CANCELLED,
                previous_status=payment_intent.status,
            )
            updated_pgp_intent = await self.payment_repo.update_pgp_payment_intent_status(
                id=pgp_payment_intent.id, status=IntentStatus.CANCELLED
            )
            if self.ENABLE_NEW_CHARGE_TABLES:
                await self._update_charge_pair_after_cancel(
                    payment_intent=payment_intent, status=ChargeStatus.CANCELLED
                )

        return updated_intent, updated_pgp_intent

    async def update_payment_after_refund_with_provider(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_refund: ProviderRefund,
        refund_amount: int,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        async with self.payment_repo.payment_database_transaction():
            updated_intent = await self.payment_repo.update_payment_intent_amount(
                id=payment_intent.id, amount=(payment_intent.amount - refund_amount)
            )
            updated_pgp_intent = await self.payment_repo.update_pgp_payment_intent_amount(
                id=pgp_payment_intent.id, amount=(payment_intent.amount - refund_amount)
            )
            if self.ENABLE_NEW_CHARGE_TABLES:
                await self._update_charge_pair_after_refund(
                    payment_intent=payment_intent, provider_refund=provider_refund
                )

        return updated_intent, updated_pgp_intent

    async def increase_payment_amount(
        self,
        amount: int,
        cart_payment: CartPayment,
        existing_payment_intents: List[PaymentIntent],
        idempotency_key: str,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        self.req_context.log.info(
            f"New intent for cart payment {cart_payment.id}, due to higher amount {amount} (from {cart_payment.amount})"
        )

        # Immutable properties, such as currency, are derived from the previous/most recent intent in order to
        # have these fields for new intent submission and keep API simple for clients.
        most_recent_intent = self.get_most_recent_intent(existing_payment_intents)

        # Get payment resource IDs, required for submitting intent to providers
        pgp_intent = await self._get_most_recent_pgp_payment_intent(most_recent_intent)
        self.req_context.log.debug(f"Gathering fields from last intent {pgp_intent.id}")

        # New intent pair for the higher amount
        async with self.payment_repo.payment_database_transaction():
            payment_intent, pgp_payment_intent = await self._create_new_intent_pair(
                cart_payment=cart_payment,
                idempotency_key=idempotency_key,
                payment_method_id=most_recent_intent.payment_method_id,
                provider_payment_resource_id=pgp_intent.payment_method_resource_id,
                provider_customer_resource_id=pgp_intent.customer_resource_id,
                provider_metadata=most_recent_intent.metadata,
                amount=amount,
                country=most_recent_intent.country,
                currency=most_recent_intent.currency,
                capture_method=most_recent_intent.capture_method,
                payer_statement_description=most_recent_intent.statement_descriptor,
                capture_after=most_recent_intent.capture_after,
            )

            # Insert adjustment history record
            await self.payment_repo.insert_payment_intent_adjustment_history(
                id=uuid.uuid4(),
                payer_id=cart_payment.payer_id,
                payment_intent_id=payment_intent.id,
                amount=amount,
                amount_original=cart_payment.amount,
                amount_delta=(amount - cart_payment.amount),
                currency=payment_intent.currency,
            )

        self.req_context.log.debug(
            f"Created intent pair {payment_intent.id}, {pgp_payment_intent.id}"
        )

        return payment_intent, pgp_payment_intent

    async def lower_amount_for_uncaptured_payment(
        self,
        cart_payment: CartPayment,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        amount: int,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        # There is no need to call provider at this point in time.  The original auth done upon cart payment
        # creation is sufficient to cover a lower amount, so there is no need to update the amount with the provider.
        # Instead we will record updated amounts in our system, which will be reflected at time of (delayed) capture.

        async with self.payment_repo.payment_database_transaction():
            updated_intent = await self.payment_repo.update_payment_intent_amount(
                id=payment_intent.id, amount=amount
            )
            updated_pgp_intent = await self.payment_repo.update_pgp_payment_intent_amount(
                id=pgp_payment_intent.id, amount=amount
            )
            if self.ENABLE_NEW_CHARGE_TABLES:
                await self._update_charge_pair_after_amount_reduction(
                    payment_intent=payment_intent, amount=amount
                )
            await self.payment_repo.insert_payment_intent_adjustment_history(
                id=uuid.uuid4(),
                payer_id=cart_payment.payer_id,
                payment_intent_id=payment_intent.id,
                amount=amount,
                amount_original=cart_payment.amount,
                amount_delta=(amount - cart_payment.amount),
                currency=payment_intent.currency,
            )

        return updated_intent, updated_pgp_intent

    async def get_required_payment_resource_ids(
        self,
        payer_id: Optional[uuid.UUID],
        payment_method_id: Optional[uuid.UUID],
        legacy_country_id: int,
        legacy_payment: Optional[LegacyPayment],
    ) -> Tuple[PaymentResourceIds, LegacyPayment]:
        # We need to look up the pgp's account ID and payment method ID, so that we can use then for intent
        # submission and management.  A client is expected to either provide either (a) both payer_id and
        # payment_method_id, which we can use to look up corresponding pgp resource IDs, or (b) stripe resource
        # IDs directly via the legacy_payment request field.  Case (b) is for legacy clients who have not fully
        # adopted payin service yet, and in this case we direclty use those IDs and no lookup is needed.  A
        # LegacyPayment instance is also returned since it is required for persisting charge records in the old system.
        self.req_context.log.debug("Getting payment info.")

        if payer_id and payment_method_id:
            raw_payment_method = await self.payment_method_client.get_raw_payment_method(
                payer_id=payer_id,
                payer_id_type=PayerIdType.PAYER_ID,
                payment_method_id=payment_method_id,
                payment_method_id_type=PaymentMethodIdType.PAYMENT_METHOD_ID,
            )
            raw_payer = await self.payer_client.get_raw_payer(
                payer_id=payer_id, payer_id_type=PayerIdType.PAYER_ID
            )
            payer_resource_id = raw_payer.pgp_customer_id()
            payment_method_resource_id = raw_payment_method.pgp_payment_method_id()

            if not raw_payer.payer_entity:
                self.req_context.log.error("No payer entity found.")
                raise CartPaymentCreateError(
                    error_code=PayinErrorCode.CART_PAYMENT_CREATE_INVALID_DATA,
                    retryable=False,
                )

            result_legacy_payment = LegacyPayment(
                dd_consumer_id=raw_payer.payer_entity.dd_payer_id,
                dd_stripe_card_id=raw_payment_method.legacy_dd_stripe_card_id(),
                dd_country_id=legacy_country_id,
            )
            self.req_context.log.debug(
                f"Legacy payment generated for resource lookup: {result_legacy_payment}"
            )
        elif legacy_payment:
            # Legacy payment case: no payer_id/payment_method_id provided
            payer_resource_id = legacy_payment.stripe_customer_id
            payment_method_resource_id = ""
            if legacy_payment.stripe_payment_method_id:
                payment_method_resource_id = legacy_payment.stripe_payment_method_id
            elif legacy_payment.stripe_card_id:
                payment_method_resource_id = legacy_payment.stripe_card_id

            result_legacy_payment = legacy_payment
            self.req_context.log.debug(
                f"Legacy resource IDs: {payer_resource_id}, {payment_method_resource_id}"
            )

        # Ensure we have the necessary fields.  Though payer_client/payment_method_client already throws exceptions
        # if not found, still check here since we have to support the legacy payment case.
        if not payer_resource_id:
            self.req_context.log.warn("No payer pgp resource ID found.")
            raise CartPaymentCreateError(
                error_code=PayinErrorCode.CART_PAYMENT_CREATE_INVALID_DATA,
                retryable=False,
            )

        if not payment_method_resource_id:
            self.req_context.log.warn("No payment method pgp resource ID found.")
            raise CartPaymentCreateError(
                error_code=PayinErrorCode.CART_PAYMENT_CREATE_INVALID_DATA,
                retryable=False,
            )

        payment_resource_ids = PaymentResourceIds(
            provider_payment_resource_id=payment_method_resource_id,
            provider_customer_resource_id=payer_resource_id,
        )
        return payment_resource_ids, result_legacy_payment

    def populate_cart_payment_for_response(
        self, cart_payment: CartPayment, payment_intent: PaymentIntent
    ) -> CartPayment:
        """
        Populate fields within a CartPayment instance to be suitable for an API response body.
        Since CartPayment is a view on top of several models, it is necessary to synthesize info
        into a CartPayment instance from associated models.

        Arguments:
            cart_payment {CartPayment} -- The CartPayment instance to update.
            payment_intent {PaymentIntent} -- An associated PaymentIntent.
            pgp_payment_intent {PgpPaymentIntent} -- An associated PgpPaymentIntent.
        """
        cart_payment.payer_statement_description = payment_intent.statement_descriptor
        cart_payment.payment_method_id = payment_intent.payment_method_id
        return cart_payment

    async def update_cart_payment_attributes(
        self,
        cart_payment: CartPayment,
        idempotency_key: str,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        amount: int,
        client_description: Optional[str],
    ) -> CartPayment:
        updated_cart_payment = await self.payment_repo.update_cart_payment_details(
            cart_payment_id=cart_payment.id,
            amount=amount,
            client_description=client_description,
        )
        self.populate_cart_payment_for_response(updated_cart_payment, payment_intent)
        return updated_cart_payment


class CartPaymentProcessor:
    # TODO: use payer_country passed to processor functions to get the right stripe platform key within CartPaymentInterface.

    def __init__(
        self,
        log: BoundLogger = Depends(get_logger_from_req),
        cart_payment_interface: CartPaymentInterface = Depends(CartPaymentInterface),
        legacy_payment_interface: LegacyPaymentInterface = Depends(
            LegacyPaymentInterface
        ),
    ):
        self.log = log
        self.cart_payment_interface = cart_payment_interface
        self.legacy_payment_interface = legacy_payment_interface

    async def _update_state_after_cancel_with_provider(
        self, payment_intent: PaymentIntent, pgp_payment_intent: PgpPaymentIntent
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        # provider_refund: ProviderRefund, refund_amount: int
        payment_intent, pgp_payment_intent = await self.cart_payment_interface.update_payment_after_cancel_with_provider(
            payment_intent=payment_intent, pgp_payment_intent=pgp_payment_intent
        )

        # TODO: Determine if update to legacy charge pair is needed

        return payment_intent, pgp_payment_intent

    async def _update_state_after_submit_to_provider(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_payment_intent: ProviderPaymentIntent,
        correlation_ids: CorrelationIds,
        legacy_payment: LegacyPayment,
    ) -> Tuple[
        PaymentIntent,
        PgpPaymentIntent,
        Optional[LegacyConsumerCharge],
        LegacyStripeCharge,
    ]:
        # Update state of payment in our system now that payment exists in provider
        payment_intent, pgp_payment_intent = await self.cart_payment_interface.update_payment_after_submission_to_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_intent=provider_payment_intent,
        )
        # Also update state in our legacy system: ConsumerCharge/StripeCharge still used there until migration to new service
        # is entirely complete
        legacy_consumer_charge, legacy_stripe_charge = await self.legacy_payment_interface.create_charge_after_payment_submitted(
            correlation_ids=correlation_ids,
            legacy_payment=legacy_payment,
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_intent=provider_payment_intent,
        )

        # When submitting a new intent, capture may happen if:
        # (a) Caller specified capture_method = auto.  This happens above when updating state, and intents are already transitioned to success/failed states.
        # (b) Caller specified capture_method = manual (delay capture) but based on config we are not going to wait - handled below.
        if self.cart_payment_interface.is_capture_immediate(
            payment_intent
        ) and self.cart_payment_interface.does_intent_require_capture(payment_intent):
            await self.capture_payment(payment_intent)

        return (
            payment_intent,
            pgp_payment_intent,
            legacy_consumer_charge,
            legacy_stripe_charge,
        )

    async def _update_state_after_refund_with_provider(
        self,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
        provider_refund: ProviderRefund,
        refund_amount: int,
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        payment_intent, pgp_payment_intent = await self.cart_payment_interface.update_payment_after_refund_with_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
                payment_intent
            ),
            provider_refund=provider_refund,
            refund_amount=refund_amount,
        )

        await self.legacy_payment_interface.update_charge_after_payment_refunded(
            provider_refund=provider_refund
        )
        return payment_intent, pgp_payment_intent

    async def _cancel_payment_intent(
        self, cart_payment: CartPayment, payment_intent: PaymentIntent
    ) -> Tuple[PaymentIntent, PgpPaymentIntent]:
        can_intent_be_cancelled = self.cart_payment_interface.can_payment_intent_be_cancelled(
            payment_intent
        )
        can_intent_be_refunded = self.cart_payment_interface.can_payment_intent_be_refunded(
            payment_intent
        )
        pgp_payment_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
            payment_intent
        )

        if not can_intent_be_cancelled and not can_intent_be_refunded:
            # If not able to cancel or refund, no action is needed (for example, intent is in failed state).
            return payment_intent, pgp_payment_intent

        if can_intent_be_cancelled:
            # Intent not yet captured: it can be cancelled.
            # Cancel with provider
            await self.cart_payment_interface.cancel_provider_payment_charge(
                payment_intent,
                pgp_payment_intent,
                CancelPaymentIntent.CancellationReason.ABANDONED,
            )

            # Update state in our system after operation with provider
            updated_payment_intent, updated_pgp_payment_intent = await self._update_state_after_cancel_with_provider(
                payment_intent=payment_intent, pgp_payment_intent=pgp_payment_intent
            )
        elif can_intent_be_refunded:
            # The intent cannot be cancelled because its state is beyond capture.  Instead we must refund
            provider_refund = await self.cart_payment_interface.refund_provider_payment(
                payment_intent=payment_intent,
                pgp_payment_intent=pgp_payment_intent,
                reason=RefundCharge.RefundReason.REQUESTED_BY_CONSUMER,
                refund_amount=payment_intent.amount,
            )

            # Update state
            updated_payment_intent, updated_pgp_payment_intent = await self._update_state_after_refund_with_provider(
                payment_intent=payment_intent,
                pgp_payment_intent=pgp_payment_intent,
                provider_refund=provider_refund,
                refund_amount=payment_intent.amount,
            )

        return updated_payment_intent, updated_pgp_payment_intent

    async def _update_payment_with_higher_amount(
        self,
        cart_payment: CartPayment,
        legacy_payment: LegacyPayment,
        idempotency_key: str,
        amount: int,
    ):
        payment_intents = await self.cart_payment_interface.get_cart_payment_intents(
            cart_payment
        )
        existing_payment_intent = self.cart_payment_interface.filter_payment_intents_by_idempotency_key(
            payment_intents, idempotency_key
        )

        payment_resource_ids, legacy_payment = await self.cart_payment_interface.get_required_payment_resource_ids(
            payer_id=cart_payment.payer_id,
            payment_method_id=payment_intents[0].payment_method_id,
            legacy_payment=legacy_payment,
            legacy_country_id=self.legacy_payment_interface.get_country_id_by_code(
                payment_intents[0].country
            ),
        )

        if existing_payment_intent:
            # Client cart payment adjustment attempt received before.  If adjustment was entirely handled before, we can immediate return.
            if not self.cart_payment_interface.is_payment_intent_submitted(
                existing_payment_intent
            ):
                self.log.info(
                    f"[_update_payment_with_higher_amount] Duplicate amount increase request for idempotency_key {idempotency_key}"
                )
                self.cart_payment_interface.populate_cart_payment_for_response(
                    cart_payment, existing_payment_intent
                )
                return cart_payment

            # We have record of the payment but it did not make it to provider.  Pick up where we left off by trying to submit to
            # provider and update state accordingly.
            payment_intent = existing_payment_intent
            pgp_payment_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
                payment_intent
            )
            self.log.info(
                f"[_update_payment_with_higher_amount] Process existing intents for amount increase request"
            )
        else:
            # First attempt at cart payment adjustment for this idempotency key.
            payment_intent, pgp_payment_intent = await self.cart_payment_interface.increase_payment_amount(
                cart_payment=cart_payment,
                existing_payment_intents=payment_intents,
                amount=amount,
                idempotency_key=idempotency_key,
            )

        # Call to provider to create payment on their side, and update state in our system based on the result
        # TODO catch error and update state accordingly: both payment_intent/pgp_payment_intent, and legacy charges
        provider_payment_intent = await self.cart_payment_interface.submit_payment_to_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_resource_id=payment_resource_ids.provider_payment_resource_id,
            provider_customer_resource_id=payment_resource_ids.provider_customer_resource_id,
        )

        # Find the most recent intent that was submitted and get the stripe charge id from it.  It is used to update
        # state in the legacy system (to find the charge record).
        non_failed_intents = self.cart_payment_interface.get_submitted_or_captured_intents(
            payment_intents
        )
        if non_failed_intents:
            last_non_failed_intent = self.cart_payment_interface.get_most_recent_intent(
                non_failed_intents
            )
            last_non_failed_pgp_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
                last_non_failed_intent
            )
            legacy_payment.stripe_charge_id = (
                last_non_failed_pgp_intent.charge_resource_id
            )

        # Update state of payment in our system now that payment exists in provider.
        # Also takes care of triggering immediate capture if needed.
        payment_intent, pgp_payment_intent, legacy_consumer_charge, legacy_stripe_charge = await self._update_state_after_submit_to_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_intent=provider_payment_intent,
            correlation_ids=cart_payment.correlation_ids,
            legacy_payment=legacy_payment,
        )

        # Cancel old intents
        intent_operations = []
        for intent in payment_intents:
            intent_operations.append(
                self._cancel_payment_intent(
                    cart_payment=cart_payment, payment_intent=intent
                )
            )
        if len(intent_operations) > 0:
            await gather(*intent_operations)

        return payment_intent, pgp_payment_intent

    async def _update_payment_with_lower_amount(
        self, cart_payment: CartPayment, new_amount: int
    ):
        payment_intents = await self.cart_payment_interface.get_cart_payment_intents(
            cart_payment
        )
        # TODO handle idempotency key
        capturable_intents = self.cart_payment_interface.get_capturable_payment_intents(
            payment_intents
        )
        refundable_intents = self.cart_payment_interface.get_refundable_payment_intents(
            payment_intents
        )
        if not capturable_intents and not refundable_intents:
            raise PaymentIntentRefundError(
                error_code=PayinErrorCode.PAYMENT_INTENT_ADJUST_REFUND_ERROR,
                retryable=False,
            )

        if capturable_intents:
            # If there are any uncaptured intents suitable for this amount change, update
            capturable_intent = self.cart_payment_interface.get_most_recent_intent(
                capturable_intents
            )
            capturable_pgp_payment_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
                capturable_intent
            )
            payment_intent, pgp_payment_intent = await self.cart_payment_interface.lower_amount_for_uncaptured_payment(
                cart_payment=cart_payment,
                payment_intent=capturable_intent,
                pgp_payment_intent=capturable_pgp_payment_intent,
                amount=new_amount,
            )
        elif refundable_intents:
            refundable_intent = self.cart_payment_interface.get_most_recent_intent(
                capturable_intents
            )
            refund_amount = refundable_intent.amount - new_amount
            # TODO verify if pgp intent can be refunded: may have been refunded previously, protect against
            # exceeding limits (avoid call to provider if not necessary)

            provider_refund = await self.cart_payment_interface.refund_provider_payment(
                payment_intent=payment_intent,
                pgp_payment_intent=pgp_payment_intent,
                reason=RefundCharge.RefundReason.REQUESTED_BY_CONSUMER,
                refund_amount=refund_amount,
            )

            # Update state
            payment_intent, pgp_payment_intent = await self._update_state_after_refund_with_provider(
                payment_intent=payment_intent,
                pgp_payment_intent=pgp_payment_intent,
                provider_refund=provider_refund,
                refund_amount=refund_amount,
            )

        return payment_intent, pgp_payment_intent

    async def update_payment_for_legacy_charge(
        self,
        idempotency_key: str,
        dd_charge_id: int,
        payer_id: Optional[str],
        amount: int,
        client_description: Optional[str],
    ) -> CartPayment:
        """Update an existing payment associated with a legacy consumer charge.

        Arguments:
            payment_method_repo {PaymentMethodRepository} -- Repo for accessing PaymentMethod and associated models.
            idempotency_key {str} -- Client specified value for ensuring idempotency.
            dd_charge_id {int} -- ID of the legacy consumer charge associated with the cart payment to adjust.
            payer_id {str} -- ID of the payer who owns the specified cart payment.
            amount {int} -- New amount to use for cart payment.
            client_description {Optional[str]} -- New client description to use for cart payment.

        Raises:
            CartPaymentReadError: [description]
            CartPaymentReadError: [description]
            PaymentIntentNotInRequiresCaptureState: [description]
            PaymentIntentConcurrentAccessError: [description]

        Returns:
            CartPayment -- [description]
        """
        cart_payment_id = await self.legacy_payment_interface.get_associated_cart_payment_id(
            dd_charge_id
        )
        if not cart_payment_id:
            self.log.error(
                f"Did not find cart payment for consumer charge {dd_charge_id}"
            )
            raise CartPaymentReadError(
                error_code=PayinErrorCode.CART_PAYMENT_NOT_FOUND, retryable=False
            )

        return await self.update_payment(
            idempotency_key=idempotency_key,
            cart_payment_id=cart_payment_id,
            payer_id=None,  # Not currently used in udpate_payment
            amount=amount,
            client_description=client_description,
        )

    async def update_payment(
        self,
        idempotency_key: str,
        cart_payment_id: uuid.UUID,
        payer_id: Optional[uuid.UUID],
        amount: int,
        client_description: Optional[str],
    ) -> CartPayment:
        """Update an existing payment.

        Arguments:
            payment_method_repo {PaymentMethodRepository} -- Repo for accessing PaymentMethod and associated models.
            idempotency_key {str} -- Client specified value for ensuring idempotency.
            cart_payment_id {uuid.UUID} -- ID of the cart payment to adjust.
            payer_id {str} -- ID of the payer who owns the specified cart payment.
            amount {int} -- New amount to use for cart payment.
            client_description {Optional[str]} -- New client description to use for cart payment.

        Raises:
            CartPaymentReadError: Raised when there is an error retrieving the specified cart payment.

        Returns:
            CartPayment -- An updated CartPayment instance, reflecting changes requested by the client.
        """

        cart_payment, legacy_payment = await self.cart_payment_interface.get_cart_payment(
            cart_payment_id
        )
        if not cart_payment or not legacy_payment:
            raise CartPaymentReadError(
                error_code=PayinErrorCode.CART_PAYMENT_NOT_FOUND, retryable=False
            )

        # Ensure the caller can access the cart payment being modified
        if not self.cart_payment_interface.is_accessible(
            cart_payment=cart_payment, request_payer_id=payer_id, credential_owner=""
        ):
            raise CartPaymentReadError(
                error_code=PayinErrorCode.CART_PAYMENT_OWNER_MISMATCH, retryable=False
            )

        # Update the cart payment
        # TODO concurrency control for attempts to update the same cart payment
        if self.cart_payment_interface.is_amount_adjusted_higher(cart_payment, amount):
            payment_intent, pgp_payment_intent = await self._update_payment_with_higher_amount(
                cart_payment, legacy_payment, idempotency_key, amount
            )
        elif self.cart_payment_interface.is_amount_adjusted_lower(cart_payment, amount):
            payment_intent, pgp_payment_intent = await self._update_payment_with_lower_amount(
                cart_payment, amount
            )
        else:
            # Amount is the same: properties of cart payment other than the amount may be changing
            payment_intents = await self.cart_payment_interface.get_cart_payment_intents(
                cart_payment
            )
            payment_intent = self.cart_payment_interface.get_most_recent_intent(
                payment_intents
            )
            pgp_payment_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
                payment_intent
            )

        updated_cart_payment = await self.cart_payment_interface.update_cart_payment_attributes(
            cart_payment=cart_payment,
            idempotency_key=idempotency_key,
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            amount=amount,
            client_description=client_description,
        )
        return self.cart_payment_interface.populate_cart_payment_for_response(
            updated_cart_payment, payment_intent
        )

    async def capture_payment(self, payment_intent: PaymentIntent) -> None:
        """Capture a payment intent.

        Arguments:
            payment_intent {PaymentIntent} -- The PaymentIntent to capture.

        Raises:
            e: Raises an exception if database operations fail.

        Returns:
            None
        """
        self.log.info(f"Capture attempt for payment_intent {payment_intent.id}")

        if not self.cart_payment_interface.does_intent_require_capture(payment_intent):
            self.log.info(
                f"Payment intent not eligible for capturing, in state {payment_intent.status}"
            )
            raise PaymentIntentNotInRequiresCaptureState()

        # Update intent status; acts as optimistic lock
        try:
            payment_intent = await self.cart_payment_interface.acquire_for_capture(
                payment_intent
            )
        except PaymentIntentCouldNotBeUpdatedError:
            raise PaymentIntentConcurrentAccessError()

        # Find the PgpPaymentIntent to capture
        pgp_payment_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
            payment_intent
        )

        # Call to provider to capture, with idempotency key
        provider_payment_intent = await self.cart_payment_interface.submit_capture_to_provider(
            payment_intent, pgp_payment_intent
        )

        # Update state in our system
        updated_payment_intent, updated_pgp_payment_intent = await self.cart_payment_interface.update_payment_after_capture_with_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_intent=provider_payment_intent,
        )
        await self.legacy_payment_interface.update_charge_after_payment_captured(
            provider_payment_intent
        )

    async def create_payment(
        self,
        request_cart_payment: CartPayment,
        request_legacy_payment: Optional[LegacyPayment],
        request_legacy_stripe_metadata: Optional[Dict[str, Any]],
        request_legacy_correlation_ids: Optional[LegacyCorrelationIds],
        idempotency_key: str,
        country: CountryCode,
        currency: CurrencyType,
        client_description: Optional[str],
    ) -> Tuple[CartPayment, LegacyPayment]:
        """Submit a cart payment creation request.

        Arguments:
            request_cart_payment {CartPayment} -- CartPayment model containing request paramters provided by client.
            request_legacy_payment {LegacyPayment} -- LegacyPayment model containing legacy fields.  For v0 use only.
            idempotency_key {str} -- Client specified value for ensuring idempotency.
            country {CurrencyType} -- ISO country code.
            currency {CurrencyType} -- Currency for cart payment request.
            client_description {str} -- Pass through value clients may associated with the cart payment.

        Returns:
            CartPayment -- A CartPayment model for the created payment.
        """
        # Overload the correlation IDs for the legacy case, since they end up persisted in the same fields of the cart payment.
        if (
            not request_cart_payment.correlation_ids.reference_id
            and request_legacy_correlation_ids
        ):
            request_cart_payment.correlation_ids = CorrelationIds(
                reference_id=str(request_legacy_correlation_ids.reference_id),
                reference_type=str(request_legacy_correlation_ids.reference_type),
            )

        # TODO Check country, currency are supported values
        # amount is positive
        # reference_id/reference_type are supported values as defined by agreements with other teams

        # If payment method is not found or not owned by the specified payer, an exception is raised and handled by
        # our exception handling middleware.
        payment_resource_ids, legacy_payment = await self.cart_payment_interface.get_required_payment_resource_ids(
            payer_id=request_cart_payment.payer_id,
            payment_method_id=request_cart_payment.payment_method_id,
            legacy_payment=request_legacy_payment,
            legacy_country_id=self.legacy_payment_interface.get_country_id_by_code(
                country
            ),
        )

        # Check for resubmission by client
        existing_cart_payment, existing_legacy_payment, existing_payment_intent = await self.cart_payment_interface.find_existing_payment(
            request_cart_payment.payer_id, idempotency_key
        )
        if (
            existing_cart_payment
            and existing_legacy_payment
            and existing_payment_intent
        ):
            # Client is attempting to create payment that we already have record of
            # If payment was entirely submitted before, we can immediately return.
            if not self.cart_payment_interface.is_payment_intent_submitted(
                existing_payment_intent
            ):
                self.log.info(
                    f"[create_payment] Duplicate cart payment creation request for key {existing_payment_intent.idempotency_key}"
                )
                return (
                    self.cart_payment_interface.populate_cart_payment_for_response(
                        existing_cart_payment, existing_payment_intent
                    ),
                    existing_legacy_payment,
                )

            # We have record of the payment but it did not make it to provider.  Pick up where we left off by trying to submit to
            # provider and update state accordingly.
            cart_payment = existing_cart_payment
            payment_intent = existing_payment_intent
            pgp_payment_intent = await self.cart_payment_interface.get_cart_payment_submission_pgp_intent(
                payment_intent
            )
            self.log.info(
                f"[create_payment] Processing existing intents for cart payment creation request for key {existing_payment_intent.idempotency_key}"
            )
        else:
            # New payment: Create records in our system for the new cart payment
            cart_payment, payment_intent, pgp_payment_intent = await self.cart_payment_interface.create_new_payment(
                request_cart_payment=request_cart_payment,
                legacy_payment=legacy_payment,
                provider_payment_resource_id=payment_resource_ids.provider_payment_resource_id,
                provider_customer_resource_id=payment_resource_ids.provider_customer_resource_id,
                provider_metadata=request_legacy_stripe_metadata,
                idempotency_key=idempotency_key,
                country=country,
                currency=currency,
                client_description=client_description,
            )

        # Call to provider to create payment on their side, and update state in our system based on the result
        # TODO catch error and update state accordingly: both payment_intent/pgp_payment_intent, and legacy charges
        provider_payment_intent = await self.cart_payment_interface.submit_payment_to_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_resource_id=payment_resource_ids.provider_payment_resource_id,
            provider_customer_resource_id=payment_resource_ids.provider_customer_resource_id,
        )

        # Update state of payment in our system now that payment exists in provider.
        # Also takes care of triggering immediate capture if needed.
        payment_intent, pgp_payment_intent, legacy_consumer_charge, legacy_stripe_charge = await self._update_state_after_submit_to_provider(
            payment_intent=payment_intent,
            pgp_payment_intent=pgp_payment_intent,
            provider_payment_intent=provider_payment_intent,
            correlation_ids=request_cart_payment.correlation_ids,
            legacy_payment=legacy_payment,
        )
        if legacy_consumer_charge:
            legacy_payment.dd_charge_id = legacy_consumer_charge.id

        self.cart_payment_interface.populate_cart_payment_for_response(
            cart_payment, payment_intent
        )
        return cart_payment, legacy_payment
