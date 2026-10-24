"""High-confidence, deterministic off-topic guard for StockArMobile AI agents.

This is intentionally a conservative pre-filter: it blocks clear requests for
unrelated general-topic answers, not isolated topic words that may name products.
The role-specific system prompt remains the broader backstop.
"""
from __future__ import annotations

import re
import unicodedata


def _normalize(value: str) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return "".join(char for char in raw if not unicodedata.combining(char))


# Explicit requests for general-topic answers. Patterns are deliberately phrased
# as intents; a product mention such as "¿Tenés Biblias en stock?" is not enough.
_OUT_OF_SCOPE_INTENTS = (
    (
        "religion",
        re.compile(
            r"\b(?:que dice|que ensena|explicame|explica|interpreta|interpretame|resumime|resume|"
            r"cita|dame|decime|contame)\b.{0,65}\b(?:la biblia|biblia|versiculos?|salmos?|"
            r"evangelio|jesus|jesucristo|dios|el coran|coran|torah|religion)\b"
            r"|\b(?:quien es|quien fue)\s+(?:jesus|jesucristo|dios)\b"
            r"|\b(?:oracion|rezos?)\s+(?:para|por)\b"
        ),
    ),
    (
        "sports",
        re.compile(
            r"\b(?:quien gano|resultado de|tabla de posiciones|cuando juega|a que hora juega|"
            r"goles de|resumen del partido|proximo partido|proximo encuentro)\b"
            r"|\b(?:explicame|explica)\s+(?:las reglas del|el reglamento del)\s+"
            r"(?:futbol|basquet|tenis|rugby|voleibol)\b"
        ),
    ),
    (
        "politics",
        re.compile(
            r"\b(?:por quien(?: deberia| debo| tendria que)? votar|intencion de voto|resultados? de las elecciones|"
            r"quien es el presidente|quien es la presidenta|que opinas del presidente|"
            r"partidos politicos|encuesta electoral)\b"
        ),
    ),
    (
        "entertainment",
        re.compile(
            r"\b(?:resumen de|explicame el final de|quien interpreta a|quien actua en|"
            r"que paso en la serie|letra de la cancion)\b.{0,65}\b"
            r"(?:pelicula|serie|novela|cancion|artista|actor|actriz|temporada)\b"
        ),
    ),
    (
        "schoolwork",
        re.compile(
            r"\b(?:haceme|hace|resolveme|resuelve|resolvé)\s+(?:la )?(?:tarea|deberes|trabajo practico)\b"
            r"|\b(?:explicame|explica|resumime|resume)\s+(?:la )?"
            r"(?:fotosintesis|gravedad|sistema solar|guerra mundial|revolucion francesa)\b"
            r"|\b(?:que es|define|definime)\s+(?:la )?"
            r"(?:fotosintesis|gravedad|sistema solar|celula eucariota)\b"
        ),
    ),
)

# Commercial/operational signals prevent false positives where the topic word
# refers to an item or campaign handled by the agent.
_RELEVANT_INTENTS = {
    "vendedor": re.compile(
        r"\b(?:stock|catalogo|producto|precio|precios|pedido|presupuesto|carrito|compra|comprar|"
        r"vender|envio|entrega|disponible|disponibilidad|horario|promocion|promociones|pago|"
        r"factura|cantidad|unidad|marca|codigo)\b"
    ),
    "asistente": re.compile(
        r"\b(?:stock|inventario|producto|precio|venta|ventas|cliente|clientes|factura|proveedor|"
        r"reporte|informe|caja|compra|compras|pedido|presupuesto|empresa|comercio|gestion|"
        r"stockarmobile|facturacion)\b"
    ),
    "analista": re.compile(
        r"\b(?:ventas|venta|stock|rotacion|rentabilidad|margen|precios|precio|clientes|"
        r"tendencia|analisis|analizar|periodo|facturacion|productos|inventario|oportunidad)\b"
    ),
    "marketing": re.compile(
        r"\b(?:marketing|campana|campanas|promocion|promociones|publicidad|producto|productos|"
        r"clientes|segmento|ventas|vender|comercio|marca|stockarmobile|publicar|anuncio)\b"
    ),
    "comercial": re.compile(
        r"\b(?:stockarmobile|stock ar mobile|plan|planes|precio|precios|contratacion|contratar|"
        r"suscripcion|prueba gratuita|demo|funcionalidad|funcionalidades|integracion|integraciones|"
        r"agentes ia|sistema de gestion|comercio|negocio|referidos|mercado pago)\b"
    ),
}


def detect_out_of_scope_intent(agent_key: str, message: str) -> str | None:
    """Return a high-confidence unrelated topic category, else None."""
    key = str(agent_key or "").strip().lower()
    normalized = _normalize(message)
    if not normalized or key not in _RELEVANT_INTENTS:
        return None
    # A real operational/commercial request takes priority over a topic word.
    if _RELEVANT_INTENTS[key].search(normalized):
        return None
    for category, pattern in _OUT_OF_SCOPE_INTENTS:
        if pattern.search(normalized):
            return category
    return None
