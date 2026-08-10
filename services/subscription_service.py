"""Servicio de suscripciones SaaS multiempresa."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from time import perf_counter
from typing import Any


class SubscriptionCommandError(RuntimeError):
    pass


@dataclass
class SubscriptionCommandBase:
    company_id: int
    actor_user_id: int | None = None
    actor_role: str | None = None
    origin: str = "system"
    ip_address: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CreateSubscriptionCommand(SubscriptionCommandBase):
    plan_id: int | None = None
    status: str = "pending"
    start_date: Any = None
    next_billing_date: Any = None
    renewal_enabled: bool = True


@dataclass
class ChangePlanCommand(SubscriptionCommandBase):
    plan_id: int | None = None


@dataclass
class CancelSubscriptionCommand(SubscriptionCommandBase):
    subscription_id: int | None = None
    cancel_at_period_end: bool = True


@dataclass
class ReactivateSubscriptionCommand(SubscriptionCommandBase):
    subscription_id: int | None = None


@dataclass
class RenewSubscriptionCommand(SubscriptionCommandBase):
    subscription_id: int | None = None
    payment_status: str | None = None
    paid_at: Any = None


@dataclass
class ExtendSubscriptionCommand(SubscriptionCommandBase):
    subscription_id: int | None = None
    days: int = 7


@dataclass
class ExpireSubscriptionCommand(SubscriptionCommandBase):
    subscription_id: int | None = None
    reason: str = "expired"


@dataclass
class ChangePaymentMethodCommand(SubscriptionCommandBase):
    subscription_id: int | None = None
    payment_method: str | None = None
    external_reference: str | None = None


@dataclass
class AssignManualSubscriptionCommand(SubscriptionCommandBase):
    plan_id: int | None = None
    manual_reason: str = ""
    created_by_admin: int | None = None


@dataclass
class RemoveManualSubscriptionCommand(SubscriptionCommandBase):
    subscription_id: int | None = None


@dataclass
class CommandResult:
    command_name: str
    subscription_id: int | None
    company_id: int
    status_before: str | None
    status_after: str | None
    plan_before_id: int | None
    plan_after_id: int | None
    details: dict[str, Any] = field(default_factory=dict)


logger = logging.getLogger(__name__)


class SubscriptionService:
    # Máquina de estados del módulo.
    STATE_DRAFT = "draft"
    STATE_PENDING = "pending"
    STATE_PENDING_PAYMENT = "pending_payment"
    STATE_PENDING_CONFIRMATION = "pending_confirmation"
    STATE_ACTIVE = "active"
    STATE_SCHEDULED = "scheduled"
    STATE_EXPIRED = "expired"
    STATE_CANCELLED = "cancelled"
    STATE_SUSPENDED = "suspended"
    STATE_TRIAL = "trial"
    STATE_TRIAL_EXPIRED = "trial_expired"
    OVERDUE_GRACE_DAYS = 5

    VALID_STATES = {
        STATE_DRAFT,
        STATE_PENDING,
        STATE_PENDING_PAYMENT,
        STATE_PENDING_CONFIRMATION,
        STATE_TRIAL,
        STATE_TRIAL_EXPIRED,
        STATE_ACTIVE,
        STATE_SCHEDULED,
        STATE_EXPIRED,
        STATE_CANCELLED,
        STATE_SUSPENDED,
    }

    ALLOWED_TRANSITIONS = {
        STATE_DRAFT: {STATE_PENDING, STATE_CANCELLED},
        STATE_PENDING: {STATE_PENDING_PAYMENT, STATE_PENDING_CONFIRMATION, STATE_ACTIVE, STATE_CANCELLED, STATE_EXPIRED},
        STATE_PENDING_PAYMENT: {STATE_PENDING_CONFIRMATION, STATE_ACTIVE, STATE_CANCELLED, STATE_EXPIRED},
        STATE_PENDING_CONFIRMATION: {STATE_PENDING_PAYMENT, STATE_ACTIVE, STATE_CANCELLED, STATE_EXPIRED},
        STATE_TRIAL: {STATE_ACTIVE, STATE_TRIAL_EXPIRED, STATE_CANCELLED},
        STATE_TRIAL_EXPIRED: {STATE_EXPIRED, STATE_ACTIVE, STATE_CANCELLED},
        STATE_ACTIVE: {STATE_SCHEDULED, STATE_CANCELLED, STATE_EXPIRED, STATE_SUSPENDED},
        STATE_SCHEDULED: {STATE_ACTIVE, STATE_CANCELLED, STATE_EXPIRED},
        STATE_SUSPENDED: {STATE_ACTIVE, STATE_CANCELLED, STATE_EXPIRED},
        STATE_EXPIRED: {STATE_ACTIVE, STATE_CANCELLED},
        STATE_CANCELLED: {STATE_ACTIVE},
    }

    TRIAL_STATUSES = {STATE_TRIAL, "trialing"}
    PENDING_STATUSES = {STATE_PENDING, STATE_PENDING_PAYMENT, STATE_PENDING_CONFIRMATION, "in_process", "authorized"}
    ACTIVE_STATUSES = {STATE_ACTIVE, STATE_SCHEDULED, "activa", "approved"}
    BLOCKED_STATUSES = {STATE_SUSPENDED, STATE_EXPIRED, STATE_CANCELLED, "canceled", "rejected", "charged_back", STATE_TRIAL_EXPIRED}
    OPEN_STATUSES = {STATE_DRAFT, STATE_PENDING, STATE_PENDING_PAYMENT, STATE_PENDING_CONFIRMATION, STATE_ACTIVE, STATE_SCHEDULED, STATE_SUSPENDED, STATE_TRIAL, STATE_TRIAL_EXPIRED}
    TERMINAL_STATUSES = {STATE_EXPIRED, STATE_CANCELLED}

    PAYMENT_STATUS_MAP = {
        "approved": STATE_ACTIVE,
        "authorized": STATE_PENDING_CONFIRMATION,
        "pending": STATE_PENDING,
        "in_process": STATE_PENDING_PAYMENT,
        "cancelled": STATE_CANCELLED,
        "expired": STATE_EXPIRED,
        "rejected": STATE_EXPIRED,
        "refunded": STATE_ACTIVE,
        "charged_back": STATE_SUSPENDED,
    }

    CreateSubscriptionCommand = CreateSubscriptionCommand
    ChangePlanCommand = ChangePlanCommand
    CancelSubscriptionCommand = CancelSubscriptionCommand
    ReactivateSubscriptionCommand = ReactivateSubscriptionCommand
    RenewSubscriptionCommand = RenewSubscriptionCommand
    ExtendSubscriptionCommand = ExtendSubscriptionCommand
    ExpireSubscriptionCommand = ExpireSubscriptionCommand
    ChangePaymentMethodCommand = ChangePaymentMethodCommand
    AssignManualSubscriptionCommand = AssignManualSubscriptionCommand
    RemoveManualSubscriptionCommand = RemoveManualSubscriptionCommand

    @staticmethod
    def _command_name(command) -> str:
        return command.__class__.__name__

    @staticmethod
    def _command_key(command) -> str:
        if getattr(command, "idempotency_key", None):
            return str(command.idempotency_key)
        payload = json.dumps(asdict(command), sort_keys=True, default=str, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        return f"{command.__class__.__name__}:{digest}"

    @staticmethod
    def _normalize_state(value: str | None, *, default: str = STATE_PENDING) -> str:
        normalized = (value or default).strip().lower()
        return normalized if normalized in SubscriptionService.VALID_STATES else default

    @staticmethod
    def _transition(subscription, to_state: str, *, reason: str):
        current_state = SubscriptionService._normalize_state(getattr(subscription, "status", None), default=SubscriptionService.STATE_DRAFT)
        target_state = SubscriptionService._normalize_state(to_state, default=SubscriptionService.STATE_PENDING)
        if current_state == target_state:
            return
        allowed = SubscriptionService.ALLOWED_TRANSITIONS.get(current_state, set())
        if target_state not in allowed:
            raise SubscriptionCommandError(
                f"Transición inválida de estado: {current_state} -> {target_state} ({reason})"
            )
        subscription.status = target_state

    @staticmethod
    def _enforce_date_invariants(subscription):
        starts_at = getattr(subscription, "starts_at", None)
        ends_at = getattr(subscription, "ends_at", None)
        start_date = getattr(subscription, "start_date", None)
        next_billing = getattr(subscription, "next_billing_date", None)
        if starts_at and ends_at and ends_at < starts_at:
            raise SubscriptionCommandError("Fechas inválidas: ends_at no puede ser menor que starts_at.")
        if start_date and next_billing and next_billing < start_date:
            raise SubscriptionCommandError("Fechas inválidas: next_billing_date no puede ser menor que start_date.")

    @staticmethod
    def _enforce_company_invariants(db_session, *, company_id: int):
        from app import Subscription

        active_count = (
            Subscription.query.filter_by(company_id=company_id)
            .filter(Subscription.status == SubscriptionService.STATE_ACTIVE)
            .count()
        )
        if active_count > 1:
            raise SubscriptionCommandError("Invariante violada: más de una suscripción ACTIVE para la empresa.")

    @staticmethod
    def _lock_company_subscriptions(company_id: int):
        from app import Subscription

        try:
            return (
                Subscription.query.filter_by(company_id=company_id)
                .with_for_update()
                .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
                .all()
            )
        except Exception:
            return (
                Subscription.query.filter_by(company_id=company_id)
                .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
                .all()
            )

    @staticmethod
    def _metadata_dict(subscription):
        raw = (getattr(subscription, "metadata_json", None) or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _set_metadata(subscription, values):
        data = SubscriptionService._metadata_dict(subscription)
        data.update(values or {})
        subscription.metadata_json = json.dumps(data, ensure_ascii=False)
        return data

    @staticmethod
    def is_manual_subscription(subscription):
        data = SubscriptionService._metadata_dict(subscription)
        managed_by = (str(data.get("managed_by") or "").strip().lower())
        if managed_by in {"admin", "superadmin", "manual"}:
            return True
        return bool(data.get("is_manual"))

    @staticmethod
    def _is_open_status(status):
        return (status or "").strip().lower() in SubscriptionService.OPEN_STATUSES

    @staticmethod
    def _close_for_change(subscription, *, now, actor_user_id, origin):
        normalized_status = SubscriptionService._normalize_state(subscription.status, default=SubscriptionService.STATE_DRAFT)
        if normalized_status not in SubscriptionService.TERMINAL_STATUSES:
            SubscriptionService._transition(subscription, SubscriptionService.STATE_CANCELLED, reason="close_for_change")
        subscription.ends_at = now
        subscription.cancel_at_period_end = False
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        SubscriptionService._set_metadata(
            subscription,
            {
                "closed_reason": "plan_change",
                "closed_at": now.isoformat(),
                "closed_by_user_id": actor_user_id,
                "closed_origin": origin,
            },
        )

    @staticmethod
    def active_subscription_for_company(company_id: int):
        from app import Subscription

        rows = (
            Subscription.query.filter_by(company_id=company_id)
            .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
            .all()
        )
        for row in rows:
            if SubscriptionService._is_open_status(row.status):
                return row
        return rows[0] if rows else None

    @staticmethod
    def run_command(db_session, command: SubscriptionCommandBase) -> CommandResult:
        from app import Company, Subscription, SubscriptionCommandExecution

        command_name = SubscriptionService._command_name(command)
        command_key = SubscriptionService._command_key(command)
        t0 = perf_counter()
        logger.info(
            "Subscription command start: command=%s key=%s company_id=%s actor_user_id=%s origin=%s",
            command_name,
            command_key,
            command.company_id,
            getattr(command, "actor_user_id", None),
            getattr(command, "origin", "system"),
        )
        existing = SubscriptionCommandExecution.query.filter_by(command_key=command_key).first()
        if existing and (existing.command_status or "").lower() == "completed":
            payload = {}
            try:
                payload = json.loads(existing.result_json) if existing.result_json else {}
            except json.JSONDecodeError:
                payload = {}
            return CommandResult(
                command_name=command_name,
                subscription_id=existing.subscription_id,
                company_id=existing.company_id,
                status_before=payload.get("status_before"),
                status_after=payload.get("status_after"),
                plan_before_id=payload.get("plan_before_id"),
                plan_after_id=payload.get("plan_after_id"),
                details=payload.get("details") or {},
            )

        try:
            company = db_session.get(Company, command.company_id)
            if company is None:
                raise SubscriptionCommandError("Empresa no encontrada para ejecutar comando de suscripción.")

            SubscriptionService._lock_company_subscriptions(company.id)
            result = SubscriptionService._dispatch_command(db_session, company=company, command=command)
            subscription = db_session.get(Subscription, result.subscription_id) if result.subscription_id else None
            if subscription is not None:
                SubscriptionService._enforce_date_invariants(subscription)

            SubscriptionService._enforce_company_invariants(db_session, company_id=company.id)

            execution = SubscriptionCommandExecution(
                command_name=command_name,
                command_key=command_key,
                command_status="completed",
                company_id=company.id,
                subscription_id=result.subscription_id,
                actor_user_id=getattr(command, "actor_user_id", None),
                origin=getattr(command, "origin", "system") or "system",
                ip_address=getattr(command, "ip_address", None),
                payload_json=json.dumps(asdict(command), ensure_ascii=False, default=str),
                result_json=json.dumps(
                    {
                        "status_before": result.status_before,
                        "status_after": result.status_after,
                        "plan_before_id": result.plan_before_id,
                        "plan_after_id": result.plan_after_id,
                        "details": result.details,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            db_session.add(execution)
            SubscriptionService._record_history(db_session, company_id=company.id, command=command, result=result)
            elapsed_ms = int((perf_counter() - t0) * 1000)
            logger.info(
                "Subscription command success: command=%s key=%s company_id=%s subscription_id=%s status_before=%s status_after=%s elapsed_ms=%s",
                command_name,
                command_key,
                company.id,
                result.subscription_id,
                result.status_before,
                result.status_after,
                elapsed_ms,
            )
            return result
        except Exception:
            elapsed_ms = int((perf_counter() - t0) * 1000)
            logger.exception(
                "Subscription command failure: command=%s key=%s company_id=%s elapsed_ms=%s",
                command_name,
                command_key,
                command.company_id,
                elapsed_ms,
            )
            raise

    @staticmethod
    def _dispatch_command(db_session, *, company, command: SubscriptionCommandBase) -> CommandResult:
        if isinstance(command, CreateSubscriptionCommand):
            return SubscriptionService._handle_create_subscription(db_session, company=company, command=command)
        if isinstance(command, ChangePlanCommand):
            return SubscriptionService._handle_change_plan(db_session, company=company, command=command)
        if isinstance(command, CancelSubscriptionCommand):
            return SubscriptionService._handle_cancel(db_session, company=company, command=command)
        if isinstance(command, ReactivateSubscriptionCommand):
            return SubscriptionService._handle_reactivate(db_session, company=company, command=command)
        if isinstance(command, RenewSubscriptionCommand):
            return SubscriptionService._handle_renew(db_session, company=company, command=command)
        if isinstance(command, ExtendSubscriptionCommand):
            return SubscriptionService._handle_extend(db_session, company=company, command=command)
        if isinstance(command, ExpireSubscriptionCommand):
            return SubscriptionService._handle_expire(db_session, company=company, command=command)
        if isinstance(command, ChangePaymentMethodCommand):
            return SubscriptionService._handle_change_payment_method(db_session, company=company, command=command)
        if isinstance(command, AssignManualSubscriptionCommand):
            return SubscriptionService._handle_assign_manual(db_session, company=company, command=command)
        if isinstance(command, RemoveManualSubscriptionCommand):
            return SubscriptionService._handle_remove_manual(db_session, company=company, command=command)
        raise SubscriptionCommandError(f"Comando no soportado: {command.__class__.__name__}")

    @staticmethod
    def _record_history(db_session, *, company_id: int, command: SubscriptionCommandBase, result: CommandResult):
        from app import AuditLog, PaymentHistory

        detail = (
            f"command={result.command_name} from={result.status_before} to={result.status_after} "
            f"plan_from={result.plan_before_id} plan_to={result.plan_after_id}"
        )
        db_session.add(
            AuditLog(
                user_id=command.actor_user_id,
                company_id=company_id,
                action=f"subscription_command_{result.command_name}",
                entity="subscription",
                entity_id=result.subscription_id,
                detail=(detail + f" origin={command.origin} ip={command.ip_address or 'n/a'}"),
            )
        )
        db_session.add(
            PaymentHistory(
                company_id=company_id,
                subscription_id=result.subscription_id,
                event=f"subscription_{result.command_name}",
                detail=detail,
                source=command.origin,
                status=result.status_after,
                payload_json=json.dumps(
                    {
                        "command": result.command_name,
                        "origin": command.origin,
                        "status_before": result.status_before,
                        "status_after": result.status_after,
                        "plan_before_id": result.plan_before_id,
                        "plan_after_id": result.plan_after_id,
                        "metadata": command.metadata,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        )

    @staticmethod
    def _target_subscription_for_command(company_id: int, subscription_id: int | None):
        from app import Subscription

        if subscription_id is not None:
            return Subscription.query.filter_by(id=subscription_id, company_id=company_id).first()
        return SubscriptionService.active_subscription_for_company(company_id)

    @staticmethod
    def _new_subscription(
        db_session,
        *,
        company,
        plan,
        status: str,
        start_date,
        next_billing_date,
        renewal_enabled: bool,
        metadata: dict[str, Any],
        last_payment_date=None,
        trial_end=None,
    ):
        from app import Subscription

        subscription = Subscription(
            company_id=company.id,
            plan_id=plan.id if plan else None,
            status=SubscriptionService._normalize_state(status, default=SubscriptionService.STATE_PENDING),
            start_date=start_date,
            starts_at=start_date,
            trial_end=trial_end,
            ends_at=next_billing_date,
            next_billing_date=next_billing_date,
            last_payment_date=last_payment_date,
            renewal_enabled=renewal_enabled,
            auto_renew=renewal_enabled,
            cancel_at_period_end=not renewal_enabled,
        )
        SubscriptionService._set_metadata(subscription, metadata)
        db_session.add(subscription)
        db_session.flush()
        return subscription

    @staticmethod
    def _handle_create_subscription(db_session, *, company, command: CreateSubscriptionCommand) -> CommandResult:
        from app import Plan, Subscription, utcnow

        plan = db_session.get(Plan, command.plan_id) if command.plan_id else None
        if plan is None:
            raise SubscriptionCommandError("Plan inválido para crear suscripción.")

        now = command.start_date or utcnow()
        status = SubscriptionService._normalize_state(command.status, default=SubscriptionService.STATE_PENDING)
        has_successful_payment = (
            db_session.query(Subscription.id)
            .filter(Subscription.company_id == company.id, Subscription.last_payment_date.isnot(None))
            .first()
            is not None
        )
        trial_end = SubscriptionService.trial_end_for_company(company, now=now)
        in_trial_window = bool(trial_end and now <= trial_end)
        if status == SubscriptionService.STATE_PENDING and float(plan.price or 0) > 0 and in_trial_window and not has_successful_payment:
            status = SubscriptionService.STATE_TRIAL

        next_due = command.next_billing_date or (now + timedelta(days=int(plan.duration_days or 30)))
        if status == SubscriptionService.STATE_TRIAL:
            next_due = trial_end

        sub = SubscriptionService._new_subscription(
            db_session,
            company=company,
            plan=plan,
            status=status,
            start_date=now,
            next_billing_date=next_due,
            renewal_enabled=bool(command.renewal_enabled),
            metadata={
                "managed_by": "superadmin" if (command.actor_role or "").lower() == "superadmin" else "system",
                "is_manual": False,
                "origin": command.origin,
            },
            trial_end=next_due if status == SubscriptionService.STATE_TRIAL else None,
        )
        return CommandResult(
            command_name="CreateSubscriptionCommand",
            subscription_id=sub.id,
            company_id=company.id,
            status_before=None,
            status_after=sub.status,
            plan_before_id=None,
            plan_after_id=sub.plan_id,
            details={"created": True},
        )

    @staticmethod
    def _handle_change_plan(db_session, *, company, command: ChangePlanCommand) -> CommandResult:
        from app import Plan, utcnow

        plan = db_session.get(Plan, command.plan_id) if command.plan_id else None
        if plan is None:
            raise SubscriptionCommandError("Plan inválido para cambio de plan.")

        current = SubscriptionService.active_subscription_for_company(company.id)
        if current is not None and current.plan_id == plan.id and SubscriptionService._normalize_state(current.status) in SubscriptionService.OPEN_STATUSES:
            return CommandResult(
                command_name="ChangePlanCommand",
                subscription_id=current.id,
                company_id=company.id,
                status_before=current.status,
                status_after=current.status,
                plan_before_id=current.plan_id,
                plan_after_id=current.plan_id,
                details={"idempotent": True, "reason": "already_on_plan"},
            )

        now = utcnow()
        if current is not None:
            if SubscriptionService.is_manual_subscription(current) and (command.origin or "").lower() not in {"superadmin", "portal_confirm", "admin"}:
                raise SubscriptionCommandError("La suscripción manual sólo puede modificarse por acción administrativa o confirmación explícita.")
            SubscriptionService._close_for_change(current, now=now, actor_user_id=command.actor_user_id, origin=command.origin)

        trial_end = SubscriptionService.trial_end_for_company(company, now=now)
        is_paid = float(getattr(plan, "price", 0) or 0) > 0 and (plan.code or "").strip().lower() != "trial"
        if is_paid:
            new_status = SubscriptionService.STATE_PENDING_PAYMENT
            next_due = now
            trial_end_value = None
            last_payment_date = None
        else:
            new_status = SubscriptionService.STATE_TRIAL
            next_due = trial_end
            trial_end_value = trial_end
            last_payment_date = None

        new_sub = SubscriptionService._new_subscription(
            db_session,
            company=company,
            plan=plan,
            status=new_status,
            start_date=now,
            next_billing_date=next_due,
            renewal_enabled=not is_paid,
            metadata={
                "managed_by": "user",
                "is_manual": False,
                "origin": command.origin,
                "changed_by_user_id": command.actor_user_id,
            },
            last_payment_date=last_payment_date,
            trial_end=trial_end_value,
        )

        return CommandResult(
            command_name="ChangePlanCommand",
            subscription_id=new_sub.id,
            company_id=company.id,
            status_before=getattr(current, "status", None),
            status_after=new_sub.status,
            plan_before_id=getattr(current, "plan_id", None),
            plan_after_id=new_sub.plan_id,
            details={"previous_subscription_id": getattr(current, "id", None)},
        )

    @staticmethod
    def _handle_cancel(db_session, *, company, command: CancelSubscriptionCommand) -> CommandResult:
        from app import utcnow

        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción para cancelar.")

        status_before = subscription.status
        if command.cancel_at_period_end:
            subscription.cancel_at_period_end = True
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            target_state = SubscriptionService.STATE_CANCELLED
            if SubscriptionService._normalize_state(subscription.status) in {SubscriptionService.STATE_ACTIVE, SubscriptionService.STATE_TRIAL, SubscriptionService.STATE_PENDING_PAYMENT, SubscriptionService.STATE_PENDING_CONFIRMATION, SubscriptionService.STATE_PENDING}:
                SubscriptionService._transition(subscription, target_state, reason="cancel_subscription")
        else:
            SubscriptionService._transition(subscription, SubscriptionService.STATE_CANCELLED, reason="cancel_subscription_immediate")
            subscription.ends_at = utcnow()
            subscription.cancel_at_period_end = False
            subscription.renewal_enabled = False
            subscription.auto_renew = False

        return CommandResult(
            command_name="CancelSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=status_before,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
            details={"cancel_at_period_end": command.cancel_at_period_end},
        )

    @staticmethod
    def _handle_reactivate(db_session, *, company, command: ReactivateSubscriptionCommand) -> CommandResult:
        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción para reactivar.")

        status_before = subscription.status
        normalized = SubscriptionService._normalize_state(subscription.status)
        if normalized in {SubscriptionService.STATE_CANCELLED, SubscriptionService.STATE_SUSPENDED, SubscriptionService.STATE_EXPIRED, SubscriptionService.STATE_TRIAL_EXPIRED}:
            SubscriptionService._transition(subscription, SubscriptionService.STATE_ACTIVE, reason="reactivate")

        subscription.cancel_at_period_end = False
        subscription.renewal_enabled = True
        subscription.auto_renew = True
        return CommandResult(
            command_name="ReactivateSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=status_before,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
        )

    @staticmethod
    def _handle_renew(db_session, *, company, command: RenewSubscriptionCommand) -> CommandResult:
        from app import utcnow

        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción para renovar.")

        if SubscriptionService.is_manual_subscription(subscription):
            origin = (command.origin or "").lower()
            normalized_payment_status = (command.payment_status or "").strip().lower()
            payment_unlock_statuses = {"approved", "refunded"}
            if origin in {"webhook", "checkout"} and normalized_payment_status in payment_unlock_statuses:
                pass
            elif origin in {"webhook", "cron", "checkout"}:
                raise SubscriptionCommandError("Suscripción manual: renovación automática no permitida.")

        now = command.paid_at or utcnow()
        status_before = subscription.status
        normalized = SubscriptionService._normalize_state(subscription.status)
        if normalized in {SubscriptionService.STATE_PENDING, SubscriptionService.STATE_PENDING_PAYMENT, SubscriptionService.STATE_PENDING_CONFIRMATION, SubscriptionService.STATE_TRIAL, SubscriptionService.STATE_TRIAL_EXPIRED, SubscriptionService.STATE_EXPIRED, SubscriptionService.STATE_CANCELLED, SubscriptionService.STATE_SUSPENDED}:
            SubscriptionService._transition(subscription, SubscriptionService.STATE_ACTIVE, reason="renew")

        duration = int(subscription.plan.duration_days if subscription.plan else 30)
        base = subscription.next_billing_date if subscription.next_billing_date and subscription.next_billing_date > now else now
        subscription.last_payment_date = now
        subscription.start_date = base
        subscription.starts_at = base
        subscription.next_billing_date = base + timedelta(days=duration)
        subscription.ends_at = subscription.next_billing_date
        subscription.cancel_at_period_end = False
        subscription.renewal_enabled = True
        subscription.auto_renew = True

        return CommandResult(
            command_name="RenewSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=status_before,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
        )

    @staticmethod
    def _handle_extend(db_session, *, company, command: ExtendSubscriptionCommand) -> CommandResult:
        from app import utcnow

        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción para extender.")

        days = max(1, min(int(command.days or 1), 365))
        base = subscription.next_billing_date or utcnow()
        subscription.next_billing_date = base + timedelta(days=days)
        subscription.ends_at = subscription.next_billing_date
        return CommandResult(
            command_name="ExtendSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=subscription.status,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
            details={"days": days},
        )

    @staticmethod
    def _handle_expire(db_session, *, company, command: ExpireSubscriptionCommand) -> CommandResult:
        from app import utcnow

        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción para expirar.")
        status_before = subscription.status
        if SubscriptionService.is_manual_subscription(subscription) and (command.origin or "").lower() in {"webhook", "cron", "checkout"}:
            raise SubscriptionCommandError("Suscripción manual: expiración automática no permitida.")
        reason_normalized = (command.reason or "").strip().lower()
        target_state = SubscriptionService.STATE_SUSPENDED if "suspend" in reason_normalized else SubscriptionService.STATE_EXPIRED
        SubscriptionService._transition(subscription, target_state, reason=command.reason or "expire")
        subscription.ends_at = subscription.ends_at or utcnow()
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        return CommandResult(
            command_name="ExpireSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=status_before,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
            details={"reason": command.reason},
        )

    @staticmethod
    def _handle_change_payment_method(db_session, *, company, command: ChangePaymentMethodCommand) -> CommandResult:
        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción para actualizar método de pago.")

        metadata = SubscriptionService._set_metadata(
            subscription,
            {
                "preferred_payment_method": (command.payment_method or "").strip() or None,
                "payment_method_updated_by": command.actor_user_id,
                "payment_method_origin": command.origin,
            },
        )
        if command.external_reference:
            subscription.external_reference = command.external_reference
        mp_subscription_id = (command.metadata or {}).get("mercadopago_subscription_id")
        if mp_subscription_id:
            subscription.mercadopago_subscription_id = str(mp_subscription_id)
        return CommandResult(
            command_name="ChangePaymentMethodCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=subscription.status,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
            details={"preferred_payment_method": metadata.get("preferred_payment_method")},
        )

    @staticmethod
    def _handle_assign_manual(db_session, *, company, command: AssignManualSubscriptionCommand) -> CommandResult:
        from app import Plan, utcnow

        plan = db_session.get(Plan, command.plan_id) if command.plan_id else None
        if plan is None:
            raise SubscriptionCommandError("Plan inválido para asignación manual.")

        current = SubscriptionService.active_subscription_for_company(company.id)
        now = utcnow()
        if current is not None:
            SubscriptionService._close_for_change(current, now=now, actor_user_id=command.actor_user_id, origin="manual_assign")

        next_due = now + timedelta(days=int(plan.duration_days or 30))
        subscription = SubscriptionService._new_subscription(
            db_session,
            company=company,
            plan=plan,
            status=SubscriptionService.STATE_ACTIVE,
            start_date=now,
            next_billing_date=next_due,
            renewal_enabled=False,
            metadata={
                "is_manual": True,
                "managed_by": "superadmin",
                "created_by_admin": command.created_by_admin or command.actor_user_id,
                "manual_reason": (command.manual_reason or "").strip(),
                "origin": command.origin,
            },
            last_payment_date=now,
        )
        return CommandResult(
            command_name="AssignManualSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=getattr(current, "status", None),
            status_after=subscription.status,
            plan_before_id=getattr(current, "plan_id", None),
            plan_after_id=subscription.plan_id,
        )

    @staticmethod
    def _handle_remove_manual(db_session, *, company, command: RemoveManualSubscriptionCommand) -> CommandResult:
        subscription = SubscriptionService._target_subscription_for_command(company.id, command.subscription_id)
        if subscription is None:
            raise SubscriptionCommandError("No hay suscripción manual para remover.")
        metadata = SubscriptionService._set_metadata(
            subscription,
            {
                "is_manual": False,
                "managed_by": "system",
                "manual_removed_by": command.actor_user_id,
                "manual_removed_origin": command.origin,
            },
        )
        return CommandResult(
            command_name="RemoveManualSubscriptionCommand",
            subscription_id=subscription.id,
            company_id=company.id,
            status_before=subscription.status,
            status_after=subscription.status,
            plan_before_id=subscription.plan_id,
            plan_after_id=subscription.plan_id,
            details={"is_manual": metadata.get("is_manual")},
        )

    @staticmethod
    def _trial_days() -> int:
        try:
            from services.plan_service import PlanService

            return int(getattr(PlanService, "TRIAL_DAYS", 10) or 10)
        except Exception:
            return 10

    @staticmethod
    def trial_end_for_company(company, now=None):
        from app import utcnow

        current = now or utcnow()
        trial_days = SubscriptionService._trial_days()
        return getattr(company, "trial_ends_at", None) or ((getattr(company, "created_at", None) or current) + timedelta(days=trial_days))

    @staticmethod
    def resolve_company_access_state(company, subscription=None, now=None):
        from app import utcnow

        current = now or utcnow()
        trial_end = SubscriptionService.trial_end_for_company(company, now=current)
        raw_status = ((getattr(subscription, "status", None) or SubscriptionService.STATE_TRIAL) if subscription is not None else SubscriptionService.STATE_TRIAL).lower()
        in_trial_window = bool(trial_end and current <= trial_end)
        is_manual = bool(subscription is not None and SubscriptionService.is_manual_subscription(subscription))

        if not getattr(company, "active", True):
            return {
                "status": "suspended",
                "subscription_status": raw_status,
                "can_access": False,
                "reason": "La empresa ha sido suspendida.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": getattr(subscription, "next_billing_date", None) if subscription is not None else None,
            }

        if subscription is None:
            if in_trial_window:
                return {
                    "status": SubscriptionService.STATE_TRIAL,
                    "subscription_status": SubscriptionService.STATE_TRIAL,
                    "can_access": True,
                    "reason": "Periodo de prueba activo.",
                    "trial_ends_at": trial_end,
                    "reference_date": trial_end,
                    "next_billing_date": trial_end,
                }
            return {
                "status": SubscriptionService.STATE_TRIAL_EXPIRED,
                "subscription_status": SubscriptionService.STATE_TRIAL_EXPIRED,
                "can_access": False,
                "reason": "Tu prueba expiró. Suscribite para continuar.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": trial_end,
            }

        if raw_status in SubscriptionService.TRIAL_STATUSES or raw_status in SubscriptionService.PENDING_STATUSES:
            if in_trial_window:
                return {
                    "status": SubscriptionService.STATE_TRIAL,
                    "subscription_status": raw_status,
                    "can_access": True,
                    "reason": "Periodo de prueba activo.",
                    "trial_ends_at": trial_end,
                    "reference_date": trial_end,
                    "next_billing_date": trial_end,
                }
            return {
                "status": SubscriptionService.STATE_TRIAL_EXPIRED,
                "subscription_status": raw_status,
                "can_access": False,
                "reason": "Tu prueba expiró. Suscribite para continuar.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": trial_end,
            }

        if raw_status in SubscriptionService.BLOCKED_STATUSES:
            return {
                "status": raw_status,
                "subscription_status": raw_status,
                "can_access": False,
                "reason": "La suscripción no está activa.",
                "trial_ends_at": trial_end,
                "reference_date": getattr(subscription, "next_billing_date", None) or getattr(subscription, "ends_at", None) or trial_end,
                "next_billing_date": getattr(subscription, "next_billing_date", None),
            }

        if raw_status in SubscriptionService.ACTIVE_STATUSES:
            paid_limit = getattr(subscription, "next_billing_date", None) or getattr(subscription, "ends_at", None)
            # Suscripciones manuales pueden quedar abiertas sin fecha límite,
            # pero si tienen fecha configurada deben bloquearse al vencer.
            if is_manual:
                if paid_limit and current > paid_limit:
                    return {
                        "status": SubscriptionService.STATE_EXPIRED,
                        "subscription_status": raw_status,
                        "can_access": False,
                        "reason": "La suscripción manual venció en la fecha configurada.",
                        "trial_ends_at": trial_end,
                        "reference_date": paid_limit,
                        "next_billing_date": paid_limit,
                    }
                return {
                    "status": SubscriptionService.STATE_ACTIVE,
                    "subscription_status": raw_status,
                    "can_access": True,
                    "reason": "Suscripción manual activa.",
                    "trial_ends_at": trial_end,
                    "reference_date": paid_limit,
                    "next_billing_date": paid_limit,
                }
            if paid_limit and current > paid_limit:
                grace_limit = paid_limit + timedelta(days=SubscriptionService.OVERDUE_GRACE_DAYS)
                if current < grace_limit:
                    return {
                        "status": SubscriptionService.STATE_PENDING_PAYMENT,
                        "subscription_status": raw_status,
                        "can_access": True,
                        "reason": (
                            "La suscripción está vencida y en período de gracia "
                            f"({SubscriptionService.OVERDUE_GRACE_DAYS} días). Registrá el pago para evitar bloqueo."
                        ),
                        "trial_ends_at": trial_end,
                        "reference_date": grace_limit,
                        "next_billing_date": paid_limit,
                    }
                return {
                    "status": SubscriptionService.STATE_EXPIRED,
                    "subscription_status": raw_status,
                    "can_access": False,
                    "reason": "La suscripción está vencida y superó el período de gracia.",
                    "trial_ends_at": trial_end,
                    "reference_date": paid_limit,
                    "next_billing_date": paid_limit,
                }
            return {
                "status": SubscriptionService.STATE_ACTIVE,
                "subscription_status": raw_status,
                "can_access": True,
                "reason": "Suscripción activa.",
                "trial_ends_at": trial_end,
                "reference_date": paid_limit,
                "next_billing_date": paid_limit,
            }

        # Fallback conservador: durante trial permitimos, fuera de trial bloqueamos.
        if in_trial_window:
            return {
                "status": SubscriptionService.STATE_TRIAL,
                "subscription_status": raw_status,
                "can_access": True,
                "reason": "Periodo de prueba activo.",
                "trial_ends_at": trial_end,
                "reference_date": trial_end,
                "next_billing_date": trial_end,
            }
        return {
            "status": SubscriptionService.STATE_TRIAL_EXPIRED,
            "subscription_status": raw_status,
            "can_access": False,
            "reason": "Tu prueba expiró. Suscribite para continuar.",
            "trial_ends_at": trial_end,
            "reference_date": trial_end,
            "next_billing_date": trial_end,
        }

    @staticmethod
    def ensure_company_trial(db_session, company, trial_plan):
        from app import utcnow

        existing = SubscriptionService.active_subscription_for_company(company.id)
        if existing:
            return existing

        now = utcnow()
        command = CreateSubscriptionCommand(
            company_id=company.id,
            actor_user_id=None,
            actor_role="system",
            origin="auto_trial",
            plan_id=(trial_plan.id if trial_plan else None),
            status=SubscriptionService.STATE_TRIAL,
            start_date=now,
            renewal_enabled=True,
        )
        result = SubscriptionService.run_command(db_session, command)
        from app import Subscription

        return db_session.get(Subscription, result.subscription_id)

    @staticmethod
    def change_plan_transaction(
        db_session,
        *,
        company,
        plan,
        actor_user_id: int | None,
        origin: str,
        managed_by: str = "user",
    ):
        result = SubscriptionService.run_command(
            db_session,
            ChangePlanCommand(
                company_id=company.id,
                actor_user_id=actor_user_id,
                actor_role=managed_by,
                origin=origin,
                plan_id=plan.id,
                idempotency_key=f"change-plan:{company.id}:{plan.id}:{origin}:{actor_user_id or 0}",
            ),
        )
        from app import Subscription

        subscription = db_session.get(Subscription, result.subscription_id)
        return {"previous": None, "subscription": subscription}

    @staticmethod
    def start_or_change_plan(db_session, *, company, plan, user_id: int | None, external_reference: str | None = None):
        result = SubscriptionService.run_command(
            db_session,
            ChangePlanCommand(
                company_id=company.id,
                actor_user_id=user_id,
                actor_role="system",
                origin="service_start_or_change",
                plan_id=plan.id,
                idempotency_key=f"start-or-change:{company.id}:{plan.id}:{user_id or 0}",
            ),
        )
        from app import Subscription

        subscription = db_session.get(Subscription, result.subscription_id)
        subscription.external_reference = external_reference
        return subscription

    @staticmethod
    def apply_payment_status(subscription, payment_status: str):
        normalized = (payment_status or SubscriptionService.STATE_PENDING).lower()
        mapped = SubscriptionService.PAYMENT_STATUS_MAP.get(normalized, normalized)
        if mapped in {SubscriptionService.STATE_ACTIVE, "approved"}:
            SubscriptionService._transition(subscription, SubscriptionService.STATE_ACTIVE, reason="payment_status")
            from app import utcnow

            now = utcnow()
            subscription.last_payment_date = now
            duration = int(subscription.plan.duration_days if subscription.plan else 30)
            base = subscription.next_billing_date if subscription.next_billing_date and subscription.next_billing_date > now else now
            subscription.start_date = base
            subscription.starts_at = base
            subscription.next_billing_date = base + timedelta(days=duration)
            subscription.ends_at = subscription.next_billing_date
            subscription.renewal_enabled = True
            subscription.auto_renew = True
            return subscription

        if mapped in {SubscriptionService.STATE_PENDING, SubscriptionService.STATE_PENDING_PAYMENT, SubscriptionService.STATE_PENDING_CONFIRMATION}:
            current_normalized = SubscriptionService._normalize_state(subscription.status)
            if mapped == SubscriptionService.STATE_PENDING_PAYMENT:
                target = SubscriptionService.STATE_PENDING_PAYMENT
            elif mapped == SubscriptionService.STATE_PENDING_CONFIRMATION:
                target = SubscriptionService.STATE_PENDING_CONFIRMATION
            else:
                target = (
                    SubscriptionService.STATE_PENDING_PAYMENT
                    if current_normalized == SubscriptionService.STATE_PENDING_PAYMENT
                    else SubscriptionService.STATE_PENDING
                )
            SubscriptionService._transition(subscription, target, reason="payment_status_pending")
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            return subscription

        if mapped in {SubscriptionService.STATE_CANCELLED, SubscriptionService.STATE_EXPIRED, SubscriptionService.STATE_SUSPENDED, "rejected"}:
            terminal_target = SubscriptionService.STATE_SUSPENDED if mapped == SubscriptionService.STATE_SUSPENDED else SubscriptionService.STATE_EXPIRED
            if mapped == SubscriptionService.STATE_CANCELLED:
                terminal_target = SubscriptionService.STATE_CANCELLED
            SubscriptionService._transition(subscription, terminal_target, reason="payment_status_terminal")
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            return subscription

        return subscription
