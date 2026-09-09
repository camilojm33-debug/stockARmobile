"""Blueprint de dashboard: metricas, onboarding y tour guiado."""

import copy
import uuid

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation
from services.ai_agent.config_service import ensure_agent_for_key
from services.ai_agent.usage_service import can_use_ai
from services.ai_agent.orchestrator import AgentOrchestrator
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.dashboard_service import build_dashboard_context
from services.invoice_upload_service import InvoiceUploadError, InvoiceUploadService
from services.invoice_ai_service import InvoiceAIError, InvoiceAIService
from services.invoice_matching_service import InvoiceMatchingService, normalize
from services.invoice_purchase_service import InvoicePurchaseError, InvoicePurchaseService
from app import tenant_required

bp = Blueprint("dashboard", __name__)


def _invoice_record(upload_id, company_id):
    for conversation in Conversation.query.filter_by(company_id=company_id).all():
        # deepcopy: evita compartir referencias anidadas con conversation.metadata_json (rompia la deteccion de cambios).
        metadata = copy.deepcopy(conversation.metadata_json or {})
        uploads = list(metadata.get("invoice_uploads") or [])
        for index, upload in enumerate(uploads):
            if str(upload.get("upload_id")) == str(upload_id):
                return conversation, metadata, uploads, index, upload
    return None


def _save_invoice(conversation, metadata, uploads, index, upload):
    uploads[index] = upload
    metadata["invoice_uploads"] = uploads
    conversation.metadata_json = metadata
    db.session.commit()


def _already_applied_invoice(upload_id, company_id, document_hash):
    if not document_hash:
        return None
    for conversation in Conversation.query.filter_by(company_id=company_id).all():
        for upload in (conversation.metadata_json or {}).get("invoice_uploads", []):
            if str(upload.get("upload_id")) != str(upload_id) and upload.get("status") == "APLICADA" and (upload.get("invoice") or {}).get("document_hash") == document_hash:
                return upload.get("result") or {"status": "APLICADA"}
    return None


@bp.route("/")
@tenant_required
def index():
    return render_template("dashboard/index.html", **build_dashboard_context())


@bp.route("/stats")
@tenant_required
def stats():
    from app import Product, Sale, SaleItem, db, scope_query_to_company

    company_id = getattr(current_user, "company_id", None)
    categories_result = (
        scope_query_to_company(
            db.session.query(Product.category.label("category"), db.func.sum(SaleItem.quantity).label("total_sold"))
            .join(Product, SaleItem.product_id == Product.id)
            .join(Sale, SaleItem.sale_id == Sale.id)
            .filter(Sale.company_id == company_id)
            .group_by(Product.category),
            Product,
        ).all()
    )
    categories_list = [cat[0] or "N/A" for cat in categories_result]
    categories_data = [cat[1] or 0 for cat in categories_result]
    return render_template("dashboard/stats.html", categories=categories_list, categories_data=categories_data)


@bp.route("/inicio-rapido")
@tenant_required
def quick_start():
    return render_template("dashboard/quick_start.html")


