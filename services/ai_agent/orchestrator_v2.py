"""Production-oriented runtime for StockARmobile AI agents."""
from __future__ import annotations
import json, uuid
from services.ai_agent.providers.openai_compatible import OpenAICompatibleProvider
from services.ai_agent.config_service import VENDOR_AGENT_NAME, choose_agent
from services.ai_agent.tools.base import AgentTool
from services.ai_agent.tools.business_metrics import ResumenVentasTool, StockCriticoTool
from services.ai_agent.tools.customer_search import BuscarClienteTool
from services.ai_agent.tools.product_search import BuscarProductoTool
from services.ai_agent.tools.stock_query import ConsultarStockTool
from services.ai_agent.vendor_order_service import VendorOrderService
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent, AgentConfiguration, Conversation, ConversationMessage
LMStudioProvider=OpenAICompatibleProvider
VENDOR_SYSTEM_PROMPT="Sos el Vendedor 24 hs de StockARmobile. Consultá herramientas antes de afirmar precio o stock. No inventes información."
BUSINESS_SYSTEM_PROMPT="Sos el Asistente empresarial de StockARmobile. Usá herramientas para consultar datos reales y nunca inventes cifras."
class VendorCartTool(AgentTool):
    name="carrito_vendedor"; description="Consulta el carrito actual del cliente de WhatsApp."; input_schema={"type":"object","properties":{},"additionalProperties":False}
    def execute(self,**kwargs): return VendorOrderService.get_cart(company_id=self.company_id,conversation_id=self._context["conversation_id"])
class VendorAddTool(AgentTool):
    name="agregar_al_carrito"; description="Agrega un producto al carrito."; input_schema={"type":"object","properties":{"product_query":{"type":"string"},"quantity":{"type":"number","minimum":0.01}},"required":["product_query","quantity"],"additionalProperties":False}
    def execute(self,**kwargs): return VendorOrderService.update_cart(company_id=self.company_id,conversation_id=self._context["conversation_id"],items=[kwargs])
class VendorRemoveTool(AgentTool):
    name="quitar_del_carrito"; description="Quita un producto del carrito."; input_schema={"type":"object","properties":{"product_query":{"type":"string"}},"required":["product_query"],"additionalProperties":False}
    def execute(self,**kwargs): return VendorOrderService.remove_from_cart(company_id=self.company_id,conversation_id=self._context["conversation_id"],product_query=kwargs["product_query"])
class VendorOrderPreviewTool(AgentTool):
    name="preparar_pedido"; description="Prepara un pedido pendiente y genera un link seguro de pago."; input_schema={"type":"object","properties":{"customer_name":{"type":"string"}},"additionalProperties":False}
    def execute(self,**kwargs): return VendorOrderService.create_pending_order(company_id=self.company_id,conversation_id=self._context["conversation_id"],customer_name=str(kwargs.get("customer_name") or ""),customer_phone=str(self._context.get("customer_phone") or ""),actor_user_id=self._context.get("actor_user_id"))