@bp.route("/onboarding", methods=["GET", "POST"])
@tenant_required
def onboarding():
    pending_company_id = session.get("post_register_onboarding_company_id")
    current_company_id = getattr(current_user, "company_id", None)
    if pending_company_id and int(pending_company_id) != int(current_company_id or 0):
        return redirect(url_for("dashboard.index"))

    steps = [
        {"step": 1, "title": "Datos del negocio", "description": "Completa información comercial, fiscal y de contacto en Mi Empresa."},
        {"step": 2, "title": "Moneda y configuración", "description": "Confirma moneda, formato y ajustes generales antes de operar."},
        {"step": 3, "title": "Productos", "description": "Carga tus productos y define stock mínimo para empezar ordenado."},
        {"step": 4, "title": "Clientes", "description": "Registra clientes frecuentes para ventas rápidas y seguimiento."},
        {"step": 5, "title": "Primera venta", "description": "Abre caja, agrega productos y registra tu primera operación."},
        {"step": 6, "title": "Caja y reporte", "description": "Cierra caja, revisa totales y valida diferencias al final del día."},
    ]

    if request.method == "POST":
        session.pop("post_register_onboarding_company_id", None)
        session["guided_tour_pending"] = True
        session.pop("guided_tour_seen", None)
        flash("Onboarding completado. Te mostramos el recorrido guiado.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("dashboard/onboarding.html", steps=steps, progress=100)


@bp.route("/tour/complete", methods=["POST"])
@tenant_required
def tour_complete():
    session.pop("guided_tour_pending", None)
    session["guided_tour_seen"] = True
    flash("Recorrido guiado finalizado.", "info")
    next_url = (request.form.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("dashboard.index"))


@bp.route("/ai-agent/chat", methods=["POST"])
@tenant_required
def ai_agent_chat():
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    message = payload.get("message")
    conversation_id = payload.get("conversation_id")
    agent_key = str(payload.get("agent") or "asistente").strip().lower()
    invoice_upload = request.files.get("invoice_file")
    if agent_key not in {"asistente", "vendedor", "analista", "marketing"}:
        return jsonify({"success": False, "error": "Agente inválido."}), 400

    if not isinstance(message, str) or not message.strip():
        return jsonify({"success": False, "error": "El mensaje es obligatorio."}), 400

    company_id = getattr(current_user, "company_id", None)
    if company_id in (None, ""):
        return jsonify({"success": False, "error": "No hay empresa activa para esta sesión."}), 403

    from app import Company

    company = Company.query.filter_by(id=company_id).first()
    access = can_use_ai(company, agent_key)
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    if invoice_upload is not None and agent_key != "asistente":
        return jsonify({"success": False, "error": "Las facturas solo pueden recibirse desde el Asistente Empresarial."}), 403
    if invoice_upload is not None:
        invoice_access = can_use_ai(company, "facturas")
        if not invoice_access.allowed:
            return jsonify({"success": False, "error": invoice_access.reason}), 403

    if conversation_id not in (None, ""):
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Identificador de conversación inválido."}), 400

        conversation = (
            db.session.query(Conversation)
            .filter(Conversation.id == conversation_id, Conversation.company_id == company_id)
            .first()
        )
        if conversation is None:
            return jsonify({"success": False, "error": "La conversación no pertenece a tu empresa."}), 403
        if conversation.agent_id:
            current_agent = db.session.get(__import__("stockarmobile.models.conversations", fromlist=["Agent"]).Agent, conversation.agent_id)
            if current_agent is not None and current_agent.name.lower() != {"asistente": "asistente empresarial", "vendedor": "vendedor 24 hs", "analista": "analista ia", "marketing": "marketing ia"}[agent_key]:
                return jsonify({"success": False, "error": "La conversación pertenece a otro agente."}), 409
    else:
        selected_agent = ensure_agent_for_key(company_id, agent_key)
        if selected_agent is None:
            selected_agent = __import__("services.ai_agent.config_service", fromlist=["choose_agent"]).choose_agent(company_id, channel="whatsapp" if agent_key == "vendedor" else "web")
        conversation = Conversation(
            company_id=company_id,
            agent_id=selected_agent.id,
            channel="web",
        )
        db.session.add(conversation)
        db.session.flush()

    if invoice_upload is not None:
        upload_record = None
        try:
            upload_record = InvoiceUploadService.receive(
                invoice_upload,
                company_id=company_id,
                user_id=current_user.id,
                conversation_id=conversation.id,
            )
            conversation_metadata = dict(conversation.metadata_json or {})
            invoice_uploads = list(conversation_metadata.get("invoice_uploads") or [])
            invoice_uploads.append(upload_record)
            conversation_metadata["invoice_uploads"] = invoice_uploads
            conversation.metadata_json = conversation_metadata
            db.session.commit()
        except InvoiceUploadError as exc:
            db.session.rollback()
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception:
            db.session.rollback()
            if upload_record is not None:
                InvoiceUploadService.delete(upload_record, company_id=company_id)
            current_app.logger.exception(
                "Error registrando factura temporal: company_id=%s conversation_id=%s",
                company_id,
                conversation.id,
            )
            return jsonify({"success": False, "error": "No se pudo recibir la factura. Intenta nuevamente."}), 500

        return jsonify(
            {
                "success": True,
                "conversation_id": conversation.id,
                "document_id": upload_record["upload_id"],
                "status": upload_record["status"],
                "content": "Factura recibida correctamente.\n\nArchivo: "
                f"{upload_record['original_name']}\n\nEstado: Pendiente de procesamiento.",
            }
        )

    try:
        result = AgentRuntime.process(
            company_id=company_id,
            conversation_id=conversation.id,
            message=message.strip(),
            channel="web",
            sender_id=current_user.id,
            idempotency_key=str(uuid.uuid4()),
            metadata={},
            include_system_prompt=False,
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Error procesando chat del agente IA: company_id=%s conversation_id=%s agent=%s",
            company_id,
            conversation.id,
            agent_key,
        )
        return jsonify({"success": False, "error": "No se pudo procesar tu solicitud."}), 500

    return jsonify(
        {
            "success": True,
            "conversation_id": result.get("conversation_id"),
            "message_id": result.get("message_id"),
            "assistant_message_id": result.get("assistant_message_id"),
            "content": result.get("content"),
        }
    )


@bp.route("/ai-agent/invoices/<upload_id>/process", methods=["POST"])
@tenant_required
def process_invoice(upload_id):
    company_id = getattr(current_user, "company_id", None)
    found = _invoice_record(upload_id, company_id)
    if found is None:
        return jsonify({"success": False, "error": "Factura no encontrada."}), 404
    conversation, metadata, uploads, index, upload = found
    if upload.get("status") in {"PROCESANDO", "PROCESADA", "REQUIERE_REVISION", "LISTA_PARA_CONFIRMAR", "CONFIRMADA", "APLICADA"}:
        return jsonify({"success": True, "status": upload.get("status"), "preview": upload.get("invoice")}), 200
    from app import Company, Product, Supplier
    company = Company.query.filter_by(id=company_id).first()
    access = can_use_ai(company, "facturas")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    upload["status"] = "PROCESANDO"
    _save_invoice(conversation, metadata, uploads, index, upload)
    try:
        extracted = InvoiceAIService(AgentRuntime.provider()).extract(upload, company_id=company_id)
        previous_result = _already_applied_invoice(upload_id, company_id, extracted.get("document_hash"))
        if previous_result is not None:
            raise InvoiceAIError("Esta factura ya fue aplicada anteriormente en esta empresa.")
        products = Product.query.filter_by(company_id=company_id, active=True).all()
        suppliers = Supplier.query.filter_by(company_id=company_id, active=True).all()
        extracted["supplier_match"] = InvoiceMatchingService.supplier(suppliers=suppliers, name=(extracted.get("supplier") or {}).get("name"))
        extracted["matches"] = InvoiceMatchingService.products(products=products, items=extracted.get("items") or [])
        extracted["status"] = "REQUIERE_REVISION" if extracted.get("requires_review") or extracted["supplier_match"].get("status") == "AMBIGUO" or any(line["matching_status"] in {"AMBIGUO", "MATCH_PROPUESTO"} for line in extracted["matches"]) else "LISTA_PARA_CONFIRMAR"
        # metadata_json (SQLite/Postgres JSON) no serializa Decimal: se persiste una copia JSON-segura, preservando precision como string.
        upload["invoice"] = InvoiceAIService.json_safe(extracted)
        upload["status"] = extracted["status"]
        if not upload.get("usage_recorded"):
            from stockarmobile.models.conversations import Agent, ConversationMessage
            agent = db.session.get(Agent, conversation.agent_id)
            usage_message = ConversationMessage(conversation_id=conversation.id, company_id=company_id, sender_type="agent", sender_id=agent.id if agent else None, role="assistant", content="Extracción de factura preparada para revisión.", content_type="invoice_preview", metadata_json={"agent_key": "asistente", "invoice_upload_id": upload_id})
            db.session.add(usage_message)
            db.session.flush()
            if agent:
                from services.ai_agent.usage_service import record_ai_usage
                record_ai_usage(company_id=company_id, agent_id=agent.id, conversation_id=conversation.id, user_id=current_user.id, interaction_type="asistente", message_id=usage_message.id)
            upload["usage_recorded"] = True
        _save_invoice(conversation, metadata, uploads, index, upload)
        return jsonify({"success": True, "status": upload["status"], "preview": upload["invoice"]})
    except (InvoiceUploadError, InvoiceAIError, ValueError) as exc:
        db.session.rollback()
        upload["status"] = "ERROR"
        upload["error"] = str(exc)[:300]
        try:
            _save_invoice(conversation, metadata, uploads, index, upload)
        except Exception:
            db.session.rollback()
        return jsonify({"success": False, "status": "ERROR", "error": "No se pudo procesar la factura: " + str(exc)}), 422
    except RuntimeError:
        db.session.rollback()
        upload["status"] = "ERROR"
        upload["error"] = "Error del proveedor IA."
        current_app.logger.exception("Error del proveedor procesando factura IA: company_id=%s upload_id=%s", company_id, upload_id)
        try:
            _save_invoice(conversation, metadata, uploads, index, upload)
        except Exception:
            db.session.rollback()
        return jsonify({"success": False, "status": "ERROR", "error": "No se pudo comunicar con el proveedor IA."}), 422
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error procesando factura IA: company_id=%s upload_id=%s", company_id, upload_id)
        return jsonify({"success": False, "status": "ERROR", "error": "No se pudo procesar la factura."}), 500


@bp.route("/ai-agent/invoices/<upload_id>", methods=["GET"])
@tenant_required
def invoice_preview(upload_id):
    company_id = getattr(current_user, "company_id", None)
    found = _invoice_record(upload_id, company_id)
    if found is None:
        return jsonify({"success": False, "error": "Factura no encontrada."}), 404
    return jsonify({"success": True, "status": found[4].get("status"), "preview": found[4].get("invoice"), "original_name": found[4].get("original_name")})


@bp.route("/ai-agent/invoices/<upload_id>/resolve", methods=["POST"])
@tenant_required
def resolve_invoice_line(upload_id):
    company_id = getattr(current_user, "company_id", None)
    found = _invoice_record(upload_id, company_id)
    if found is None:
        return jsonify({"success": False, "error": "Factura no encontrada."}), 404
    conversation, metadata, uploads, index, upload = found
    invoice = upload.get("invoice") or {}
    payload = request.get_json(silent=True) or {}
    if payload.get("target") == "supplier":
        from app import Supplier
        # decision explicita del usuario: usar proveedor existente o proponer crear uno nuevo. No se crea ningun Supplier aqui.
        decision = payload.get("decision") or ("use_existing" if payload.get("supplier_id") else None)
        if decision == "create_new":
            proposed_name = str((invoice.get("supplier") or {}).get("name") or "").strip()
            if not proposed_name:
                return jsonify({"success": False, "error": "No hay un nombre de proveedor propuesto para crear."}), 400
            invoice["supplier_match"] = {"status": "NUEVO_PROVEEDOR_CONFIRMADO", "supplier_id": None, "name": proposed_name, "candidates": []}
        elif decision == "use_existing":
            supplier_id = payload.get("supplier_id")
            supplier = Supplier.query.filter_by(id=supplier_id, company_id=company_id, active=True).first()
            if supplier is None:
                return jsonify({"success": False, "error": "Proveedor inválido para esta empresa."}), 409
            invoice["supplier_match"] = {"status": "MATCH_EXACTO", "supplier_id": supplier.id, "name": supplier.name, "candidates": []}
        else:
            return jsonify({"success": False, "error": "Decisión de proveedor inválida."}), 400
        invoice["status"] = "LISTA_PARA_CONFIRMAR" if not invoice.get("requires_review") and not any(item.get("matching_status") in {"AMBIGUO", "MATCH_PROPUESTO"} for item in invoice.get("matches") or []) else "REQUIERE_REVISION"
        upload["invoice"] = invoice
        upload["status"] = invoice["status"]
        _save_invoice(conversation, metadata, uploads, index, upload)
        return jsonify({"success": True, "status": upload["status"], "preview": invoice})
    line_number = payload.get("line_number")
    line = next((item for item in invoice.get("matches") or [] if str(item.get("line_number")) == str(line_number)), None)
    if line is None:
        return jsonify({"success": False, "error": "Línea no encontrada."}), 400
    if payload.get("action") == "exclude":
        line["matching_status"] = "EXCLUIDA"
    else:
        from app import Product
        products = Product.query.filter_by(company_id=company_id, active=True).all()
        query = payload.get("code") or payload.get("barcode") or payload.get("description")
        candidates = [product for product in products if normalize(product.barcode) == normalize(query) or normalize(product.name) == normalize(query)]
        if len(candidates) != 1:
            return jsonify({"success": False, "error": "La selección no identifica un único producto."}), 409
        line.update({"matching_status": "MATCH_EXACTO", "matching_reason": "SELECCION_MANUAL", "product_id": candidates[0].id, "product_name": candidates[0].name, "product_code": candidates[0].barcode})
    invoice["status"] = "LISTA_PARA_CONFIRMAR" if not any(item.get("matching_status") in {"AMBIGUO", "MATCH_PROPUESTO"} for item in invoice.get("matches") or []) and not invoice.get("requires_review") else "REQUIERE_REVISION"
    upload["invoice"] = invoice
    upload["status"] = invoice["status"]
    _save_invoice(conversation, metadata, uploads, index, upload)
    return jsonify({"success": True, "status": upload["status"], "preview": invoice})


@bp.route("/ai-agent/invoices/<upload_id>/confirm", methods=["POST"])
@tenant_required
def confirm_invoice(upload_id):
    if getattr(current_user, "role", None) not in {"admin", "superadmin"}:
        return jsonify({"success": False, "error": "No tienes permisos para confirmar compras."}), 403
    company_id = getattr(current_user, "company_id", None)
    found = _invoice_record(upload_id, company_id)
    if found is None:
        return jsonify({"success": False, "error": "Factura no encontrada."}), 404
    conversation, metadata, uploads, index, upload = found
    try:
        duplicate_result = _already_applied_invoice(upload_id, company_id, (upload.get("invoice") or {}).get("document_hash"))
        if duplicate_result is not None:
            return jsonify({"success": True, "status": "APLICADA", "result": duplicate_result, "duplicate": True})
        result = InvoicePurchaseService.confirm(company_id=company_id, user_id=current_user.id, invoice=upload.get("invoice") or {}, conversation=conversation)
        upload["invoice"]["status"] = "APLICADA"
        upload["status"] = "APLICADA"
        upload["result"] = result
        _save_invoice(conversation, metadata, uploads, index, upload)
        return jsonify({"success": True, "status": "APLICADA", "result": result})
    except InvoicePurchaseError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error confirmando factura IA: company_id=%s upload_id=%s", company_id, upload_id)
        return jsonify({"success": False, "error": "No se pudo confirmar la compra."}), 500