class AgentRuntime:
    tool_registry={"buscar_producto":BuscarProductoTool,"consultar_stock":ConsultarStockTool,"buscar_cliente":BuscarClienteTool,"resumen_ventas":ResumenVentasTool,"stock_critico":StockCriticoTool,"carrito_vendedor":VendorCartTool,"agregar_al_carrito":VendorAddTool,"quitar_del_carrito":VendorRemoveTool,"preparar_pedido":VendorOrderPreviewTool}
    @classmethod
    def provider(cls): return LMStudioProvider()
    @classmethod
    def ensure_agent(cls,company_id,*,channel): return choose_agent(company_id,channel=channel)
    @classmethod
    def _config(cls,agent,company_id): return db.session.query(AgentConfiguration).filter(AgentConfiguration.agent_id==agent.id,AgentConfiguration.company_id==company_id).order_by(AgentConfiguration.id.asc()).first()
    @classmethod
    def _history(cls,company_id,conversation_id,limit=20):
        rows=db.session.query(ConversationMessage).filter(ConversationMessage.company_id==company_id,ConversationMessage.conversation_id==conversation_id).order_by(ConversationMessage.id.desc()).limit(limit).all()
        return [{"role":r.role,"content":str(r.content or "")} for r in reversed(rows) if r.role in {"user","assistant"}]
    @classmethod
    def _tool_definitions(cls): return [{"type":"function","function":{"name":n,"description":getattr(t,"description",""),"parameters":getattr(t,"input_schema",{"type":"object","properties":{}})}} for n,t in cls.tool_registry.items()]
    @classmethod
    def _execute_tool(cls,name,*,company_id,arguments,context=None):
        if not isinstance(arguments,dict): return {"success":False,"error":"arguments must be an object"}
        tool_class=cls.tool_registry.get(name)
        if tool_class is None:return {"success":False,"error":"tool_not_found"}
        if "company_id" in arguments:return {"success":False,"error":"company_id must be passed explicitly"}
        result=tool_class(company_id=company_id,**(context or {})).execute(**arguments)
        return result if isinstance(result,dict) else {"success":False,"error":"tool result must be an object"}
    @classmethod
    def process(cls,*,company_id,conversation_id,message,channel,sender_id=None,external_message_id=None,idempotency_key=None,metadata=None,provider_override=None,include_system_prompt=True):
        conversation=db.session.query(Conversation).filter(Conversation.id==conversation_id,Conversation.company_id==company_id).first()
        if conversation is None: raise ValueError("Conversation not found for company.")
        agent=db.session.query(Agent).filter(Agent.id==conversation.agent_id,Agent.company_id==company_id).first()
        if agent is None:
            agent=cls.ensure_agent(company_id,channel=channel); conversation.agent_id=agent.id; conversation.channel=channel; db.session.flush()
        if not agent.active:
            return {"status":"disabled","conversation_id":conversation.id,"company_id":company_id,"agent_id":agent.id,"content":""}
        if idempotency_key:
            duplicate=db.session.query(ConversationMessage).filter(ConversationMessage.company_id==company_id,ConversationMessage.idempotency_key==idempotency_key).first()
            if duplicate:return {"status":"duplicate","conversation_id":conversation.id,"message_id":duplicate.id,"content":""}
        history=cls._history(company_id,conversation.id,19); trace_id=str(uuid.uuid4())
        incoming=ConversationMessage(conversation_id=conversation.id,company_id=company_id,sender_type="user",sender_id=sender_id,role="user",content=str(message),content_type="text",external_message_id=external_message_id,idempotency_key=idempotency_key,trace_id=trace_id,metadata_json=metadata or {})
        db.session.add(incoming); db.session.flush()
        config=cls._config(agent,company_id); prompt=VENDOR_SYSTEM_PROMPT if agent.name==VENDOR_AGENT_NAME else BUSINESS_SYSTEM_PROMPT
        if config and config.system_prompt: prompt+=f"\n\nInstrucciones del comercio:\n{config.system_prompt}"
        messages=([{"role":"system","content":prompt}] if include_system_prompt else [])+history+[{"role":"user","content":str(message)}]
        kwargs={}
        if config:
            if config.model: kwargs["model"]=config.model
            if config.temperature is not None: kwargs["temperature"]=config.temperature
            if config.max_tokens is not None: kwargs["max_tokens"]=config.max_tokens
        provider=provider_override or cls.provider(); response=provider.generate(messages=messages,tools=cls._tool_definitions(),**kwargs)
        tool_call=response.get("tool_call") if isinstance(response,dict) else None; final_content=response.get("content") if isinstance(response,dict) else None
        if tool_call:
            name=tool_call.get("name"); args=tool_call.get("arguments") or {}; context={"conversation_id":conversation.id,"customer_phone":(metadata or {}).get("from") or "","actor_user_id":sender_id}; result=cls._execute_tool(name,company_id=company_id,arguments=args,context=context); tool_id=tool_call.get("id") or "call_1"
            tool_messages=messages+[{"role":"assistant","content":None,"tool_calls":[{"id":tool_id,"type":"function","function":{"name":name,"arguments":json.dumps(args,ensure_ascii=False)}}]},{"role":"tool","tool_call_id":tool_id,"content":json.dumps(result,ensure_ascii=False)}]
            response=provider.generate(messages=tool_messages,**kwargs); final_content=response.get("content") if isinstance(response,dict) else None
        if not final_content: raise RuntimeError("El proveedor IA no devolvió una respuesta.")
        assistant=ConversationMessage(conversation_id=conversation.id,company_id=company_id,sender_type="agent",sender_id=agent.id,role="assistant",content=str(final_content),content_type="text",trace_id=trace_id,metadata_json={"channel":channel,"agent_name":agent.name})
        db.session.add(assistant); db.session.commit()
        return {"status":"completed","company_id":company_id,"conversation_id":conversation.id,"agent_id":agent.id,"message_id":incoming.id,"assistant_message_id":assistant.id,"content":str(final_content),"trace_id":trace_id}
