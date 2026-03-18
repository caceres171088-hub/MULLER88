"""
API REST para gestión de equipo en Futmondo.

Endpoints disponibles:
  GET  /team                      - Jugadores del equipo
  GET  /team/{team_id}            - Jugadores de un equipo específico
  GET  /championship              - Información del campeonato
  GET  /market                    - Mercado de jugadores
  GET  /market/mine               - Mis jugadores en el mercado
  POST /market/sell               - Poner jugador a la venta
  DELETE /market/sell/{player_id} - Retirar jugador del mercado
  PATCH /market/hide/{player_id}  - Ocultar/mostrar jugador en mercado
  POST /market/bid                - Realizar una puja
  POST /market/clause/{player_id} - Pagar cláusula de un jugador
  GET  /player/{player_id}        - Datos de un jugador
  GET  /pressroom                 - Sala de prensa del equipo
  GET  /budget                    - Saldo y límite salarial del equipo
  GET  /strategy                  - Recomendaciones: vender, comprar, robar
  GET  /strategy/speculate        - Oportunidades de especulación en el mercado
  GET  /strategy/lineup           - XI óptimo para máximos puntos por jornada
  GET  /strategy/star             - Identificar estrella del equipo y estrellas fichables en rivales
  POST /auto/run                  - Piloto automático: vende, especula y roba sin intervención
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from futmondo_client import FutmondoAuth, FutmondoClient


# ---------------------------------------------------------------------------
# Constantes globales
# ---------------------------------------------------------------------------

LALIGA_TOTAL_JORNADAS = 38
STARTING_BUDGET       = 200_000_000
MONEY_PER_POINT       = 150_000
TARGET_PTS_JORNADA    = 90     # objetivo mínimo: 90 pts del XI por jornada
MIN_AVG_PER_PLAYER    = round(TARGET_PTS_JORNADA / 11, 2)   # ≈ 8.18 pts/j
MAX_ROSTER_SIZE       = 16


# ---------------------------------------------------------------------------
# Lifespan & dependency
# ---------------------------------------------------------------------------

_client: FutmondoClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _autogestione_task
    try:
        _client = FutmondoClient()
    except ValueError as exc:
        raise RuntimeError(f"Error de configuración: {exc}") from exc

    # Auto-arrancar la autogestión si la config persistida dice enabled=True
    saved_cfg = _ag_load_config()
    if saved_cfg.enabled and _client:
        _autogestione_task = asyncio.create_task(_autogestione_loop(_client, saved_cfg))

    yield

    # Apagar la autogestión limpiamente
    if _autogestione_task and not _autogestione_task.done():
        _autogestione_task.cancel()
        try:
            await _autogestione_task
        except asyncio.CancelledError:
            pass

    if _client:
        await _client.close()


def get_client() -> FutmondoClient:
    if _client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cliente Futmondo no inicializado",
        )
    return _client


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Futmondo Team Manager API",
    description="API REST para gestionar tu equipo en Futmondo.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    mail: str = Field(..., description="Email de tu cuenta Futmondo")
    pwd: str = Field(..., description="Contraseña de tu cuenta Futmondo")


class SellPlayerRequest(BaseModel):
    player_id:   str = Field(..., description="ID del jugador")
    player_slug: str = Field(..., description="Slug numérico del jugador (campo 'slug' del roster)")
    # price se calcula automáticamente: valor_mercado × 1.5 (regla de la liga)
    # Si se envía, debe ser >= valor × 1.5, de lo contrario se devuelve error 422.


class BidRequest(BaseModel):
    player_id: str = Field(..., description="ID del jugador")
    player_slug: str = Field(..., description="Slug numérico del jugador")
    price: int = Field(..., gt=0, description="Importe de la puja en monedas")


class ModifyBidRequest(BaseModel):
    bid_id: str = Field(..., description="ID de la puja existente (campo 'bid.id' del mercado)")
    player_id: str = Field(..., description="ID del jugador")
    player_slug: str = Field(..., description="Slug numérico del jugador")
    price: int = Field(..., gt=0, description="Nuevo importe de la puja")


class PayClauseRequest(BaseModel):
    player_slug: str = Field(..., description="Slug del jugador (campo 'slug' del roster del equipo rival)")
    price: int = Field(..., gt=0, description="Precio exacto de la cláusula (campo clause.price del jugador)")


class AutoPlayRequest(BaseModel):
    dry_run: bool = Field(True, description="Solo planifica, no ejecuta. Pon False para ejecutar de verdad.")
    # Ventas — desactivadas por defecto: esperamos a que nos roben
    sell_bottom_pct: float = Field(0.0, ge=0.0, le=1.0,
        description="Vende el X% inferior de la plantilla (0.0 = no vender, estrategia de esperar robo)")
    sell_min_avg: float = Field(0.0, ge=0.0,
        description="Protege jugadores con avg_per_game ≥ este valor. 0 = sin protección")
    sell_markup: float = Field(0.10, ge=0.0, le=0.5,
        description="Precio de venta = valor × (1 + markup), cap automático en valor×1.5 (regla Futmondo)")
    # Fichajes por cláusula
    steal_top: int = Field(3, ge=0, le=5,
        description="Máximo de fichajes por cláusula a ejecutar")
    steal_max_clause: float = Field(150_000_000, ge=0,
        description="Presupuesto máximo por cláusula (0 = sin límite)")
    steal_min_avg: float = Field(MIN_AVG_PER_PLAYER, ge=0.0,
        description="Media mínima del objetivo para ficharlo (default: 90pts/11 jugadores)")
    prefer_gk: bool = Field(True,
        description="Priorizar robo de portero único rival (máximo daño competitivo)")
    # Alineación
    target_jornada: float = Field(180.0, ge=1.0,
        description="Objetivo de pts/jornada para el análisis de plantilla y gap")


class AutoRunRequest(BaseModel):
    dry_run: bool = Field(
        True,
        description=(
            "Si True (por defecto) solo simula y muestra el plan sin ejecutar nada. "
            "Pon False para ejecutar de verdad."
        ),
    )
    sell_bottom_pct: float = Field(
        0.0, ge=0.0, le=1.0,
        description="Vende el X% inferior de tu plantilla por eficiencia (0.0 = no vender, estrategia de esperar robo)",
    )
    buy_min_profit: float = Field(
        0.10, ge=0.0, le=10.0,
        description="Mínimo de descuento para comprar un jugador del mercado (0.10 = 10% bajo su valor real)",
    )
    buy_top: int = Field(5, ge=0, le=20, description="Máximo de jugadores a comprar del mercado")
    steal_top: int = Field(3, ge=0, le=10, description="Máximo de jugadores a robar por cláusula")
    steal_min_efficiency: float = Field(
        0.0, ge=0.0,
        description="Eficiencia mínima (avg_pts/jornada por millón) — ignorar si steal_min_avg > 0",
    )
    steal_min_avg: float = Field(
        MIN_AVG_PER_PLAYER, ge=0.0,
        description="Sólo robar jugadores con avg_per_game >= este valor (default: 90pts/11 jugadores ≈ 8.18)",
    )
    steal_max_clause: float = Field(
        0.0, ge=0.0,
        description="Coste máximo de cláusula a pagar (0 = sin límite). Útil para controlar el gasto",
    )
    max_teams_scan: int = Field(8, ge=1, le=20, description="Equipos rivales a escanear en busca de objetivos")
    sell_min_avg: float = Field(
        0.0, ge=0.0,
        description=(
            "Protege jugadores: no vender si su avg_per_game >= este valor. "
            "0 = sin protección. Ejemplo: 7.0 = no vender si hace ≥7 pts/jornada"
        ),
    )
    target_jornada: float = Field(
        TARGET_PTS_JORNADA, ge=1.0,
        description="Objetivo de puntos totales del XI por jornada.",
    )


# ---------------------------------------------------------------------------
# Handlers de error comunes
# ---------------------------------------------------------------------------

def _handle_error(exc: Exception) -> None:
    if isinstance(exc, httpx.HTTPStatusError):
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Error de Futmondo: {exc.response.text}",
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Error comunicando con Futmondo: {exc}",
    )


# ---------------------------------------------------------------------------
# Auth / Login
# ---------------------------------------------------------------------------

@app.post("/login", tags=["Auth"])
async def login(body: LoginRequest):
    """
    Autentica con email y contraseña y devuelve el token y userid.

    **Nota:** Solo funciona para cuentas con email/contraseña directo.
    Si tu cuenta usa Microsoft, Google o Facebook OAuth, debes obtener
    el token manualmente desde el navegador (ver README).
    """
    auth = FutmondoAuth()
    try:
        result = await auth.login(body.mail, body.pwd)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except Exception as exc:
        _handle_error(exc)
    finally:
        await auth.close()


# ---------------------------------------------------------------------------
# Equipo
# ---------------------------------------------------------------------------

@app.get("/team", tags=["Equipo"])
async def get_team(client: FutmondoClient = Depends(get_client)):
    """Devuelve los jugadores de tu equipo."""
    try:
        return await client.get_team_players()
    except Exception as exc:
        _handle_error(exc)


@app.get("/team/{team_id}", tags=["Equipo"])
async def get_team_by_id(team_id: str, client: FutmondoClient = Depends(get_client)):
    """Devuelve los jugadores de un equipo específico por su ID."""
    try:
        return await client.get_team_players(team_id=team_id)
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Campeonato
# ---------------------------------------------------------------------------

@app.get("/championship", tags=["Campeonato"])
async def get_championship(client: FutmondoClient = Depends(get_client)):
    """Devuelve información del campeonato y todos sus equipos."""
    try:
        return await client.get_championship_info()
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Mercado
# ---------------------------------------------------------------------------

@app.get("/market", tags=["Mercado"])
async def get_market(client: FutmondoClient = Depends(get_client)):
    """Devuelve los jugadores disponibles en el mercado."""
    try:
        return await client.get_market()
    except Exception as exc:
        _handle_error(exc)


@app.get("/market/mine", tags=["Mercado"])
async def get_my_market_players(client: FutmondoClient = Depends(get_client)):
    """Devuelve tus jugadores listados en el mercado."""
    try:
        return await client.get_my_players_in_market()
    except Exception as exc:
        _handle_error(exc)


@app.post("/market/sell", status_code=status.HTTP_201_CREATED, tags=["Mercado"])
async def sell_player(
    body: SellPlayerRequest, client: FutmondoClient = Depends(get_client)
):
    """
    Pone un jugador a la venta aplicando la regla de la liga:
    **precio = valor_mercado × 1.5** (nunca por debajo del precio de compra).
    La pestaña de cláusula se activa siempre (`isClause=True`).
    """
    # Obtener valor actual del jugador desde el roster
    try:
        team_raw = await client.get_team_players()
    except Exception as exc:
        _handle_error(exc)

    player = next(
        (p for p in _extract_list(team_raw)
         if _get_field(p, "_id", "id") == body.player_id
         or str(p.get("slug")) == body.player_slug),
        None,
    )
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jugador no encontrado en tu plantilla",
        )

    value     = float(_get_field(player, "value", "marketValue") or 0)
    buy_price = float(player.get("buyPrice") or 0)
    price     = _compute_sell_price(value, buy_price)

    try:
        result = await client.set_player_in_market(body.player_id, body.player_slug, price)
        return {
            **result,
            "_pricing": {
                "market_value":  int(value),
                "buy_price":     int(buy_price),
                "applied_price": price,
                "rule":          "valor_mercado × 1.5 (regla de la liga)",
                "isClause":      True,
            },
        }
    except Exception as exc:
        _handle_error(exc)


@app.delete("/market/sell/{player_id}", tags=["Mercado"])
async def remove_from_market(
    player_id: str, client: FutmondoClient = Depends(get_client)
):
    """Retira un jugador del mercado."""
    try:
        return await client.remove_player_from_market(player_id)
    except Exception as exc:
        _handle_error(exc)


class DirectSellRequest(BaseModel):
    player_slug: str = Field(..., description="Slug del jugador (campo 'slug' del roster)")


@app.post("/team/directsell/{player_id}", tags=["Mercado"])
async def direct_sell_player(
    player_id: str,
    body: DirectSellRequest,
    client: FutmondoClient = Depends(get_client),
):
    """
    **Venta directa** — elimina al jugador de tu plantilla de forma inmediata.

    Equivalente al botón 'Venta directa' de la app. No requiere fijar precio ni
    esperar a que alguien compre: el jugador sale al instante y libera hueco.

    Usa el `player_slug` del jugador (campo `slug` del roster).
    """
    try:
        return await client.direct_sell(player_id, body.player_slug)
    except Exception as exc:
        _handle_error(exc)


@app.patch("/market/hide/{player_id}", tags=["Mercado"])
async def toggle_player_visibility(
    player_id: str, client: FutmondoClient = Depends(get_client)
):
    """Activa o desactiva la visibilidad de un jugador en el mercado."""
    try:
        return await client.hide_player_in_market(player_id)
    except Exception as exc:
        _handle_error(exc)


@app.post("/market/bid", tags=["Mercado"])
async def place_bid(body: BidRequest, client: FutmondoClient = Depends(get_client)):
    """Puja por un jugador en venta por otro equipo (traspaso/cláusula)."""
    try:
        return await client.set_bid(body.player_id, body.player_slug, body.price)
    except Exception as exc:
        _handle_error(exc)


@app.post("/market/bid/modify", tags=["Mercado"])
async def modify_bid(body: ModifyBidRequest, client: FutmondoClient = Depends(get_client)):
    """Modifica/sube una puja existente en la subasta del mercado automático."""
    try:
        return await client.modify_bid(body.bid_id, body.player_id, body.player_slug, body.price)
    except Exception as exc:
        _handle_error(exc)


@app.post("/market/clause/{player_id}", tags=["Mercado"])
async def pay_clause(
    player_id: str,
    body: PayClauseRequest,
    client: FutmondoClient = Depends(get_client),
):
    """
    Paga la cláusula de un jugador para ficharlo directamente.

    Necesitas el `player_slug` y el `price` (campo `clause.price`) del jugador,
    obtenibles desde `GET /team/{team_id}` o `GET /player/{player_id}`.
    """
    try:
        return await client.pay_player_clause(player_id, body.player_slug, body.price)
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Jugadores
# ---------------------------------------------------------------------------

@app.get("/player/{player_id}", tags=["Jugadores"])
async def get_player(player_id: str, client: FutmondoClient = Depends(get_client)):
    """Devuelve los datos detallados de un jugador."""
    try:
        return await client.get_player_data(player_id)
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Sala de prensa
# ---------------------------------------------------------------------------

@app.get("/pressroom", tags=["Sala de Prensa"])
async def get_pressroom(client: FutmondoClient = Depends(get_client)):
    """Devuelve las noticias y novedades del equipo en la sala de prensa."""
    try:
        return await client.get_pressroom()
    except Exception as exc:
        _handle_error(exc)


@app.get("/fichajes/hoy", tags=["Fichajes"])
async def get_fichajes_hoy(client: FutmondoClient = Depends(get_client)):
    """
    **Fichajes de hoy** — traspasos registrados en la sala de prensa durante el día de hoy.

    Filtra las noticias del pressroom por la fecha actual (mismo patrón que
    `getTodayTransfers` del Telegram bot de referencia).
    """
    try:
        raw = await client.get_pressroom()
    except Exception as exc:
        _handle_error(exc)

    today = datetime.now(timezone.utc).date().isoformat()  # "YYYY-MM-DD"

    # Extraer lista de noticias de la respuesta
    news: list = []
    if isinstance(raw, list):
        news = raw
    elif isinstance(raw, dict):
        ans = raw.get("answer", raw)
        if isinstance(ans, list):
            news = ans
        elif isinstance(ans, dict):
            for v in ans.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    news = v
                    break

    today_items = []
    for item in news:
        item_date = item.get("date") or item.get("createdAt") or item.get("timestamp") or ""
        if isinstance(item_date, (int, float)):
            from datetime import datetime as dt_cls
            item_date = dt_cls.fromtimestamp(item_date / 1000, tz=timezone.utc).date().isoformat()
        if str(item_date).startswith(today):
            today_items.append(item)

    return {
        "date":  today,
        "total": len(today_items),
        "items": today_items,
    }


# ---------------------------------------------------------------------------
# Finanzas
# ---------------------------------------------------------------------------

@app.get("/budget", tags=["Finanzas"])
async def get_budget(client: FutmondoClient = Depends(get_client)):
    """
    Devuelve el saldo disponible, límite salarial y estado financiero de tu equipo.

    Úsalo para saber cuánto dinero tienes para fichar y cuánto margen salarial te queda.
    """
    try:
        return await client.get_userteam_info()
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Estrategia
# ---------------------------------------------------------------------------

def _extract_list(data: dict | list) -> list[dict]:
    """Extrae la lista principal de jugadores/equipos de la respuesta de Futmondo."""
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    if "answer" in data:
        data = data["answer"]
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    # Busca la primera lista de dicts con campos típicos de jugador
    for val in data.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            if any(f in val[0] for f in ["_id", "id", "name", "slug", "value", "clause"]):
                return val
    return []


def _get_field(player: dict, *keys):
    """Devuelve el primer valor no-None de los campos dados."""
    for k in keys:
        v = player.get(k)
        if v is not None:
            return v
    return None


def _clause_price(player: dict) -> float:
    """Extrae el precio de cláusula correctamente (puede ser dict o número)."""
    clause = player.get("clause")
    if isinstance(clause, dict):
        return float(clause.get("price") or clause.get("suggestedClause") or 0)
    if clause is not None:
        return float(clause)
    return 0.0


def _avg_per_game(player: dict) -> float:
    """Puntos medios por jornada (campo average.average si existe, si no calcula)."""
    avg = (player.get("average") or {}).get("average")
    if avg is not None:
        return round(float(avg), 4)
    pts = float(_get_field(player, "points", "score", "avgScore", "totalPoints") or 0)
    matches = int((player.get("average") or {}).get("matches") or 0)
    return round(pts / matches, 4) if matches else 0.0


def _last5_avg(player: dict) -> float:
    """Promedio últimas 5 jornadas (racha). Si no hay datos, usa avg de temporada."""
    last5 = (player.get("average") or {}).get("averageLastFive")
    if last5 is not None:
        return round(float(last5), 4)
    return _avg_per_game(player)


def _lineup_score(player: dict) -> float:
    """Puntuación para decidir titularidad: 70% racha last5 + 30% media temporada."""
    last5 = _last5_avg(player)
    season = _avg_per_game(player)
    return round(0.7 * last5 + 0.3 * season, 4)


def _efficiency(player: dict, cost_key: str) -> float:
    """Puntos medios por jornada por millón de coste. Cuanto mayor, mejor valor."""
    score = _avg_per_game(player)
    if cost_key == "clause":
        cost = _clause_price(player)
    else:
        raw = _get_field(player, cost_key, "value", "marketValue") or 0
        cost = float(raw) if not isinstance(raw, dict) else 0.0
    if not score or not cost:
        return 0.0
    return round(score / (cost / 1_000_000), 4)


def _player_summary(player: dict, action: str) -> dict:
    """Genera un resumen con los campos clave del jugador."""
    buy_price = float(player.get("buyPrice") or 0)
    result = {
        "id": _get_field(player, "_id", "id"),
        "slug": player.get("slug"),
        "name": _get_field(player, "name", "playerName", "player_name"),
        "position": _get_field(player, "role", "position", "pos"),
        "score": _get_field(player, "points", "score", "avg_score", "avgScore"),
        "avg_per_game": (player.get("average") or {}).get("average"),
        "value": _get_field(player, "value", "marketValue", "market_value"),
        "buy_price": buy_price if buy_price else None,
        "team": _get_field(player, "team", "teamName", "team_name"),
        "efficiency": _efficiency(
            player,
            "price" if action == "buy" else ("clause" if action == "steal" else "value"),
        ),
        "action": action,
    }
    if action == "steal":
        result["clause_price"] = _clause_price(player)
    if action == "buy":
        result["price"] = _get_field(player, "price", "sell_price", "sellPrice")
        result["bids"] = _get_field(player, "numberOfBids", "bid", "bidPrice")
    return result


@app.get("/strategy", tags=["Estrategia"])
async def get_strategy(
    top: int = Query(10, ge=1, le=25, description="Número de recomendaciones por categoría"),
    max_teams: int = Query(8, ge=1, le=20, description="Máximo de equipos rivales a analizar para cláusulas"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Estrategia para construir el equipo ganador y maximizar tu límite.**

    Analiza en paralelo tu plantilla, el mercado y los equipos rivales y devuelve
    tres listas ordenadas por eficiencia (puntos / millón de €):

    - **sell** – Tus jugadores con peor relación puntos/valor: véndelos para liberar presupuesto.
    - **buy**  – Jugadores del mercado con mejor relación puntos/precio: ficha barato y sube el límite.
    - **steal** – Jugadores de equipos rivales con mejor relación puntos/cláusula: róbalos pagando la cláusula.

    Usa `POST /market/sell` para poner a la venta, `POST /market/bid` para pujar en el mercado
    y `POST /market/clause/{player_id}` para ejecutar un robo por cláusula.
    """
    try:
        # Datos base en paralelo
        team_data, market_data, championship_data = await asyncio.gather(
            client.get_team_players(),
            client.get_market(),
            client.get_championship_info(),
        )

        my_players = _extract_list(team_data)
        my_ids = {_get_field(p, "_id", "id") for p in my_players if _get_field(p, "_id", "id")}
        market_players = _extract_list(market_data)

        # Obtener IDs de equipos rivales
        all_teams = _extract_list(championship_data)
        rival_ids = [
            tid for t in all_teams
            if (tid := _get_field(t, "_id", "id", "userteamId")) and tid != client.user_team_id
        ][:max_teams]

        # Rosters rivales en paralelo (ignorar errores individuales)
        rival_results = await asyncio.gather(
            *[client.get_team_players(team_id=tid) for tid in rival_ids],
            return_exceptions=True,
        )

        steal_candidates: list[dict] = []
        for res in rival_results:
            if isinstance(res, Exception):
                continue
            for p in _extract_list(res):
                pid = _get_field(p, "_id", "id")
                if pid and pid not in my_ids:
                    steal_candidates.append(p)

        # Ordenar y recortar
        sell = sorted(my_players, key=lambda p: _efficiency(p, "value"))[:top]
        buy = sorted(market_players, key=lambda p: _efficiency(p, "price"), reverse=True)[:top]
        steal = sorted(steal_candidates, key=lambda p: _efficiency(p, "clause"), reverse=True)[:top]

        return {
            "summary": {
                "my_players": len(my_players),
                "market_players": len(market_players),
                "rival_players_scanned": len(steal_candidates),
            },
            "sell": [_player_summary(p, "sell") for p in sell],
            "buy": [_player_summary(p, "buy") for p in buy],
            "steal": [_player_summary(p, "steal") for p in steal],
        }
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Especulación y alineación óptima
# ---------------------------------------------------------------------------

# Formaciones habituales: {DEF, MID, FWD} – siempre 1 portero
_FORMATIONS = [
    {"name": "4-3-3", "DEF": 4, "MID": 3, "FWD": 3},
    {"name": "4-4-2", "DEF": 4, "MID": 4, "FWD": 2},
    {"name": "4-5-1", "DEF": 4, "MID": 5, "FWD": 1},
    {"name": "3-5-2", "DEF": 3, "MID": 5, "FWD": 2},
    {"name": "3-4-3", "DEF": 3, "MID": 4, "FWD": 3},
    {"name": "5-3-2", "DEF": 5, "MID": 3, "FWD": 2},
    {"name": "5-4-1", "DEF": 5, "MID": 4, "FWD": 1},
]

_POS_MAP = {
    # Portero (campo 'role' en Futmondo)
    "portero": "GK", "gk": "GK", "por": "GK", "goalkeeper": "GK", "1": "GK",
    # Defensa
    "defensa": "DEF", "def": "DEF", "defender": "DEF", "2": "DEF",
    # Centrocampista
    "centrocampista": "MID", "mid": "MID", "cen": "MID", "midfielder": "MID", "3": "MID",
    # Delantero
    "delantero": "FWD", "fwd": "FWD", "del": "FWD", "forward": "FWD", "4": "FWD",
}


def _normalize_pos(player: dict) -> str:
    raw = _get_field(player, "role", "role2", "position", "pos") or ""
    return _POS_MAP.get(str(raw).lower(), str(raw).upper() or "UNK")


def _primary_pos(player: dict) -> str:
    raw = str(_get_field(player, "role", "position", "pos") or "").lower()
    return _POS_MAP.get(raw, "")


def _secondary_pos(player: dict) -> str:
    raw = str(player.get("role2") or "").strip().lower()
    return _POS_MAP.get(raw, "") if raw else ""


def _best_lineup_analysis(players: list[dict], target_apg: float = 150.0) -> dict:
    """
    Prueba todas las formaciones usando rol principal y secundario (role2).
    Rankea por avg_per_game y devuelve progreso hacia el objetivo de puntos/jornada.
    Solo coloca como titulares a jugadores disponibles (_is_available).
    Los lesionados/sancionados van directamente al banquillo.
    """
    available = [p for p in players if _is_available(p)]
    unavailable = [p for p in players if not _is_available(p)]

    # Si no hay suficientes jugadores disponibles, admitir los del mercado como
    # última opción para evitar slots vacíos (un -5 fijo es peor que cualquier pts ≥ 0)
    if len(available) < 11:
        market_fallback = [
            p for p in unavailable
            if isinstance(p.get("market"), dict) and p.get("market", {}).get("inMarket")
            and _avg_per_game(p) > 0   # descartar porteros/jugadores sin media
        ]
        market_fallback.sort(key=_lineup_score, reverse=True)
        slots_needed = 11 - len(available)
        available = available + market_fallback[:slots_needed]
        unavailable = [p for p in unavailable if p not in available]

    best: dict | None = None
    best_total = -1.0

    for f in _FORMATIONS:
        needed = {"GK": 1, "DEF": f["DEF"], "MID": f["MID"], "FWD": f["FWD"]}

        # Paso 1: llenar con rol principal (solo disponibles)
        used: set[str] = set()
        assignment: dict[str, list[dict]] = {pos: [] for pos in needed}

        primary_pools = {
            pos: sorted(
                [p for p in available if _primary_pos(p) == pos],
                key=_lineup_score, reverse=True,
            )
            for pos in needed
        }
        for pos, n in needed.items():
            for p in primary_pools[pos]:
                if len(assignment[pos]) >= n:
                    break
                assignment[pos].append(p)
                used.add(_get_field(p, "_id", "id"))

        # Paso 2: rellenar huecos con role2 (solo disponibles)
        for pos, n in needed.items():
            if len(assignment[pos]) >= n:
                continue
            r2_pool = sorted(
                [p for p in available
                 if _get_field(p, "_id", "id") not in used and _secondary_pos(p) == pos],
                key=_lineup_score, reverse=True,
            )
            for p in r2_pool:
                if len(assignment[pos]) >= n:
                    break
                assignment[pos].append(p)
                used.add(_get_field(p, "_id", "id"))

        # Comprobar formación completa
        if any(len(assignment[pos]) < needed[pos] for pos in needed):
            continue

        total_apg = sum(
            _avg_per_game(p)
            for pl in assignment.values()
            for p in pl
        )
        if total_apg > best_total:
            best_total = total_apg
            starters = []
            for pos, pl in assignment.items():
                for p in pl:
                    summary = _player_summary(p, "lineup")
                    summary["assigned_pos"] = pos
                    summary["avg_per_game"] = _avg_per_game(p)
                    summary["last5_avg"] = _last5_avg(p)
                    summary["lineup_score"] = _lineup_score(p)
                    summary["available"] = True
                    summary["availability"] = _availability_label(p)
                    if _primary_pos(p) != pos:
                        summary["playing_as_role2"] = True
                    starters.append(summary)
            best = {
                "formation": f["name"],
                "projected_pts_jornada": round(total_apg, 2),
                "target_pts_jornada": target_apg,
                "gap_to_target": round(target_apg - total_apg, 2),
                "starters": starters,
            }

    if best is None:
        # Fallback: los 11 mejores disponibles por racha
        top11 = sorted(available, key=_lineup_score, reverse=True)[:11]
        total_apg = sum(_avg_per_game(p) for p in top11)
        best = {
            "formation": "libre",
            "projected_pts_jornada": round(total_apg, 2),
            "target_pts_jornada": target_apg,
            "gap_to_target": round(target_apg - total_apg, 2),
            "starters": [
                {**_player_summary(p, "lineup"), "avg_per_game": _avg_per_game(p),
                 "available": True, "availability": _availability_label(p)}
                for p in top11
            ],
        }

    # Suplentes: disponibles no titulares + NO disponibles (con etiqueta de estado)
    starter_ids = {s["id"] for s in best["starters"]}
    bench_available = sorted(
        [p for p in available if _get_field(p, "_id", "id") not in starter_ids],
        key=_avg_per_game, reverse=True,
    )
    bench_unavailable = sorted(unavailable, key=_avg_per_game, reverse=True)

    bench_entries = []
    for p in bench_available:
        entry = _player_summary(p, "lineup")
        entry["avg_per_game"] = _avg_per_game(p)
        entry["available"] = True
        entry["availability"] = _availability_label(p)
        bench_entries.append(entry)
    for p in bench_unavailable:
        entry = _player_summary(p, "lineup")
        entry["avg_per_game"] = _avg_per_game(p)
        entry["available"] = False
        entry["availability"] = _availability_label(p)
        bench_entries.append(entry)

    best["bench"] = bench_entries
    best["unavailable_count"] = len(unavailable)
    best["unavailable"] = [
        {"name": p.get("name"), "role": p.get("role"), "status": _availability_label(p)}
        for p in unavailable
    ]
    return best


@app.get("/strategy/speculate", tags=["Estrategia"])
async def speculate(
    top: int = Query(15, ge=1, le=50, description="Número de oportunidades a devolver"),
    min_discount: float = Query(0.05, ge=0.0, le=1.0, description="Descuento mínimo sobre el valor real (0.05 = 5%)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Especulación: compra barato y vende caro para ganar dinero.**

    Escanea el mercado y devuelve los jugadores que cotizan POR DEBAJO de su
    valor real (cláusula o valor de mercado). Cuanto mayor sea `profit_ratio`,
    mayor es el margen de beneficio potencial.

    Estrategia:
    1. Compra los jugadores de esta lista (`POST /market/bid` o `POST /market/clause/{id}`).
    2. Espera a que su valor suba (buen rendimiento en jornadas).
    3. Véndelos en el mercado (`POST /market/sell`) por encima del precio de compra.

    Campos de la respuesta:
    - `price`: precio de compra en el mercado ahora mismo
    - `real_value`: valor / cláusula real del jugador
    - `profit_absolute`: beneficio bruto estimado (real_value - price)
    - `profit_ratio`: rentabilidad porcentual ((real_value - price) / price)
    """
    try:
        market_data = await client.get_market()
        players = _extract_list(market_data)

        deals = []
        for p in players:
            price = float(_get_field(p, "price", "sell_price", "sellPrice") or 0)
            real_value = float(_get_field(p, "value", "marketValue", "clause") or 0)
            if price <= 0 or real_value <= price:
                continue
            ratio = (real_value - price) / price
            if ratio < min_discount:
                continue
            deals.append({
                "id": _get_field(p, "_id", "id"),
                "slug": p.get("slug"),
                "name": _get_field(p, "name", "playerName", "player_name"),
                "position": _get_field(p, "role", "position", "pos"),
                "score": _get_field(p, "points", "score", "avg_score", "avgScore"),
                "avg_per_game": (p.get("average") or {}).get("average"),
                "price": int(price),
                "real_value": int(real_value),
                "profit_absolute": int(real_value - price),
                "profit_ratio": round(ratio, 4),
                "team": _get_field(p, "team", "teamName", "team_name"),
                "bids": _get_field(p, "numberOfBids", "bid"),
            })

        deals.sort(key=lambda d: d["profit_ratio"], reverse=True)
        return {
            "total_opportunities": len(deals),
            "shown": min(top, len(deals)),
            "deals": deals[:top],
        }
    except Exception as exc:
        _handle_error(exc)


@app.get("/strategy/star", tags=["Estrategia"])
async def strategy_star(
    scan_rivals: bool = Query(True, description="Escanear equipos rivales en busca de estrellas fichables"),
    star_max_clause: float = Query(0.0, ge=0.0, description="Presupuesto máximo para una estrella rival (0 = sin límite)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Estrategia de equipo centrado en una estrella.**

    Devuelve:
    - `my_star`: el jugador más valioso de tu equipo actual (protegido, nunca vender)
    - `my_squad_value`: valor total de la plantilla
    - `star_pct_of_squad`: qué % del valor total representa la estrella
    - `rival_star_targets`: estrellas fichables en equipos rivales, ordenadas por valor de mercado
    - `recommendation`: consejo de acción (mantener, buscar estrella más cara, etc.)
    """
    try:
        team_data  = await client.get_team_players()
        my_players = _extract_list(team_data)
        if not my_players:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No hay jugadores en el equipo")

        # Identificar la estrella actual (jugador más caro del equipo)
        my_players_sorted = sorted(
            my_players,
            key=lambda p: float(_get_field(p, "value", "marketValue") or 0),
            reverse=True,
        )
        my_star   = my_players_sorted[0]
        star_value = float(_get_field(my_star, "value", "marketValue") or 0)
        squad_value = sum(float(_get_field(p, "value", "marketValue") or 0) for p in my_players)
        star_pct    = round(star_value / squad_value * 100, 1) if squad_value > 0 else 0.0

        result: dict = {
            "my_star": {
                "id":       _get_field(my_star, "_id", "id"),
                "name":     my_star.get("name", "?"),
                "role":     my_star.get("role", "?"),
                "value":    int(star_value),
                "avg_per_game": round(_avg_per_game(my_star), 2),
                "protected": True,
            },
            "my_squad_value":    int(squad_value),
            "star_pct_of_squad": star_pct,
            "rival_star_targets": [],
            "recommendation": "",
        }

        # Consejo básico según concentración de valor
        if star_pct < 20:
            result["recommendation"] = (
                "Tu plantilla es muy equilibrada — considera invertir más en una sola estrella de alto impacto."
            )
        elif star_pct > 50:
            result["recommendation"] = (
                f"{my_star.get('name','?')} ya domina el equipo ({star_pct}% del valor). "
                "Mantén a tu estrella y complementa con jugadores eficientes baratos."
            )
        else:
            result["recommendation"] = (
                f"{my_star.get('name','?')} es tu estrella actual ({star_pct}% del valor). "
                "Buen equilibrio — protégela y busca upgrade si hay una estrella rival accesible."
            )

        # Escanear rivales en busca de estrellas fichables
        if scan_rivals:
            champ_raw   = await client.get_championship_info()
            inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
            rival_teams = [
                t for t in (inner.get("teams", []) if isinstance(inner, dict) else [])
                if (t.get("teamid") or t.get("id")) != client.user_team_id
            ]
            my_ids = {_get_field(p, "_id", "id") for p in my_players}

            async def _fetch_rival(t):
                tid = t.get("teamid") or t.get("id")
                try:
                    return t, _extract_list(await client.get_team_players(team_id=tid))
                except Exception:
                    return t, []

            all_rosters = await asyncio.gather(*[_fetch_rival(t) for t in rival_teams])
            star_targets = []
            for team_info, roster in all_rosters:
                team_name = team_info.get("teamname") or team_info.get("name", "?")
                if not roster:
                    continue
                # La estrella del rival = su jugador más caro
                rival_star = max(roster, key=lambda p: float(_get_field(p, "value", "marketValue") or 0))
                pid        = _get_field(rival_star, "_id", "id")
                if pid in my_ids:
                    continue
                r_value   = float(_get_field(rival_star, "value", "marketValue") or 0)
                c_price   = _clause_price(rival_star)
                cl        = rival_star.get("clause") or {}
                transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
                if transferred or c_price <= 0:
                    continue
                if star_max_clause > 0 and c_price > star_max_clause:
                    continue
                if not _is_available(rival_star):
                    continue
                star_targets.append({
                    "id":           pid,
                    "name":         rival_star.get("name", "?"),
                    "role":         rival_star.get("role", "?"),
                    "team":         team_name,
                    "value":        int(r_value),
                    "clause_price": int(c_price),
                    "avg_per_game": round(_avg_per_game(rival_star), 2),
                    "value_vs_my_star": round((r_value - star_value) / max(star_value, 1) * 100, 1),
                })

            # Ordenar por valor descendente
            star_targets.sort(key=lambda x: -x["value"])
            result["rival_star_targets"] = star_targets[:10]

            if star_targets and star_targets[0]["value"] > star_value * 1.15:
                best = star_targets[0]
                result["recommendation"] += (
                    f" ★ UPGRADE DISPONIBLE: {best['name']} ({best['team']}) "
                    f"vale {best['value']:,} (+{best['value_vs_my_star']}%) — cláusula: {best['clause_price']:,}."
                )

        return result

    except HTTPException:
        raise
    except Exception as exc:
        _handle_error(exc)


@app.get("/strategy/lineup", tags=["Estrategia"])
async def best_lineup(
    target: float = Query(150.0, ge=1.0, description="Objetivo de puntos del XI por jornada"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **XI óptimo para máximos puntos por jornada.**

    Analiza tu plantilla completa y prueba todas las formaciones posibles
    (4-3-3, 4-4-2, 4-5-1, 3-5-2, 3-4-3, 5-3-2, 5-4-1).
    Usa `role2` para rellenar huecos en formaciones si hay pocos efectivos en alguna posición.

    Devuelve:
    - `formation`: la formación con mayor avg_per_game total
    - `projected_pts_jornada`: puntos proyectados por jornada del XI
    - `gap_to_target`: puntos que faltan para llegar al objetivo
    - `starters`: los 11 titulares con avg_per_game y posición asignada
    - `bench`: el resto de la plantilla ordenado por rendimiento
    """
    try:
        team_data = await client.get_team_players()
        players = _extract_list(team_data)
        if not players:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontraron jugadores en el equipo")
        return _best_lineup_analysis(players, target_apg=target)
    except HTTPException:
        raise
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Piloto automático
# ---------------------------------------------------------------------------

async def _exec_action(coro, action: dict) -> None:
    """Ejecuta una corutina y escribe el resultado en el dict de acción.
    Detecta errores de negocio de Futmondo (HTTP 200 con answer.error=true)."""
    try:
        result = await coro
        action["response"] = result
        # Futmondo devuelve HTTP 200 incluso para errores de negocio
        answer = result.get("answer", {}) if isinstance(result, dict) else {}
        if isinstance(answer, dict) and answer.get("error"):
            action["status"] = "error"
            action["error"] = answer.get("code", "api.error.unknown")
        else:
            action["status"] = "ok"
    except Exception as exc:
        action["status"] = "error"
        action["error"] = str(exc)


@app.post("/auto/run", tags=["Automatización"])
async def auto_run(body: AutoRunRequest, client: FutmondoClient = Depends(get_client)):
    """
    **Piloto automático — hace todo solo.**

    Recopila datos en paralelo, calcula el plan óptimo y, si `dry_run=False`,
    ejecuta cada acción en orden: primero vende, luego compra en el mercado,
    luego roba por cláusula. Finalmente devuelve el XI óptimo actualizado.

    **Flujo automático:**
    1. Obtiene tu plantilla, el mercado y los equipos rivales.
    2. **VENDE** tus jugadores con peor eficiencia (libera presupuesto).
    3. **COMPRA** del mercado los jugadores más infravalorados (especulación).
    4. **ROBA** por cláusula los mejores jugadores de equipos rivales.
    5. Devuelve el **XI óptimo** para la próxima jornada.

    Empieza con `dry_run=true` para ver el plan antes de ejecutarlo.
    """
    try:
        # ── 1. Recopilar datos en paralelo ────────────────────────────────
        team_data, market_data, championship_data = await asyncio.gather(
            client.get_team_players(),
            client.get_market(),
            client.get_championship_info(),
        )

        my_players = _extract_list(team_data)
        my_ids = {_get_field(p, "_id", "id") for p in my_players if _get_field(p, "_id", "id")}
        market_players = _extract_list(market_data)

        # Rosters rivales en paralelo
        all_teams = _extract_list(championship_data)
        rival_ids = [
            tid for t in all_teams
            if (tid := _get_field(t, "_id", "id", "userteamId")) and tid != client.user_team_id
        ][:body.max_teams_scan]

        rival_results = await asyncio.gather(
            *[client.get_team_players(team_id=tid) for tid in rival_ids],
            return_exceptions=True,
        )
        rival_players: list[dict] = []
        for res in rival_results:
            if isinstance(res, Exception):
                continue
            for p in _extract_list(res):
                pid = _get_field(p, "_id", "id")
                if pid and pid not in my_ids:
                    rival_players.append(p)

        # ── 2. Calcular acciones de VENTA ─────────────────────────────────
        # Excluir jugadores ya en el mercado (evitar error de doble listing)
        sorted_mine = sorted(
            [p for p in my_players if not p.get("market")],
            key=lambda p: _efficiency(p, "value"),
        )
        sell_count = max(1, int(len(sorted_mine) * body.sell_bottom_pct))
        # Guardia de portero: nunca vender si es el único GK
        gk_count  = sum(1 for p in my_players if _primary_pos(p) == "GK")
        def_count = sum(1 for p in my_players if _primary_pos(p) == "DEF")
        sell_candidates = [
            p for p in sorted_mine
            if not (_primary_pos(p) == "GK"  and gk_count  <= 1)   # nunca vender último portero
            and not (_primary_pos(p) == "DEF" and def_count <= 3)   # nunca vender si quedan ≤3 defensas
            and (body.sell_min_avg == 0 or _avg_per_game(p) < body.sell_min_avg)
        ][:sell_count]

        sell_actions = []
        for p in sell_candidates:
            pid = _get_field(p, "_id", "id")
            slug = p.get("slug")
            value = float(_get_field(p, "value", "marketValue") or 0)
            buy_price = float(p.get("buyPrice") or 0)
            price = _compute_sell_price(value, buy_price)
            sell_actions.append({
                "player": _player_summary(p, "sell"),
                "list_price": price,
                "buy_price": int(buy_price) if buy_price else None,
                "market_value": int(value),
                "status": "pending",
                "error": None,
                "response": None,
            })
            sell_actions[-1]["_pid"] = pid
            sell_actions[-1]["_slug"] = slug

        # ── 3. Calcular acciones de COMPRA (especulación) ─────────────────
        buy_deals = []
        for p in market_players:
            price = float(_get_field(p, "price", "sell_price", "sellPrice") or 0)
            real_value = float(_get_field(p, "value", "marketValue", "clause") or 0)
            if price <= 0 or real_value <= price:
                continue
            ratio = (real_value - price) / price
            if ratio < body.buy_min_profit:
                continue
            buy_deals.append((ratio, p, int(price)))
        buy_deals.sort(key=lambda x: x[0], reverse=True)

        buy_actions = []
        for ratio, p, price in buy_deals[: body.buy_top]:
            pid = _get_field(p, "_id", "id")
            slug = p.get("slug")
            buy_actions.append({
                "player": _player_summary(p, "buy"),
                "bid_price": price,
                "profit_ratio": round(ratio, 4),
                "status": "pending",
                "error": None,
                "response": None,
            })
            buy_actions[-1]["_pid"] = pid
            buy_actions[-1]["_slug"] = slug

        # ── 4. Calcular acciones de ROBO por cláusula ─────────────────────
        # Filtrar por rendimiento mínimo — ignoramos eficiencia si steal_min_avg > 0
        steal_candidates = [
            p for p in rival_players
            if _clause_price(p) > 0
            and (body.steal_min_avg == 0 or _avg_per_game(p) >= body.steal_min_avg)
            and (body.steal_min_efficiency == 0 or _efficiency(p, "clause") >= body.steal_min_efficiency)
            and (body.steal_max_clause == 0 or _clause_price(p) <= body.steal_max_clause)
        ]
        # Ordenar por avg_per_game DESC: siempre robamos primero el mejor jugador
        steal_candidates.sort(key=lambda p: _avg_per_game(p), reverse=True)

        # Dejar que la API de Futmondo sea el árbitro del límite de roster
        steal_actions = []
        for p in steal_candidates[: body.steal_top]:
            pid = _get_field(p, "_id", "id")
            slug = p.get("slug")
            c_price = int(_clause_price(p))
            steal_actions.append({
                "player": _player_summary(p, "steal"),
                "status": "pending",
                "error": None,
                "response": None,
            })
            steal_actions[-1]["_pid"] = pid
            steal_actions[-1]["_slug"] = slug
            steal_actions[-1]["_price"] = c_price

        # ── 5. Ejecutar si no es simulación ───────────────────────────────
        if not body.dry_run:
            # Primero vender (libera presupuesto)
            for action in sell_actions:
                pid = action.pop("_pid", None)
                slug = action.pop("_slug", None)
                if pid and slug:
                    await _exec_action(
                        client.set_player_in_market(pid, slug, action["list_price"]),
                        action,
                    )
                else:
                    action["status"] = "error"
                    action["error"] = "player_id o slug no disponible"

            # Luego comprar
            for action in buy_actions:
                pid = action.pop("_pid", None)
                slug = action.pop("_slug", None)
                if pid and slug:
                    await _exec_action(
                        client.set_bid(pid, slug, action["bid_price"]),
                        action,
                    )
                else:
                    action["status"] = "error"
                    action["error"] = "player_id o slug no disponible"

            # Luego robar por cláusula
            for action in steal_actions:
                pid = action.pop("_pid", None)
                slug = action.pop("_slug", None)
                price = action.pop("_price", None)
                if pid and slug and price:
                    await _exec_action(client.pay_player_clause(pid, slug, price), action)
                else:
                    action["status"] = "error"
                    action["error"] = "player_id, slug o price de cláusula no disponible"
        else:
            # En dry_run limpiar los campos internos
            for action in sell_actions:
                action.pop("_pid", None); action.pop("_slug", None)
            for action in buy_actions:
                action.pop("_pid", None); action.pop("_slug", None)
            for action in steal_actions:
                action.pop("_pid", None); action.pop("_slug", None); action.pop("_price", None)

        # ── 6. XI óptimo tras los cambios ─────────────────────────────────
        lineup = _best_lineup_analysis(my_players, target_apg=body.target_jornada)

        # ── 7. Resumen ejecutivo ──────────────────────────────────────────
        estimated_income = sum(a["list_price"] for a in sell_actions)
        estimated_spend = sum(a["bid_price"] for a in buy_actions)
        steal_count_ok = sum(1 for a in steal_actions if a["status"] in ("ok", "pending"))

        projected = lineup.get("projected_pts_jornada", 0)
        players_on_market = sum(1 for p in my_players if p.get("market"))
        steals_ok  = sum(1 for a in steal_actions if a.get("status") == "ok")
        steals_err = sum(1 for a in steal_actions if a.get("status") == "error")
        return {
            "dry_run": body.dry_run,
            "roster_status": {
                "total": len(my_players),
                "on_market": players_on_market,
                "active": len(my_players) - players_on_market,
                "steals_succeeded": steals_ok,
                "steals_failed": steals_err,
            },
            "target_progress": {
                "target_pts_jornada": body.target_jornada,
                "projected_pts_jornada": projected,
                "gap": round(body.target_jornada - projected, 2),
                "pct_reached": round(projected / body.target_jornada * 100, 1),
                "avg_needed_per_player": round(body.target_jornada / 11, 2),
                "current_avg_per_player": round(projected / 11, 2),
            },
            "summary": {
                "sell": len(sell_actions),
                "buy": len(buy_actions),
                "steal": len(steal_actions),
                "estimated_income": estimated_income,
                "estimated_spend": estimated_spend,
                "net_cash_flow": estimated_income - estimated_spend,
            },
            "actions": {
                "sell": sell_actions,
                "buy": buy_actions,
                "steal": steal_actions,
            },
            "lineup": lineup,
        }
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.post("/market/reprice", tags=["Mercado"])
async def reprice_market(
    markup: float = Query(0.10, ge=0.0, le=1.0, description="Margen sobre valor de mercado (0.10 = +10%)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Re-lista todos tus jugadores en venta al precio de mercado actual + markup.**

    Útil cuando los precios están desactualizados respecto al valor real.
    Cancela el listing actual y vuelve a publicarlo al precio correcto.
    Por defecto: `valor_mercado × 1.10`.
    """
    try:
        raw = await client.get_my_players_in_market()
    except Exception as exc:
        _handle_error(exc)

    listed = _extract_list(raw)
    results = []
    for p in listed:
        pid   = _get_field(p, "_id", "id")
        slug  = p.get("slug")
        value = float(_get_field(p, "value", "marketValue") or 0)
        old_price = float(_get_field(p, "price", "sellPrice") or 0)
        new_price = max(1_000_000, _compute_sell_price(value, old_price))

        action = {"player": p.get("name", "?"), "old_price": int(old_price), "new_price": new_price, "status": "pending", "error": None}
        if pid and slug and new_price != int(old_price):
            # Retirar del mercado primero
            try:
                await client.remove_player_from_market(pid)
            except Exception:
                pass
            # Re-listar al precio correcto
            result = await client.set_player_in_market(pid, str(slug), new_price)
            ans = result.get("answer", {}) if isinstance(result, dict) else {}
            if isinstance(ans, dict) and ans.get("error"):
                action["status"] = "error"
                action["error"]  = ans.get("code", "unknown")
            else:
                action["status"] = "ok"
        else:
            action["status"] = "sin_cambios"
        results.append(action)

    return {"markup": markup, "repriced": len([r for r in results if r["status"] == "ok"]), "details": results}


@app.get("/account", tags=["Sistema"])
async def account_info(client: FutmondoClient = Depends(get_client)):
    """Devuelve información de la cuenta: saldo de coins, estadísticas y equipos."""
    try:
        return await client.get_user_info()
    except Exception as exc:
        _handle_error(exc)


@app.get("/health", tags=["Sistema"])
async def health():
    """Comprueba que la API está en funcionamiento."""
    return {"status": "ok", "service": "Futmondo Team Manager API"}


# ── Rivals intelligence ────────────────────────────────────────────────────────

@app.get("/rivals/intel", tags=["Inteligencia"])
async def rivals_intel(
    max_teams: int = Query(20, ge=1, le=50),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Inteligencia de rivales — presupuesto estimado y perfil de cada equipo.**

    Para cada equipo rival calcula:
    - **Presupuesto estimado**: inicio(200M) + puntos×150k − valor_compra_plantilla_actual
    - **Gasto total en jugadores**: suma de buyPrices de su roster
    - **Jugador más caro**: el que más dinero ha invertido en un jugador
    - **Media del XI**: para compararnos con ellos

    Permite saber quién tiene dinero para robarnos jugadores o pujar en el mercado.
    """
    try:
        champ_raw = await client.get_championship_info()
    except Exception as exc:
        _handle_error(exc)

    inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id       = client.user_team_id

    async def fetch_roster(tid: str) -> list[dict]:
        try:
            raw = await client.get_team_players(team_id=tid)
            return _extract_list(raw)
        except Exception:
            return []

    team_ids = [t.get("teamid") or t.get("id") for t in champ_teams][:max_teams]
    rosters  = await asyncio.gather(*[fetch_roster(tid) for tid in team_ids])

    result = []
    for team_info, roster in zip(champ_teams, rosters):
        tid        = team_info.get("teamid") or team_info.get("id")
        pts        = float(team_info.get("points") or 0)
        team_value = float(team_info.get("teamValue") or 0)
        is_me      = tid == my_id

        total_invested = sum(float(p.get("buyPrice") or 0) for p in roster)
        money_earned   = STARTING_BUDGET + pts * MONEY_PER_POINT
        estimated_cash = money_earned - total_invested   # aproximación

        avgs    = [_avg_per_game(p) for p in roster if _avg_per_game(p) > 0]
        top11   = sorted(avgs, reverse=True)[:11]
        xi_avg  = round(sum(top11) / len(top11), 2) if top11 else 0

        most_expensive = max(roster, key=lambda p: float(p.get("buyPrice") or 0), default=None)
        on_market_cnt  = sum(1 for p in roster if p.get("market"))

        result.append({
            "team":             team_info.get("teamname") or team_info.get("name", "?"),
            "is_me":            is_me,
            "points":           pts,
            "team_value":       int(team_value),
            "roster_size":      len(roster),
            "on_market":        on_market_cnt,
            "total_invested":   int(total_invested),
            "money_earned_est": int(money_earned),
            "cash_available_est": int(max(0, estimated_cash)),
            "xi_avg":           xi_avg,
            "most_expensive_player": {
                "name":      most_expensive.get("name") if most_expensive else None,
                "buy_price": int(float(most_expensive.get("buyPrice") or 0)) if most_expensive else 0,
                "avg":       _avg_per_game(most_expensive) if most_expensive else 0,
            } if most_expensive else None,
        })

    result.sort(key=lambda x: x["cash_available_est"], reverse=True)
    return {
        "league_config": {"starting_budget": STARTING_BUDGET, "money_per_point": MONEY_PER_POINT},
        "rivals": result,
    }


@app.get("/market/bids", tags=["Inteligencia"])
async def market_bids(
    min_avg: float = Query(0.0, description="Solo jugadores con avg ≥ este valor"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Escáner de pujas del mercado — ve quién está pujando qué.**

    Escanea todos los jugadores en el mercado y muestra las pujas activas:
    - Quién puja, cuánto y cuántas pujas hay
    - Tiempo restante del listing
    - Si el jugador nos interesa (avg alta)

    Útil para saber qué rivales tienen dinero y en qué jugadores lo gastan.
    """
    try:
        raw = await client.get_market()
    except Exception as exc:
        _handle_error(exc)

    market_players = _extract_list(raw)
    now = datetime.now(timezone.utc)

    active_auctions = []
    for p in market_players:
        avg    = _avg_per_game(p)
        if avg < min_avg:
            continue
        bids   = p.get("bids") or []
        price  = float(_get_field(p, "price", "sellPrice") or 0)
        value  = float(p.get("value") or 0)
        expiry = _parse_expiry(p)
        hours  = None
        if expiry:
            delta = expiry - now
            hours = max(0.0, delta.total_seconds() / 3600)

        bid_list = []
        if isinstance(bids, list):
            for b in bids:
                bid_list.append({
                    "bidder":  b.get("username") or b.get("user") or b.get("userId") or "?",
                    "amount":  int(float(b.get("amount") or b.get("price") or b.get("bid") or 0)),
                    "team":    b.get("teamName") or b.get("team") or "?",
                })
            bid_list.sort(key=lambda b: b["amount"], reverse=True)

        best_bid = bid_list[0]["amount"] if bid_list else 0

        active_auctions.append({
            "name":        p.get("name") or p.get("playerName", "?"),
            "role":        p.get("role") or p.get("position", "?"),
            "team_club":   p.get("team") or p.get("teamName", "?"),
            "avg_per_game": round(avg, 2),
            "list_price":  int(price),
            "market_value": int(value),
            "best_bid":    best_bid,
            "num_bids":    len(bid_list),
            "bids":        bid_list,
            "hours_left":  round(hours, 1) if hours is not None else None,
            "expiry":      expiry.isoformat() if expiry else None,
        })

    active_auctions.sort(key=lambda x: x["avg_per_game"], reverse=True)
    with_bids    = [a for a in active_auctions if a["num_bids"] > 0]
    without_bids = [a for a in active_auctions if a["num_bids"] == 0]

    return {
        "total_in_market": len(active_auctions),
        "with_active_bids":   len(with_bids),
        "without_bids":       len(without_bids),
        "auctions": active_auctions,
    }


class SnipeRequest(BaseModel):
    player_id:   str   = Field(..., description="ID del jugador a snipear")
    player_slug: str   = Field(..., description="Slug del jugador")
    max_price:   int   = Field(..., description="Precio máximo que pagamos")
    snipe_seconds: int = Field(30, ge=5, le=300,
                               description="Segundos antes de expirar en que pujamos")


@app.post("/market/snipe", tags=["Inteligencia"])
async def snipe_player(body: SnipeRequest, client: FutmondoClient = Depends(get_client)):
    """
    **Sniper de subastas — puja en el último segundo.**

    Espera hasta `snipe_seconds` antes del vencimiento del listing y entonces puja,
    evitando que los rivales puedan reaccionar y contra-pujar.

    Estrategia: pujar en los últimos 30s = el rival no tiene tiempo de subir.

    El precio ofertado es `mejor_puja_actual + 1` (mínimo para ganar) hasta `max_price`.
    Si la puja actual ya supera `max_price`, no puja.
    """
    try:
        raw = await client.get_market()
    except Exception as exc:
        _handle_error(exc)

    market_players = _extract_list(raw)
    target = next(
        (p for p in market_players
         if _get_field(p, "_id", "id") == body.player_id or str(p.get("slug")) == body.player_slug),
        None
    )

    if not target:
        raise HTTPException(status_code=404, detail="Jugador no encontrado en el mercado")

    expiry = _parse_expiry(target)
    now    = datetime.now(timezone.utc)

    if not expiry:
        raise HTTPException(status_code=400, detail="El jugador no tiene fecha de expiración")

    seconds_left = (expiry - now).total_seconds()
    if seconds_left <= 0:
        raise HTTPException(status_code=400, detail="El listing ya ha expirado")

    bids     = target.get("bids") or []
    best_bid = max((float(b.get("amount") or b.get("bid") or 0) for b in bids), default=0)
    my_bid   = int(best_bid) + 1 if best_bid > 0 else int(float(_get_field(target, "price", "sellPrice") or 0))

    if my_bid > body.max_price:
        return {
            "status":      "skip",
            "reason":      f"Puja actual {my_bid:,.0f} supera tu máximo {body.max_price:,.0f}",
            "current_bid": int(best_bid),
            "max_price":   body.max_price,
        }

    wait_seconds = max(0, seconds_left - body.snipe_seconds)

    if wait_seconds > 0:
        await asyncio.sleep(min(wait_seconds, 300))   # máximo 5 min de espera en una sola llamada

    # Puja final
    try:
        resp = await client.set_bid(body.player_id, body.player_slug, my_bid)
    except Exception as exc:
        _handle_error(exc)

    ans = resp.get("answer", {}) if isinstance(resp, dict) else {}
    if isinstance(ans, dict) and ans.get("error"):
        return {"status": "error", "error": ans.get("code"), "bid_attempted": my_bid}

    return {
        "status":        "sniped",
        "player":        target.get("name") or target.get("playerName"),
        "bid_placed":    my_bid,
        "previous_best": int(best_bid),
        "waited_seconds": int(wait_seconds),
        "expiry":        expiry.isoformat(),
    }


# ── Clause sniper ──────────────────────────────────────────────────────────────

class ClauseSniperRequest(BaseModel):
    player_id:     str       = Field(..., description="ID del jugador (campo '_id' del roster rival)")
    player_slug:   str       = Field(..., description="Slug del jugador")
    clause_price:  int       = Field(..., gt=0, description="Precio exacto de la cláusula")
    team_id:       str | None = Field(None, description="ID del equipo rival para localizar al jugador más rápido")
    snipe_seconds: int       = Field(10, ge=3, le=120,
                                     description="Segundos antes de que expire la cláusula en que disparamos (default 10)")
    max_wait:      int       = Field(300, ge=5, le=3600,
                                     description="Máximo de segundos que este endpoint esperará antes de disparar. "
                                                 "Si la cláusula expira más tarde, devuelve 'too_early' con el tiempo restante.")
    dry_run:       bool      = Field(False, description="Si True planifica pero NO paga la cláusula")


@app.post("/market/clause-snipe", tags=["Inteligencia"])
async def clause_snipe(body: ClauseSniperRequest, client: FutmondoClient = Depends(get_client)):
    """
    **Sniper de cláusulas — roba en el último segundo antes de que expire.**

    Cuando una cláusula rival está a punto de caducar el rival ya no tiene tiempo
    de renovarla ni de reaccionar. Este endpoint espera hasta `snipe_seconds`
    antes de la expiración y dispara `pay_player_clause` en ese instante.

    **Flujo:**
    1. Obtiene la fecha de expiración de la cláusula del jugador.
    2. Calcula cuántos segundos faltan y espera hasta el momento exacto.
    3. Si faltan más segundos que `max_wait` devuelve `too_early` con el tiempo
       restante para que puedas relanzar el endpoint en el momento adecuado.
    4. Dispara la cláusula en el instante preciso.

    **Tip:** lanza este endpoint cuando queden `max_wait` + unos segundos de margen.
    Con `max_wait=300` (default) puedes llamarlo hasta 5 min antes y espera solo.
    """
    now = datetime.now(timezone.utc)

    # ── 1. Localizar al jugador y obtener la expiry de su cláusula ────────────
    expiry: datetime | None = None
    player_name: str = body.player_id  # fallback

    # Primero intento con get_player_data (más rápido, no requiere team_id)
    try:
        pdata = await client.get_player_data(body.player_id)
        candidate = pdata
        # La respuesta puede venir envuelta en 'answer'
        if isinstance(pdata, dict) and "answer" in pdata:
            ans = pdata["answer"]
            candidate = ans if isinstance(ans, dict) else pdata
        expiry = _clause_expiry(candidate)
        player_name = candidate.get("name") or body.player_id
    except Exception:
        pass

    # Si no tenemos expiry, buscar en el roster del equipo rival
    if expiry is None and body.team_id:
        try:
            roster_raw = await client.get_team_players(team_id=body.team_id)
            for p in _extract_list(roster_raw):
                if _get_field(p, "_id", "id") == body.player_id or str(p.get("slug")) == body.player_slug:
                    expiry = _clause_expiry(p)
                    player_name = p.get("name") or body.player_id
                    break
        except Exception:
            pass

    if expiry is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No se pudo obtener la fecha de expiración de la cláusula. "
                "Proporciona 'team_id' para buscar en el roster del equipo rival."
            ),
        )

    seconds_left = (expiry - now).total_seconds()

    if seconds_left <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"La cláusula de {player_name} ya ha expirado.",
        )

    # ── 2. Calcular tiempo de espera ──────────────────────────────────────────
    # Queremos disparar cuando queden exactamente snipe_seconds
    wait_seconds = max(0.0, seconds_left - body.snipe_seconds)

    if wait_seconds > body.max_wait:
        return {
            "status":              "too_early",
            "player":              player_name,
            "clause_price":        body.clause_price,
            "clause_expires":      expiry.isoformat(),
            "seconds_until_expiry": round(seconds_left, 1),
            "seconds_until_fire":  round(wait_seconds, 1),
            "snipe_at":            (expiry - timedelta(seconds=body.snipe_seconds)).isoformat(),
            "message": (
                f"Cláusula expira en {seconds_left / 3600:.1f}h. "
                f"Relanza este endpoint en {wait_seconds - body.max_wait:.0f}s "
                f"(o cuando queden ~{body.max_wait}s para la expiración)."
            ),
        }

    if body.dry_run:
        return {
            "status":             "plan",
            "dry_run":            True,
            "player":             player_name,
            "clause_price":       body.clause_price,
            "clause_expires":     expiry.isoformat(),
            "seconds_until_fire": round(wait_seconds, 1),
            "snipe_at":           (expiry - timedelta(seconds=body.snipe_seconds)).isoformat(),
            "message":            f"Dispararía en {wait_seconds:.0f}s (dry_run activado — no se ejecutará).",
        }

    # ── 3. Esperar hasta el momento exacto ────────────────────────────────────
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    # ── 4. Disparar la cláusula ───────────────────────────────────────────────
    try:
        resp = await client.pay_player_clause(body.player_id, body.player_slug, body.clause_price)
    except Exception as exc:
        _handle_error(exc)

    ans = resp.get("answer", {}) if isinstance(resp, dict) else {}
    if isinstance(ans, dict) and ans.get("error"):
        return {
            "status":          "error",
            "player":          player_name,
            "error":           ans.get("code", "unknown"),
            "clause_price":    body.clause_price,
            "waited_seconds":  round(wait_seconds, 1),
            "fired_at":        datetime.now(timezone.utc).isoformat(),
        }

    return {
        "status":          "stolen",
        "player":          player_name,
        "clause_paid":     body.clause_price,
        "waited_seconds":  round(wait_seconds, 1),
        "fired_at":        datetime.now(timezone.utc).isoformat(),
        "clause_expired":  expiry.isoformat(),
        "seconds_before_expiry": round(body.snipe_seconds, 1),
    }


# ── Clause helpers ─────────────────────────────────────────────────────────────

def _clause_expiry(player: dict):
    cl = player.get("clause")
    if isinstance(cl, dict):
        raw = cl.get("date") or cl.get("expires")
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                return None
    return None


def _clause_hours_left(player: dict) -> float | None:
    expiry = _clause_expiry(player)
    if not expiry:
        return None
    delta = expiry - datetime.now(timezone.utc)
    return max(0.0, delta.total_seconds() / 3600)


def _steal_risk(player: dict) -> str:
    """Riesgo de que un rival nos robe este jugador."""
    clause_val = _clause_price(player)
    hours      = _clause_hours_left(player)
    avg        = _avg_per_game(player)
    if hours is None or hours <= 0:
        return "EXPIRADA"          # ya no pueden robárnoslo por esta cláusula
    if clause_val < 20_000_000 and hours < 72:
        return "CRITICO"
    if clause_val < 50_000_000 and hours < 48:
        return "ALTO"
    if clause_val < 100_000_000:
        return "MEDIO"
    return "BAJO"


@app.get("/warroom", tags=["Guerra"])
async def war_room(
    max_clause_gk:  float = Query(150_000_000, description="Cláusula máxima para robar portero rival"),
    max_clause_any: float = Query(300_000_000, description="Cláusula máxima para robar cualquier jugador"),
    min_avg:        float = Query(7.0,          description="Media mínima de objetivos"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **War Room — sala de guerra completa.**

    Combina en una sola llamada:
    1. **Estado del roster** propio (huecos disponibles, en venta, jugadores activos)
    2. **Caza de porteros**: rivales con 1 solo portero y cláusula asequible → robar = -5 pts garantizados
    3. **Rivales confiados**: sin conectar > 12h → no reaccionarán al robo
    4. **Top objetivos**: mejores jugadores robables ordenados por avg
    5. **Plan de ataque**: prioridad = (confiado + único portero + avg alta) / cláusula
    6. **Defensa propia**: nuestros jugadores más vulnerables

    Objetivo: **{TARGET_PTS_JORNADA} pts/jornada**.
    """.format(TARGET_PTS_JORNADA=TARGET_PTS_JORNADA)
    now = datetime.now(timezone.utc)

    # ── Datos propios ──────────────────────────────────────────────────────────
    my_raw = await client.get_team_players()
    my_players = _extract_list(my_raw)
    roster_size   = len(my_players)
    on_market_cnt = sum(1 for p in my_players if p.get("market"))
    active_cnt    = roster_size - on_market_cnt
    free_slots    = max(0, MAX_ROSTER_SIZE - roster_size)
    xi            = _best_lineup_analysis(my_players, TARGET_PTS_JORNADA)

    # ── Datos rivales ──────────────────────────────────────────────────────────
    champ_raw = await client.get_championship_info()
    inner     = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id = client.user_team_id

    rival_teams = [t for t in champ_teams if (t.get("teamid") or t.get("id")) != my_id]

    async def fetch(t):
        tid = t.get("teamid") or t.get("id")
        try:
            raw = await client.get_team_players(team_id=tid)
            return t, _extract_list(raw)
        except Exception:
            return t, []

    all_rosters = await asyncio.gather(*[fetch(t) for t in rival_teams])

    my_ids = {_get_field(p, "_id", "id") for p in my_players}

    gk_targets  = []   # porteros robables de equipos con 1 solo GK
    top_targets = []   # mejores jugadores por avg
    team_intel  = []   # perfil de cada rival

    for team_info, roster in all_rosters:
        tid       = team_info.get("teamid") or team_info.get("id")
        team_name = team_info.get("teamname") or team_info.get("name", "?")
        pts       = float(team_info.get("points") or 0)
        last_acc  = team_info.get("lastAccess") or ""
        hours_offline = None
        if last_acc:
            try:
                la = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
                hours_offline = (now - la).total_seconds() / 3600
            except Exception:
                pass

        confiado = (hours_offline or 0) > 12

        gks = [p for p in roster if p.get("role", "").lower() == "portero"]
        only_one_gk = len(gks) == 1

        team_intel.append({
            "team": team_name, "points": pts,
            "roster_size": len(roster), "gk_count": len(gks),
            "hours_offline": round(hours_offline, 1) if hours_offline else 0,
            "confiado": confiado,
        })

        for p in roster:
            pid     = _get_field(p, "_id", "id")
            if pid in my_ids:
                continue
            avg     = _avg_per_game(p)
            c_price = _clause_price(p)
            cl      = p.get("clause") or {}
            transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
            c_hours = _clause_hours_left(p)
            if transferred or c_price <= 0 or (c_hours is not None and c_hours <= 0):
                continue

            role = p.get("role", "").lower()
            is_gk = role == "portero"

            # Portero único → robo = -5 garantizados
            if is_gk and only_one_gk and c_price <= max_clause_gk:
                gk_targets.append({
                    "name": p.get("name"), "role": "portero",
                    "team": team_name, "team_id": tid,
                    "avg_per_game": round(avg, 2),
                    "clause_price": int(c_price),
                    "clause_hours_left": round(c_hours, 1) if c_hours else None,
                    "team_confiado": confiado,
                    "team_offline_h": round(hours_offline, 1) if hours_offline else 0,
                    "damage": "RIVAL SIN PORTERO → -5 pts + XI roto",
                    "priority": round((10 + (20 if confiado else 0)) / (c_price / 1e6), 4),
                    "id": pid, "slug": p.get("slug"),
                })

            # Mejores jugadores generales
            if avg >= min_avg and c_price <= max_clause_any:
                last5 = float((p.get("average") or {}).get("averageLastFive") or avg)
                top_targets.append({
                    "name": p.get("name"), "role": role,
                    "team": team_name, "team_id": tid,
                    "avg_per_game": round(avg, 2),
                    "last5_avg": round(last5, 2),
                    "clause_price": int(c_price),
                    "clause_hours_left": round(c_hours, 1) if c_hours else None,
                    "team_confiado": confiado,
                    "team_offline_h": round(hours_offline, 1) if hours_offline else 0,
                    "id": pid, "slug": p.get("slug"),
                })

    gk_targets.sort(key=lambda x: (-x["team_offline_h"], x["clause_price"]))
    top_targets.sort(key=lambda x: (-x["avg_per_game"], x["clause_price"]))

    # ── Defensa propia ─────────────────────────────────────────────────────────
    my_risks = []
    for p in my_players:
        c_price = _clause_price(p)
        c_hours = _clause_hours_left(p)
        if c_price > 0 and c_hours and c_hours > 0:
            my_risks.append({
                "name": p.get("name"), "role": p.get("role"),
                "avg_per_game": _avg_per_game(p),
                "clause_price": int(c_price),
                "hours_until_expiry": round(c_hours, 1),
                "risk": _steal_risk(p),
                "on_market": bool(p.get("market")),
            })
    my_risks.sort(key=lambda x: x["clause_price"])

    return {
        "target_pts_jornada": TARGET_PTS_JORNADA,
        "roster": {
            "total": roster_size, "active": active_cnt,
            "on_market": on_market_cnt, "free_slots": free_slots,
        },
        "xi": {
            "formation": xi.get("formation"),
            "projected_pts_jornada": xi.get("projected_pts_jornada"),
            "gap_to_target": round(TARGET_PTS_JORNADA - xi.get("projected_pts_jornada", 0), 1),
        },
        "attack": {
            "gk_hunter":    gk_targets[:8],
            "top_targets":  top_targets[:10],
        },
        "defense": {
            "our_risks": my_risks,
        },
        "rival_intel": sorted(team_intel, key=lambda x: -x["hours_offline"]),
    }


@app.post("/warroom/retaliate", tags=["Guerra"])
async def retaliate(
    stolen_player_name: str = Query(..., description="Nombre del jugador que nos robaron"),
    max_clause: float = Query(200_000_000, description="Máximo a pagar en represalia"),
    prefer_gk:  bool  = Query(True, description="Priorizar robar el portero del rival (máximo daño)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Retaliación automática — responde a un robo con otro robo.**

    Si un rival nos roba un jugador, este endpoint:
    1. Identifica qué equipo robó al jugador (buscando quién lo tiene ahora)
    2. Encuentra su jugador más valioso con cláusula accesible
    3. Si `prefer_gk=true` y tienen 1 solo portero → roba el portero (daño máximo)
    4. Ejecuta el robo inmediatamente

    Robar = rival pierde jugador + -5 pts en la siguiente jornada.
    """
    # Buscar quién tiene ahora el jugador robado
    champ_raw   = await client.get_championship_info()
    inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id       = client.user_team_id
    rival_teams = [t for t in champ_teams if (t.get("teamid") or t.get("id")) != my_id]

    async def fetch(t):
        tid = t.get("teamid") or t.get("id")
        try:
            raw = await client.get_team_players(team_id=tid)
            return t, _extract_list(raw)
        except Exception:
            return t, []

    all_rosters = await asyncio.gather(*[fetch(t) for t in rival_teams])

    thief_team  = None
    thief_roster = []
    for team_info, roster in all_rosters:
        for p in roster:
            if stolen_player_name.lower() in (p.get("name") or "").lower():
                thief_team   = team_info
                thief_roster = roster
                break
        if thief_team:
            break

    if not thief_team:
        return {"status": "not_found", "message": f"No se encontró a '{stolen_player_name}' en ningún equipo rival"}

    team_name = thief_team.get("teamname") or thief_team.get("name", "?")
    gks = [p for p in thief_roster if p.get("role", "").lower() == "portero"]

    # Elegir objetivo de represalia
    target_player = None

    if prefer_gk and len(gks) == 1:
        gk = gks[0]
        cp = _clause_price(gk)
        if 0 < cp <= max_clause:
            target_player = gk

    if not target_player:
        # Mejor jugador por avg con cláusula accesible
        candidates = [
            p for p in thief_roster
            if 0 < _clause_price(p) <= max_clause
            and not (p.get("clause") or {}).get("transferred", False)
        ]
        candidates.sort(key=lambda p: _avg_per_game(p), reverse=True)
        target_player = candidates[0] if candidates else None

    if not target_player:
        return {
            "status": "no_target",
            "thief": team_name,
            "message": "No hay jugadores robables con cláusula accesible en ese equipo",
        }

    pid    = _get_field(target_player, "_id", "id")
    slug   = target_player.get("slug")
    price  = int(_clause_price(target_player))
    name   = target_player.get("name")

    resp = await client.pay_player_clause(pid, str(slug), price)
    ans  = resp.get("answer", {}) if isinstance(resp, dict) else {}
    if isinstance(ans, dict) and ans.get("error"):
        return {
            "status":  "error",
            "thief":   team_name,
            "target":  name,
            "error":   ans.get("code"),
            "clause":  price,
        }

    return {
        "status":         "retaliated",
        "thief_team":     team_name,
        "stolen_from_us": stolen_player_name,
        "we_stole":       name,
        "role":           target_player.get("role"),
        "avg_per_game":   _avg_per_game(target_player),
        "clause_paid":    price,
        "damage":         "¡PORTERO ROBADO — RIVAL SIN GK → -5 pts!" if target_player.get("role","").lower() == "portero" else f"Robado {name} avg={_avg_per_game(target_player):.2f}/j",
    }


@app.get("/defense", tags=["Estrategia"])
async def defense_alert(client: FutmondoClient = Depends(get_client)):
    """
    **Alerta defensiva — jugadores nuestros en riesgo de ser robados.**

    Muestra nuestra plantilla ordenada por riesgo de robo:
    - Cláusula baja + expira pronto = CRITICO (rival puede robarlo hoy)
    - El día antes de jornada es cuando más robos ocurren

    Riesgos: CRITICO / ALTO / MEDIO / BAJO / EXPIRADA
    """
    try:
        raw = await client.get_team_players()
    except Exception as exc:
        _handle_error(exc)

    players = _extract_list(raw)
    now     = datetime.now(timezone.utc)

    risk_order = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "BAJO": 3, "EXPIRADA": 4}
    result = []
    for p in players:
        cl         = p.get("clause") or {}
        c_price    = _clause_price(p)
        c_suggest  = cl.get("suggestedClause", 0) if isinstance(cl, dict) else 0
        c_expiry   = _clause_expiry(p)
        hours_left = _clause_hours_left(p)
        risk       = _steal_risk(p)
        result.append({
            "name":           p.get("name", "?"),
            "role":           p.get("role", "?"),
            "avg_per_game":   _avg_per_game(p),
            "on_market":      bool(p.get("market")),
            "clause_price":   int(c_price),
            "clause_suggest": int(c_suggest),
            "clause_expires": c_expiry.isoformat() if c_expiry else None,
            "hours_until_expiry": round(hours_left, 1) if hours_left is not None else None,
            "risk":           risk,
            "value":          float(p.get("value") or 0),
        })

    result.sort(key=lambda x: (risk_order.get(x["risk"], 9), x["clause_price"]))

    criticos = [r for r in result if r["risk"] == "CRITICO"]
    altos    = [r for r in result if r["risk"] == "ALTO"]

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "CRITICO": len(criticos),
            "ALTO":    len(altos),
            "MEDIO":   len([r for r in result if r["risk"] == "MEDIO"]),
            "BAJO":    len([r for r in result if r["risk"] == "BAJO"]),
        },
        "alert": "¡Actúa antes de que roben tus jugadores!" if criticos or altos else "Sin amenazas inmediatas",
        "players": result,
    }


# ── Helpers de cláusula ──────────────────────────────────────────────────────

def _compute_sell_price(value: float, buy_price: float) -> int:
    """
    Precio de venta según la regla de la liga:
      precio = valor_mercado + 50%  (= valor × 1.5)
    isClause siempre activado por el cliente HTTP.
    """
    return int(value * 1.5)


def _suggested_clause(player: dict) -> float:
    """Cláusula mínima sugerida por Futmondo. Por debajo → sanción."""
    cl = player.get("clause")
    if isinstance(cl, dict):
        return float(cl.get("suggestedClause") or 0)
    return 0.0


def _clause_sanction_status(player: dict) -> str:
    """
    Devuelve el estado de la cláusula respecto a la sugerida:
      SANCION   — precio < sugerida  → el sistema sancionará al manager
      MINIMO    — precio == sugerida → en el límite justo
      OK        — precio > sugerida  → protegido
      SIN_DATO  — no hay sugerida registrada
    """
    price     = _clause_price(player)
    suggested = _suggested_clause(player)
    if price == 0:
        return "VULNERABLE"
    if suggested <= 0:
        return "SIN_DATO"
    if price < suggested:
        return "SANCION"
    if price == suggested:
        return "MINIMO"
    return "OK"


@app.get("/defense/clause-check", tags=["Estrategia"])
async def clause_check(client: FutmondoClient = Depends(get_client)):
    """
    **Auditoría de cláusulas — detecta jugadores con cláusula inferior a la mínima.**

    Futmondo sanciona al manager cuando la cláusula de un jugador está por
    debajo del valor sugerido (`suggestedClause`). Este endpoint escanea toda
    la plantilla y clasifica cada jugador:

    - **SANCION** — `clause_price < suggested_clause` → actuar ahora
    - **MINIMO**  — precio exactamente en el mínimo → en el límite
    - **OK**      — precio por encima de la sugerida → protegido
    - **VULNERABLE** — sin cláusula activa
    - **SIN_DATO** — la API no devuelve la sugerida

    Acción recomendada: retirar del mercado y re-listar con cláusula correcta.
    """
    try:
        raw = await client.get_team_players()
    except Exception as exc:
        _handle_error(exc)

    players = _extract_list(raw)
    now     = datetime.now(timezone.utc)
    result  = []

    for p in players:
        price     = _clause_price(p)
        suggested = _suggested_clause(p)
        deficit   = max(0.0, suggested - price)
        status    = _clause_sanction_status(p)
        cl        = p.get("clause") or {}
        expiry    = _clause_expiry(p)
        hours     = _clause_hours_left(p)

        result.append({
            "name":             p.get("name", "?"),
            "role":             p.get("role", "?"),
            "avg_per_game":     round(_avg_per_game(p), 2),
            "on_market":        bool(p.get("market")),
            "clause_price":     int(price),
            "suggested_clause": int(suggested),
            "deficit":          int(deficit),
            "sanction_status":  status,
            "clause_expires":   expiry.isoformat() if expiry else None,
            "hours_until_expiry": round(hours, 1) if hours is not None else None,
            "id":               _get_field(p, "_id", "id"),
            "slug":             p.get("slug"),
        })

    # Ordenar: primero los que van a ser sancionados, luego por déficit
    order = {"SANCION": 0, "VULNERABLE": 1, "MINIMO": 2, "SIN_DATO": 3, "OK": 4}
    result.sort(key=lambda x: (order.get(x["sanction_status"], 9), -x["deficit"]))

    sanctions = [r for r in result if r["sanction_status"] == "SANCION"]
    vulnerable = [r for r in result if r["sanction_status"] == "VULNERABLE"]

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "SANCION":    len(sanctions),
            "MINIMO":     len([r for r in result if r["sanction_status"] == "MINIMO"]),
            "OK":         len([r for r in result if r["sanction_status"] == "OK"]),
            "VULNERABLE": len(vulnerable),
        },
        "alert": (
            f"¡{len(sanctions)} jugador(es) con cláusula por debajo del mínimo — SANCIÓN INMINENTE!"
            if sanctions else
            "Todas las cláusulas están en orden."
        ),
        "players": result,
    }


@app.get("/attack", tags=["Estrategia"])
async def pre_jornada_attack(
    max_clause:  float = Query(300_000_000, description="Cláusula máxima que estamos dispuestos a pagar"),
    min_avg:     float = Query(7.0,         description="Media mínima del jugador objetivo"),
    hours_window: float = Query(48.0,       description="Horas antes de jornada para actuar (ventana de ataque)"),
    max_teams:   int   = Query(20, ge=1, le=50),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Plan de ataque pre-jornada — jugadores rivales para robar y dejarles con -5 pts.**

    El día antes de que empiece una jornada, robar los mejores jugadores de los rivales:
    - Ellos pierden el jugador → -5 pts esa jornada
    - Nosotros ganamos un jugador con alta media

    Ordena por impacto máximo: jugadores con **avg alta** y **cláusula asequible**.
    Filtra por ventana temporal: cláusulas que expiren en menos de `hours_window` horas
    son las que se pueden activar ahora mismo.
    """
    try:
        champ_raw = await client.get_championship_info()
    except Exception as exc:
        _handle_error(exc)

    inner = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id = client.user_team_id

    rival_ids = [
        t.get("teamid") or t.get("id")
        for t in champ_teams
        if (t.get("teamid") or t.get("id")) != my_id
    ][:max_teams]

    # Nombre del equipo por id
    team_name_by_id = {
        t.get("teamid") or t.get("id"): t.get("teamname") or t.get("name", "?")
        for t in champ_teams
    }

    async def fetch(tid):
        try:
            raw = await client.get_team_players(team_id=tid)
            return tid, _extract_list(raw)
        except Exception:
            return tid, []

    my_raw  = await client.get_team_players()
    my_ids  = {_get_field(p, "_id", "id") for p in _extract_list(my_raw)}

    all_results = await asyncio.gather(*[fetch(tid) for tid in rival_ids])

    now = datetime.now(timezone.utc)
    targets = []
    for tid, team_players in all_results:
        team_name = team_name_by_id.get(tid, "?")
        for p in team_players:
            pid = _get_field(p, "_id", "id")
            if pid in my_ids:
                continue
            avg      = _avg_per_game(p)
            c_price  = _clause_price(p)
            cl       = p.get("clause") or {}
            transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
            if transferred or c_price <= 0 or c_price > max_clause or avg < min_avg:
                continue

            hours = _clause_hours_left(p)
            c_expiry = _clause_expiry(p)

            # Impacto = cuántos puntos le quitamos al rival si lo robamos
            # (pierde avg/j × jornadas_restantes + penalización -5 por jornada inmediata)
            impact_score = avg * 2 + 5   # avg en la jornada + los -5 que le metemos

            targets.append({
                "name":              p.get("name", "?"),
                "role":              p.get("role", "?"),
                "team":              team_name,
                "avg_per_game":      round(avg, 2),
                "last5_avg":         round(float((p.get("average") or {}).get("averageLastFive") or avg), 2),
                "clause_price":      int(c_price),
                "clause_expires":    c_expiry.isoformat() if c_expiry else None,
                "hours_left":        round(hours, 1) if hours is not None else None,
                "can_steal_now":     (hours is not None and hours > 0),
                "impact_score":      round(impact_score, 2),
                "id":                pid,
                "slug":              p.get("slug"),
            })

    # Ordenar: primero los de mayor avg, luego por cláusula más baja
    targets.sort(key=lambda x: (-x["avg_per_game"], x["clause_price"]))

    # Separar los que se pueden robar ahora vs los que no
    can_now  = [t for t in targets if t["can_steal_now"]]
    cant_now = [t for t in targets if not t["can_steal_now"]]

    return {
        "generated_at":    now.isoformat(),
        "filters":         {"max_clause": max_clause, "min_avg": min_avg, "hours_window": hours_window},
        "total_targets":   len(targets),
        "stealable_now":   len(can_now),
        "attack_plan":     can_now[:15],
        "expiry_soon":     [t for t in can_now if t["hours_left"] is not None and t["hours_left"] < hours_window][:10],
    }


# ── Monitor de anomalías ────────────────────────────────────────────────────────

# Señales de uso de bot/API por rivales:
#  1. Mismo equipo con >1 puja activa en el mercado (escaneo masivo)
#  2. Jugadores de nuestra plantilla que aparecen en noticias de traspaso hoy
#  3. Cláusulas pagadas a los pocos segundos de renovarse (timing perfecto = bot)
#  4. Rivals con lastAccess muy reciente Y múltiples movimientos en poco tiempo

async def _collect_anomalies(client: FutmondoClient) -> dict:
    """Recopila señales de comportamiento sospechoso. Devuelve el informe."""
    now   = datetime.now(timezone.utc)
    today = now.date().isoformat()
    alerts: list[dict] = []

    # ── A. Cláusulas propias mal puestas (sanción inminente) ─────────────────
    try:
        raw      = await client.get_team_players()
        my_squad = _extract_list(raw)
        for p in my_squad:
            status = _clause_sanction_status(p)
            if status == "SANCION":
                price     = _clause_price(p)
                suggested = _suggested_clause(p)
                alerts.append({
                    "type":    "SANCION_CLAUSULA",
                    "level":   "CRITICO",
                    "player":  p.get("name", "?"),
                    "detail":  f"Cláusula {int(price):,} < mínima {int(suggested):,} — déficit {int(suggested-price):,}",
                    "action":  "Retirar del mercado y re-publicar con cláusula correcta",
                })
            elif status == "VULNERABLE":
                alerts.append({
                    "type":   "SIN_CLAUSULA",
                    "level":  "ALTO",
                    "player": p.get("name", "?"),
                    "detail": "Jugador sin cláusula activa — cualquier rival puede ficharlo gratis",
                    "action": "Publicar en mercado para activar la cláusula",
                })
    except Exception:
        pass

    # ── B. Fichajes de hoy en sala de prensa (nos han robado un jugador) ─────
    try:
        raw_press = await client.get_pressroom()
        news: list = []
        if isinstance(raw_press, list):
            news = raw_press
        elif isinstance(raw_press, dict):
            ans = raw_press.get("answer", raw_press)
            if isinstance(ans, list):
                news = ans
            elif isinstance(ans, dict):
                for v in ans.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        news = v
                        break

        my_names = {p.get("name", "").lower() for p in my_squad}
        for item in news:
            item_date = item.get("date") or item.get("createdAt") or item.get("timestamp") or ""
            if isinstance(item_date, (int, float)):
                item_date = datetime.fromtimestamp(item_date / 1000, tz=timezone.utc).date().isoformat()
            if not str(item_date).startswith(today):
                continue
            text = (item.get("text") or item.get("message") or item.get("body") or "").lower()
            # Buscar si alguno de nuestros jugadores aparece en la noticia
            for name in my_names:
                if name and len(name) > 3 and name in text:
                    alerts.append({
                        "type":   "JUGADOR_TRASPASADO_HOY",
                        "level":  "ALTO",
                        "player": name.title(),
                        "detail": text[:200],
                        "action": "Verificar si fue robado por cláusula",
                    })
                    break
    except Exception:
        pass

    # ── C. Equipos rivales con pujas anómalas en el mercado ──────────────────
    try:
        raw_market = await client.get_market()
        market_players = _extract_list(raw_market)

        bids_per_team: dict[str, int] = {}
        fast_bidders:  list[dict]     = []

        for p in market_players:
            bids = p.get("bids") or []
            if not isinstance(bids, list):
                continue
            for b in bids:
                team = b.get("teamName") or b.get("team") or b.get("userId") or "?"
                bids_per_team[team] = bids_per_team.get(team, 0) + 1

        # Equipos con ≥3 pujas activas simultáneas → comportamiento de bot
        for team, count in bids_per_team.items():
            if count >= 3:
                fast_bidders.append({"team": team, "active_bids": count})
                alerts.append({
                    "type":   "BOT_SOSPECHOSO",
                    "level":  "MEDIO",
                    "player": None,
                    "detail": f"Equipo '{team}' tiene {count} pujas activas simultáneas en el mercado (posible bot/API)",
                    "action": "Ignorar sus ofertas — probablemente automatizado",
                })
    except Exception:
        pass

    # ── D. Rivales confiados con jugadores robables críticos ─────────────────
    try:
        champ_raw   = await client.get_championship_info()
        inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
        champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
        for t in champ_teams:
            if (t.get("teamid") or t.get("id")) == client.user_team_id:
                continue
            last_acc = t.get("lastAccess") or ""
            if not last_acc:
                continue
            try:
                la = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
                hours_offline = (now - la).total_seconds() / 3600
                if hours_offline > 48:
                    alerts.append({
                        "type":   "RIVAL_INACTIVO",
                        "level":  "INFO",
                        "player": None,
                        "detail": f"Equipo '{t.get('teamname') or t.get('name','?')}' lleva {hours_offline:.0f}h sin conectarse — no reaccionará a un robo",
                        "action": "Aprovechar para robar por cláusula",
                    })
            except Exception:
                pass
    except Exception:
        pass

    level_order = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "INFO": 3}
    alerts.sort(key=lambda a: level_order.get(a["level"], 9))

    criticos = [a for a in alerts if a["level"] == "CRITICO"]
    altos    = [a for a in alerts if a["level"] == "ALTO"]

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "CRITICO": len(criticos),
            "ALTO":    len(altos),
            "MEDIO":   len([a for a in alerts if a["level"] == "MEDIO"]),
            "INFO":    len([a for a in alerts if a["level"] == "INFO"]),
        },
        "global_alert": (
            "🚨 ACCIÓN INMEDIATA REQUERIDA" if criticos else
            "⚠️ Hay situaciones que requieren atención" if altos else
            "✅ Sin anomalías críticas"
        ),
        "alerts": alerts,
    }


@app.get("/monitor/anomalies", tags=["Inteligencia"])
async def monitor_anomalies(client: FutmondoClient = Depends(get_client)):
    """
    **Monitor de anomalías — detecta situaciones críticas y comportamiento sospechoso.**

    Escanea en paralelo cuatro fuentes y clasifica las alertas por nivel:

    - **CRITICO** — Cláusulas propias por debajo del mínimo (sanción inminente)
    - **ALTO**    — Jugador nuestro traspasado hoy / jugador sin cláusula
    - **MEDIO**   — Rival con ≥3 pujas simultáneas en el mercado (posible bot/API)
    - **INFO**    — Rival sin conectarse >48h (buena ventana para robar)

    La autogestión ejecuta este monitor en cada ciclo y registra en el log
    cualquier anomalía de nivel CRITICO o ALTO.
    """
    try:
        return await _collect_anomalies(client)
    except Exception as exc:
        _handle_error(exc)


# ── Market Watch ───────────────────────────────────────────────────────────────

def _parse_expiry(player: dict):
    raw = player.get("expirationDate") or player.get("expires") or player.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _listing_status(player: dict, min_accept_ratio: float) -> dict:
    """Analiza el estado de un listing: tiempo restante, mejor puja, recomendación."""
    now        = datetime.now(timezone.utc)
    expiry     = _parse_expiry(player)
    buy_price  = float(player.get("buyPrice") or 0)
    value      = float(player.get("value") or 0)
    list_price = float(player.get("price") or 0)
    bids       = player.get("bids") or []

    # Mejor puja recibida
    best_bid = 0.0
    best_bidder = None
    if isinstance(bids, list) and bids:
        for b in bids:
            amt = float(b.get("amount") or b.get("price") or b.get("bid") or 0)
            if amt > best_bid:
                best_bid    = amt
                best_bidder = b.get("username") or b.get("user") or b.get("userId")

    # Tiempo restante
    hours_left = None
    if expiry:
        delta = expiry - now
        hours_left = max(0.0, delta.total_seconds() / 3600)

    # Umbral mínimo para aceptar: buyPrice × min_accept_ratio
    min_accept = buy_price * min_accept_ratio

    # Recomendación
    if hours_left is not None and hours_left < 2 and best_bid == 0:
        recommendation = "CANCELAR_URGENTE"   # expira pronto sin puja → recuperar jugador
    elif hours_left is not None and hours_left < 6 and best_bid == 0:
        recommendation = "CANCELAR"            # expira hoy sin puja → recuperar jugador
    elif best_bid >= min_accept:
        recommendation = "ACEPTAR_PUJA"        # hay oferta interesante
    elif best_bid > 0 and best_bid < min_accept:
        recommendation = "PUJA_BAJA"           # hay puja pero insuficiente
    else:
        recommendation = "ESPERAR"             # sin puja, tiempo suficiente

    return {
        "name":           player.get("name", "?"),
        "role":           player.get("role", "?"),
        "list_price":     int(list_price),
        "buy_price":      int(buy_price),
        "market_value":   int(value),
        "best_bid":       int(best_bid),
        "best_bidder":    best_bidder,
        "num_bids":       len(bids) if isinstance(bids, list) else 0,
        "expiry":         expiry.isoformat() if expiry else None,
        "hours_left":     round(hours_left, 1) if hours_left is not None else None,
        "min_accept":     int(min_accept),
        "recommendation": recommendation,
        "_id":            player.get("id") or player.get("_id"),
        "_slug":          player.get("slug"),
    }


@app.get("/market/watch", tags=["Mercado"])
async def market_watch(
    min_accept_ratio: float = Query(
        0.80, ge=0.0, le=1.0,
        description="Ratio mínimo sobre buyPrice para considerar una puja interesante (0.80 = 80% del coste)"
    ),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Vigila los listings activos: pujas recibidas, tiempo restante y recomendación.**

    Para cada jugador en venta muestra:
    - Mejor puja recibida y quién la hizo
    - Tiempo hasta que expira el listing
    - Recomendación: `ESPERAR` / `ACEPTAR_PUJA` / `PUJA_BAJA` / `CANCELAR` / `CANCELAR_URGENTE`

    `min_accept_ratio` define el umbral de puja aceptable como fracción del buyPrice.
    Ej: 0.80 → aceptar si la puja es ≥ 80% de lo que costó el jugador.
    """
    try:
        raw = await client.get_my_players_in_market()
    except Exception as exc:
        _handle_error(exc)

    listed = _extract_list(raw)
    statuses = [_listing_status(p, min_accept_ratio) for p in listed]
    statuses.sort(key=lambda s: (s["hours_left"] or 9999))  # más urgentes primero

    urgent   = [s for s in statuses if s["recommendation"] in ("CANCELAR_URGENTE", "CANCELAR")]
    accept   = [s for s in statuses if s["recommendation"] == "ACEPTAR_PUJA"]
    low_bid  = [s for s in statuses if s["recommendation"] == "PUJA_BAJA"]
    waiting  = [s for s in statuses if s["recommendation"] == "ESPERAR"]

    return {
        "min_accept_ratio": min_accept_ratio,
        "summary": {
            "total_listed":        len(statuses),
            "urgent_cancel":       len(urgent),
            "accept_bid":          len(accept),
            "low_bid":             len(low_bid),
            "waiting":             len(waiting),
        },
        "listings": statuses,
    }


@app.post("/market/watch/cancel-expired", tags=["Mercado"])
async def cancel_expiring(
    hours_threshold: float = Query(
        3.0, ge=0.5, le=24.0,
        description="Cancelar listings que expiren en menos de N horas SIN puja"
    ),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Cancela automáticamente los listings que van a expirar sin puja.**

    Si un listing expira sin ninguna puja, Futmondo se queda con el jugador.
    Este endpoint cancela los listings en riesgo antes de que eso ocurra,
    devolviendo el jugador a tu plantilla.

    Sólo cancela listings con `num_bids == 0` y `hours_left < hours_threshold`.
    """
    try:
        raw = await client.get_my_players_in_market()
    except Exception as exc:
        _handle_error(exc)

    listed   = _extract_list(raw)
    statuses = [_listing_status(p, 0.0) for p in listed]
    to_cancel = [
        s for s in statuses
        if s["num_bids"] == 0
        and s["hours_left"] is not None
        and s["hours_left"] < hours_threshold
    ]

    results = []
    for s in to_cancel:
        pid    = s["_id"]
        result = {"player": s["name"], "hours_left": s["hours_left"], "status": "pending", "error": None}
        if pid:
            try:
                resp = await client.remove_player_from_market(pid)
                ans  = resp.get("answer", {}) if isinstance(resp, dict) else {}
                if isinstance(ans, dict) and ans.get("error"):
                    result["status"] = "error"
                    result["error"]  = ans.get("code", "unknown")
                else:
                    result["status"] = "cancelled"
            except Exception as exc:
                result["status"] = "error"
                result["error"]  = str(exc)
        results.append(result)

    return {
        "hours_threshold": hours_threshold,
        "cancelled":       len([r for r in results if r["status"] == "cancelled"]),
        "details":         results,
        "message":         "Jugadores recuperados para tu plantilla" if results else "No hay listings urgentes sin puja",
    }


def _is_available(player: dict) -> bool:
    """
    Devuelve False si el jugador está lesionado, sancionado o no disponible.

    Futmondo usa varios campos para indicar disponibilidad:
      - active: False / 0 → no disponible
      - status / playerStatus: 0=OK, 1=duda, 2=lesionado, 3=sancionado
      - injuryStatus: campo alternativo en algunas versiones de la API
    Los valores 0/False/None se consideran DISPONIBLE.
    Los valores ≥ 2 en status numérico se consideran NO DISPONIBLE.
    """
    # Jugador en el mercado de venta → no puede puntuar en la jornada
    market = player.get("market")
    if isinstance(market, dict) and market.get("inMarket"):
        return False
    if isinstance(market, bool) and market:
        return False

    # Campo booleano / entero activo
    active = player.get("active")
    if active is not None and not active:
        return False

    # Campo status (numérico): 0=ok, 1=duda, 2=lesionado, 3=sancionado
    for field in ("status", "playerStatus", "injuryStatus", "playeractive"):
        val = player.get(field)
        if val is None:
            continue
        if isinstance(val, bool):
            if not val:
                return False
        elif isinstance(val, int):
            if val >= 2:   # 2=lesionado, 3=sancionado
                return False
        elif isinstance(val, str):
            if val.lower() in ("injured", "lesionado", "suspended", "sancionado",
                               "out", "unavailable", "baja", "lesion", "sancion"):
                return False
    return True


def _availability_label(player: dict) -> str:
    """Etiqueta legible del estado de disponibilidad."""
    if not _is_available(player):
        for field in ("status", "playerStatus", "injuryStatus"):
            val = player.get(field)
            if isinstance(val, int) and val >= 2:
                return {2: "LESIONADO", 3: "SANCIONADO"}.get(val, "NO_DISPONIBLE")
            if isinstance(val, str):
                return val.upper()
        return "NO_DISPONIBLE"
    status_val = player.get("status") or player.get("playerStatus")
    if isinstance(status_val, int) and status_val == 1:
        return "DUDA"
    return "OK"


def _fitness(player: dict) -> list[float]:
    """Últimas N jornadas (scores individuales), de más antigua a más reciente."""
    avg = player.get("average") or {}
    return [float(x) for x in (avg.get("fitness") or []) if x is not None]


def _trend(fitness: list[float]) -> str:
    """Tendencia basada en últimas 5 jornadas vs media de temporada."""
    if len(fitness) < 2:
        return "sin_datos"
    first_half = fitness[: len(fitness) // 2]
    second_half = fitness[len(fitness) // 2:]
    avg_first  = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    diff = avg_second - avg_first
    if diff >= 2:
        return "subiendo"
    if diff <= -2:
        return "bajando"
    return "estable"


def _player_stats(p: dict, jornadas_restantes: int) -> dict:
    """Genera un dict completo de estadísticas + previsión para un jugador."""
    avg      = (p.get("average") or {})
    season_avg   = float(avg.get("average") or 0)
    last5_avg    = float(avg.get("averageLastFive") or season_avg)
    home_avg     = float(avg.get("homeAverage") or season_avg)
    away_avg     = float(avg.get("awayAverage") or season_avg)
    matches      = int(avg.get("matches") or 0)
    total_pts    = float(p.get("points") or 0)
    fitness      = _fitness(p)
    trend        = _trend(fitness)
    value        = float(p.get("value") or 0)
    buyPrice     = float(p.get("buyPrice") or 0)
    on_market    = bool(p.get("market"))

    # Previsión: conservadora=last5, realista=season_avg, optimista=max(home,last5)
    forecast_conservative = round(last5_avg * jornadas_restantes, 1)
    forecast_realistic    = round(season_avg * jornadas_restantes, 1)
    forecast_optimistic   = round(max(home_avg, last5_avg, season_avg) * 1.10 * jornadas_restantes, 1)

    roi = round((value - buyPrice) / buyPrice * 100, 1) if buyPrice else None

    return {
        "name":         p.get("name", "?"),
        "role":         p.get("role", "?"),
        "role2":        p.get("role2"),
        "team":         p.get("team", "?"),
        "on_market":    on_market,
        "stats": {
            "season_avg":     round(season_avg, 2),
            "last5_avg":      round(last5_avg, 2),
            "home_avg":       round(home_avg, 2),
            "away_avg":       round(away_avg, 2),
            "matches_played": matches,
            "total_pts":      round(total_pts, 1),
            "fitness":        fitness,   # últimas 5 jornadas individuales
            "trend":          trend,
        },
        "value": {
            "current": value,
            "buy_price": buyPrice,
            "roi_pct": roi,
        },
        "forecast": {
            "jornadas_restantes": jornadas_restantes,
            "conservative":  forecast_conservative,   # basado en last5
            "realistic":     forecast_realistic,       # basado en season_avg
            "optimistic":    forecast_optimistic,      # +10% sobre mejor escenario
        },
    }


@app.get("/stats/squad", tags=["Estadísticas"])
async def stats_squad(
    jornadas_played: int = Query(None, description="Jornadas jugadas (auto si omitido)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Análisis completo del equipo jornada a jornada.**

    Devuelve para cada jugador:
    - Media de temporada y últimas 5 jornadas (fitness individual)
    - Medias en casa / fuera
    - Tendencia (subiendo / estable / bajando)
    - Previsión realista, conservadora y optimista de puntos restantes
    - ROI de la inversión (valor actual vs precio de compra)

    El equipo se ordena por `season_avg` descendente.
    """
    try:
        raw = await client.get_team_players()
    except Exception as exc:
        _handle_error(exc)

    players = _extract_list(raw)

    # Estimar jornadas jugadas a partir de la media de matches del equipo
    if jornadas_played is None:
        all_matches = [int((p.get("average") or {}).get("matches") or 0) for p in players]
        jornadas_played = int(sum(all_matches) / len(all_matches)) if all_matches else 22
    jornadas_restantes = max(0, LALIGA_TOTAL_JORNADAS - jornadas_played)

    stats = [_player_stats(p, jornadas_restantes) for p in players]
    stats.sort(key=lambda s: s["stats"]["season_avg"], reverse=True)

    # Resumen del equipo
    active = [s for s in stats if not s["on_market"]]
    all_avgs = [s["stats"]["season_avg"] for s in stats]
    active_avgs = [s["stats"]["season_avg"] for s in active]
    xi_analysis = _best_lineup_analysis(players)

    summary = {
        "jornadas_played":    jornadas_played,
        "jornadas_remaining": jornadas_restantes,
        "squad_size":         len(stats),
        "active_players":     len(active),
        "team_avg_jornada":   round(sum(all_avgs) / len(all_avgs), 2) if all_avgs else 0,
        "xi_projected_avg":   xi_analysis.get("projected_pts_jornada", 0),
        "xi_formation":       xi_analysis.get("formation"),
        "forecast_realistic": round(xi_analysis.get("projected_pts_jornada", 0) * jornadas_restantes, 1),
        "target_150":         150,
        "gap_to_150":         round(150 - xi_analysis.get("projected_pts_jornada", 0), 2),
    }

    return {"summary": summary, "players": stats}


@app.get("/stats/targets", tags=["Estadísticas"])
async def stats_targets(
    jornadas_played: int = Query(None, description="Jornadas jugadas (auto si omitido)"),
    top: int = Query(20, ge=5, le=50, description="Nº de objetivos a devolver"),
    min_avg: float = Query(7.0, ge=0.0, description="Media mínima por jornada"),
    max_clause: float = Query(0, ge=0, description="Cláusula máxima en €  (0 = sin límite)"),
    max_teams: int = Query(20, ge=1, le=50),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Análisis de los mejores jugadores disponibles para fichar.**

    Escanea todos los equipos rivales y devuelve los mejores jugadores por:
    - Media por jornada (season_avg y últimas 5)
    - Tendencia de forma
    - Previsión de puntos que aportarían al equipo
    - Coste por cláusula

    Filtros: `min_avg`, `max_clause`.
    """
    try:
        champ_raw = await client.get_championship_info()
    except Exception as exc:
        _handle_error(exc)

    champ_data = _extract_list(champ_raw) or []
    if isinstance(champ_raw, dict):
        inner = champ_raw.get("answer", champ_raw)
        if isinstance(inner, dict):
            champ_data = inner.get("teams", [])

    my_team_id = client.user_team_id
    rival_ids = [
        t.get("teamid") or t.get("id") or t.get("_id")
        for t in champ_data
        if (t.get("teamid") or t.get("id") or t.get("_id")) != my_team_id
    ][:max_teams]

    # Obtener plantillas rivales en paralelo
    async def fetch_team(tid: str) -> list[dict]:
        try:
            raw = await client.get_team_players(team_id=tid)
            return _extract_list(raw)
        except Exception:
            return []

    all_teams = await asyncio.gather(*[fetch_team(tid) for tid in rival_ids])
    my_raw = await client.get_team_players()
    my_ids = {_get_field(p, "_id", "id") for p in _extract_list(my_raw)}

    # Estimar jornadas jugadas
    all_players_flat = [p for team in all_teams for p in team]
    if jornadas_played is None:
        all_matches = [int((p.get("average") or {}).get("matches") or 0) for p in all_players_flat if p]
        jornadas_played = int(sum(all_matches) / len(all_matches)) if all_matches else 22
    jornadas_restantes = max(0, LALIGA_TOTAL_JORNADAS - jornadas_played)

    targets = []
    for team_players in all_teams:
        for p in team_players:
            pid = _get_field(p, "_id", "id")
            if pid in my_ids:
                continue
            avg = _avg_per_game(p)
            if avg < min_avg:
                continue
            clause = _clause_price(p)
            if max_clause and clause > max_clause:
                continue
            if clause <= 0:
                continue

            s = _player_stats(p, jornadas_restantes)
            s["id"]           = pid
            s["slug"]         = p.get("slug")
            s["clause_price"] = clause
            s["efficiency"]   = _efficiency(p, "clause")
            targets.append(s)

    targets.sort(key=lambda x: x["stats"]["season_avg"], reverse=True)
    targets = targets[:top]

    return {
        "jornadas_played":    jornadas_played,
        "jornadas_remaining": jornadas_restantes,
        "total_found":        len(targets),
        "filters":            {"min_avg": min_avg, "max_clause": max_clause or "sin_límite"},
        "targets":            targets,
    }


# ── War Room: execute attack ───────────────────────────────────────────────────

@app.post("/warroom/execute-attack", tags=["Guerra"])
async def execute_attack(
    prefer_gk:       bool  = Query(True,          description="Priorizar robos de portero (máximo daño)"),
    max_clause_gk:   float = Query(150_000_000,   description="Cláusula máxima para portero"),
    max_clause_any:  float = Query(300_000_000,   description="Cláusula máxima cualquier jugador"),
    min_avg:         float = Query(7.0,            description="Media mínima del objetivo"),
    top:             int   = Query(3,   ge=1, le=5, description="Máximo de robos a ejecutar"),
    dry_run:         bool  = Query(False,          description="Solo planifica, no ejecuta"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Ataque pre-jornada — roba los jugadores más vulnerables ahora mismo.**

    Estrategia agresiva:
    1. GK de rival con 1 solo portero → -5 pts garantizados + XI roto
    2. Rivales confiados (offline >12h) primero (no reaccionarán)
    3. Mejor jugador disponible por avg si no hay portero robable

    Solo ejecuta si hay huecos libres en el roster (máx 12 activos).
    """
    now = datetime.now(timezone.utc)

    my_raw    = await client.get_team_players()
    my_players = _extract_list(my_raw)
    on_market  = sum(1 for p in my_players if p.get("market"))
    active_cnt = len(my_players) - on_market
    free_slots = max(0, MAX_ROSTER_SIZE - len(my_players))

    if free_slots == 0 and not dry_run:
        return {
            "status":    "roster_full",
            "message":   f"Roster lleno ({len(my_players)}/{MAX_ROSTER_SIZE} jugadores, {on_market} en venta). Espera a que expiren listings.",
            "free_slots": 0,
            "dry_run":   dry_run,
        }

    my_ids = {_get_field(p, "_id", "id") for p in my_players}

    champ_raw   = await client.get_championship_info()
    inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id       = client.user_team_id
    rival_teams = [t for t in champ_teams if (t.get("teamid") or t.get("id")) != my_id]

    async def fetch(t):
        tid = t.get("teamid") or t.get("id")
        try:
            raw = await client.get_team_players(team_id=tid)
            return t, _extract_list(raw)
        except Exception:
            return t, []

    all_rosters = await asyncio.gather(*[fetch(t) for t in rival_teams])

    # Build prioritized attack list
    attack_candidates = []
    for team_info, roster in all_rosters:
        team_name = team_info.get("teamname") or team_info.get("name", "?")
        last_acc  = team_info.get("lastAccess") or ""
        hours_offline = None
        if last_acc:
            try:
                la = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
                hours_offline = (now - la).total_seconds() / 3600
            except Exception:
                pass

        confiado   = (hours_offline or 0) > 12
        gks        = [p for p in roster if p.get("role", "").lower() == "portero"]
        only_one_gk = len(gks) == 1

        for p in roster:
            pid = _get_field(p, "_id", "id")
            if pid in my_ids:
                continue
            role     = p.get("role", "").lower()
            is_gk    = role == "portero"
            avg      = _avg_per_game(p)
            c_price  = _clause_price(p)
            cl       = p.get("clause") or {}
            transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
            c_hours  = _clause_hours_left(p)

            if transferred or c_price <= 0 or (c_hours is not None and c_hours <= 0):
                continue
            if avg < min_avg:
                continue

            # GK of single-GK team → maximum damage
            is_gk_target = is_gk and only_one_gk and c_price <= max_clause_gk and prefer_gk
            if not is_gk_target and c_price > max_clause_any:
                continue

            priority = 0
            if is_gk_target:
                priority += 1000  # GK steal = maximum damage
            if confiado:
                priority += 200
            priority += avg * 10
            priority -= c_price / 1_000_000  # cheaper = easier to afford

            attack_candidates.append({
                "name":         p.get("name"),
                "role":         role,
                "team":         team_name,
                "avg_per_game": round(avg, 2),
                "clause_price": int(c_price),
                "clause_hours_left": round(c_hours, 1) if c_hours else None,
                "confiado":     confiado,
                "hours_offline": round(hours_offline, 1) if hours_offline else 0,
                "is_gk_killer": is_gk_target,
                "only_one_gk":  only_one_gk,
                "priority":     round(priority, 2),
                "damage":       "¡GK ÚNICO ROBADO → -5 pts + XI roto!" if is_gk_target else f"avg {avg:.2f}/j",
                "id":           pid,
                "slug":         p.get("slug"),
            })

    attack_candidates.sort(key=lambda x: -x["priority"])
    plan = attack_candidates[:top]

    if dry_run or free_slots == 0:
        return {
            "status":     "plan" if dry_run else "roster_full_plan",
            "dry_run":    dry_run,
            "free_slots": free_slots,
            "roster":     {"total": len(my_players), "active": active_cnt, "on_market": on_market},
            "attack_plan": plan,
            "warning":    None if free_slots > 0 else f"Roster lleno — libera slots primero. {on_market} jugadores en venta.",
        }

    # Execute attacks for available free slots
    executed = []
    errors   = []
    slots_used = 0

    for target in plan:
        if slots_used >= free_slots:
            break
        pid   = target["id"]
        slug  = target["slug"]
        price = target["clause_price"]
        resp  = await client.pay_player_clause(pid, str(slug), price)
        ans   = resp.get("answer", {}) if isinstance(resp, dict) else {}
        if isinstance(ans, dict) and ans.get("error"):
            err_code = ans.get("code", "unknown")
            errors.append({**target, "error": err_code})
            if err_code == "api.market.max_number_players_in_roster":
                break  # roster full, stop
        else:
            executed.append({**target, "status": "stolen"})
            slots_used += 1

    return {
        "status":       "executed",
        "dry_run":      False,
        "free_slots":   free_slots,
        "slots_used":   slots_used,
        "stolen":       executed,
        "errors":       errors,
        "roster_after": {"total": len(my_players) + slots_used},
    }


# ── War Room: 180 pts attack plan ─────────────────────────────────────────────

@app.get("/warroom/plan180", tags=["Guerra"])
async def plan180(
    jornadas_remaining: int   = Query(16, ge=1,  description="Jornadas restantes en la temporada"),
    max_clause:         float = Query(0,  ge=0,  description="Cláusula máxima por jugador (0=sin límite)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Plan de ataque para 180 pts/jornada.**

    Analiza la plantilla actual vs objetivo de 180 pts/jornada y genera:
    1. **Gap analysis**: cuántos pts faltan y qué avg necesita cada posición
    2. **Jugadores a vender**: los que lastran el equipo (peor avg)
    3. **Jugadores a fichar**: rivales con avg alto que acercan al objetivo
    4. **Cash plan**: cuánto necesitas, cuánto tienes, qué vender primero
    5. **Formación óptima**: XI que maximiza puntos con plantilla actual
    """
    now = datetime.now(timezone.utc)

    # ── Datos propios ─────────────────────────────────────────────────────────
    my_raw    = await client.get_team_players()
    my_players = _extract_list(my_raw)

    budget_raw = await client.get_user_info()
    budget_ans = budget_raw.get("answer", budget_raw) if isinstance(budget_raw, dict) else {}
    # Futmondo stores cash in mondos/money/balance field
    cash = 0.0
    if isinstance(budget_ans, dict):
        for key in ("mondos", "money", "balance", "cash", "coins"):
            v = budget_ans.get(key)
            if isinstance(v, (int, float)):
                cash = float(v)
                break
        # May be nested inside a team or user object
        if cash == 0.0:
            for sub in budget_ans.values():
                if isinstance(sub, dict):
                    for key in ("mondos", "money", "balance", "cash", "coins"):
                        v = sub.get(key)
                        if isinstance(v, (int, float)):
                            cash = float(v)
                            break
                if cash:
                    break

    xi = _best_lineup_analysis(my_players, TARGET_PTS_JORNADA)
    current_proj = xi.get("projected_pts_jornada", 0)
    gap          = TARGET_PTS_JORNADA - current_proj
    avg_needed   = TARGET_PTS_JORNADA / 11  # ~16.4/player avg needed

    # ── Jugadores a vender (peor eficiencia, liberar cash) ───────────────────
    active_players = [p for p in my_players if not p.get("market")]
    sorted_by_eff  = sorted(active_players, key=lambda p: _avg_per_game(p))

    sell_plan = []
    for p in sorted_by_eff:
        avg     = _avg_per_game(p)
        value   = float(p.get("value") or 0)
        buy_p   = float(p.get("buyPrice") or 0)
        sell_at = _compute_sell_price(value, buy_p)
        sell_plan.append({
            "name":      p.get("name"),
            "role":      p.get("role"),
            "avg_per_game": round(avg, 2),
            "value":     int(value),
            "buy_price": int(buy_p),
            "sell_at":   sell_at,
            "profit":    sell_at - int(buy_p),
            "below_target_avg": avg < avg_needed,
        })

    # ── Rivales con avg alto — fichajas objetivo ──────────────────────────────
    champ_raw   = await client.get_championship_info()
    inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id       = client.user_team_id
    rival_teams = [t for t in champ_teams if (t.get("teamid") or t.get("id")) != my_id]

    async def fetch(t):
        tid = t.get("teamid") or t.get("id")
        try:
            raw = await client.get_team_players(team_id=tid)
            return _extract_list(raw)
        except Exception:
            return []

    all_rosters = await asyncio.gather(*[fetch(t) for t in rival_teams])
    my_ids      = {_get_field(p, "_id", "id") for p in my_players}

    target_list = []
    for roster in all_rosters:
        for p in roster:
            pid     = _get_field(p, "_id", "id")
            if pid in my_ids:
                continue
            avg     = _avg_per_game(p)
            c_price = _clause_price(p)
            cl      = p.get("clause") or {}
            transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
            c_hours = _clause_hours_left(p)
            if transferred or c_price <= 0 or (c_hours is not None and c_hours <= 0):
                continue
            if avg < 7.0:
                continue
            if max_clause and c_price > max_clause:
                continue
            last5 = float((p.get("average") or {}).get("averageLastFive") or avg)
            target_list.append({
                "name":         p.get("name"),
                "role":         p.get("role"),
                "avg_per_game": round(avg, 2),
                "last5_avg":    round(last5, 2),
                "clause_price": int(c_price),
                "clause_hours_left": round(c_hours, 1) if c_hours else None,
                "above_target_avg": avg >= avg_needed,
                "affordable":   c_price <= cash,
                "id":           pid,
                "slug":         p.get("slug"),
            })

    target_list.sort(key=lambda x: -x["avg_per_game"])
    top_signings = target_list[:15]

    # ── Cash plan ─────────────────────────────────────────────────────────────
    # Estimate cash if we sell worst players
    cumulative_cash = cash
    cash_steps = []
    for sp in sell_plan[:5]:
        cumulative_cash += sp["sell_at"]
        cash_steps.append({
            "sell": sp["name"],
            "gain": sp["sell_at"],
            "cumulative_cash": int(cumulative_cash),
        })

    # Estimate new avg after swapping worst players for top targets
    new_avg_projection = current_proj
    if sell_plan and top_signings:
        worst_avg  = sell_plan[0]["avg_per_game"] if sell_plan else 0
        best_steal = top_signings[0]["avg_per_game"] if top_signings else 0
        improvement_per_swap = best_steal - worst_avg
        new_avg_projection = current_proj + improvement_per_swap

    season_pts_now    = current_proj * jornadas_remaining
    season_pts_target = TARGET_PTS_JORNADA * jornadas_remaining

    return {
        "target":  TARGET_PTS_JORNADA,
        "current": {
            "projected_pts_jornada": round(current_proj, 2),
            "gap_to_target":         round(gap, 2),
            "formation":             xi.get("formation"),
            "avg_needed_per_player": round(avg_needed, 2),
            "cash_available":        int(cash),
            "season_pts_remaining":  round(season_pts_now, 1),
        },
        "after_one_swap_estimate": round(new_avg_projection, 2),
        "sell_plan": sell_plan,
        "signing_targets": top_signings,
        "cash_plan": {
            "current_cash":         int(cash),
            "sell_steps":           cash_steps,
            "total_after_5_sales":  int(cumulative_cash),
        },
        "season_outlook": {
            "jornadas_remaining":   jornadas_remaining,
            "pts_at_current_pace":  round(season_pts_now, 1),
            "pts_if_target_met":    round(season_pts_target, 1),
            "pts_gap_season":       round(season_pts_target - season_pts_now, 1),
        },
    }


# ── Auto/Play: ventas + fichajes por cláusula + alineación ────────────────────

@app.post("/auto/play", tags=["Automático", "Guerra"])
async def auto_play(
    body: AutoPlayRequest,
    client: FutmondoClient = Depends(get_client),
):
    """
    **Todo en un solo paso: vende, ficha y muestra la alineación óptima.**

    Flujo de ejecución:
    1. **Ventas** — lista los jugadores con peor rendimiento al precio máximo permitido (valor×1.5)
    2. **Fichajes** — roba por cláusula a los rivales más rentables, priorizando:
       - Portero único del rival (garantiza -5 pts al rival + XI roto)
       - Rivales offline >12h (no reaccionarán)
       - Mayor avg_per_game
    3. **Alineación** — calcula el XI óptimo con la plantilla resultante

    Siempre usa `dry_run=true` primero para revisar el plan antes de ejecutar.
    """
    now = datetime.now(timezone.utc)

    # ── 1. Datos propios y del campeonato (en paralelo) ───────────────────────
    team_raw, champ_raw = await asyncio.gather(
        client.get_team_players(),
        client.get_championship_info(),
    )
    my_players = _extract_list(team_raw)
    on_market  = sum(1 for p in my_players if p.get("market"))
    free_slots = max(0, MAX_ROSTER_SIZE - len(my_players))

    inner       = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
    champ_teams = inner.get("teams", []) if isinstance(inner, dict) else []
    my_id       = client.user_team_id
    rival_teams = [t for t in champ_teams if (t.get("teamid") or t.get("id")) != my_id]

    my_ids = {_get_field(p, "_id", "id") for p in my_players}

    # ── 2. FASE VENTAS ────────────────────────────────────────────────────────
    active = [p for p in my_players if not p.get("market")]
    gks    = [p for p in active if p.get("role", "").lower() == "portero"]

    # Ordenar por change ASC (mayor bajada de valor primero, como el bot JS getSellablePlayers)
    sorted_by_eff = sorted(active, key=lambda p: float(p.get("change") or 0))

    sell_candidates = []
    for p in sorted_by_eff:
        pid  = _get_field(p, "_id", "id")
        avg  = _avg_per_game(p)
        role = p.get("role", "").lower()
        # Nunca vender el único portero
        if role == "portero" and len(gks) <= 1:
            continue
        # Respetar protección por avg
        if body.sell_min_avg > 0 and avg >= body.sell_min_avg:
            continue
        sell_candidates.append(p)

    sell_count = max(1, int(len(sell_candidates) * body.sell_bottom_pct)) if sell_candidates else 0
    sell_targets = sell_candidates[:sell_count]

    sell_plan = []
    for p in sell_targets:
        pid      = _get_field(p, "_id", "id")
        slug     = p.get("slug")
        value    = float(_get_field(p, "value", "marketValue") or 0)
        buy_p    = float(p.get("buyPrice") or 0)
        price    = _compute_sell_price(value, buy_p)
        change = int(p.get("change") or 0)
        action = {
            "name":         p.get("name"),
            "role":         p.get("role"),
            "avg_per_game": round(_avg_per_game(p), 2),
            "change":       change,
            "value":        int(value),
            "buy_price":    int(buy_p),
            "list_price":   price,
            "profit":       price - int(buy_p),
            "status":       "pending",
            "error":        None,
            "_pid":         pid,
            "_slug":        slug,
        }
        sell_plan.append(action)

    if not body.dry_run:
        for action in sell_plan:
            # Venta directa: elimina el jugador al instante, libera hueco de inmediato.
            # Si falla (p.ej. jugador recién fichado sin ventana de venta), recurre a
            # ponerlo en mercado al precio calculado.
            direct = await client.direct_sell(action["_pid"], action["_slug"])
            direct_ans = direct.get("answer", {}) if isinstance(direct, dict) else {}
            if isinstance(direct_ans, dict) and not direct_ans.get("error"):
                action["status"] = "ok"
                action["method"] = "direct_sell"
            else:
                await _exec_action(
                    client.set_player_in_market(action["_pid"], action["_slug"], action["list_price"]),
                    action,
                )
                action.setdefault("method", "market_listing")

    sold_ok = sum(1 for a in sell_plan if a.get("status") == "ok")

    # ── 3. FASE FICHAJES (WARROOM) ────────────────────────────────────────────
    async def _fetch_rival(t):
        tid = t.get("teamid") or t.get("id")
        try:
            raw = await client.get_team_players(team_id=tid)
            return t, _extract_list(raw)
        except Exception:
            return t, []

    all_rosters = await asyncio.gather(*[_fetch_rival(t) for t in rival_teams])

    steal_candidates = []
    for team_info, roster in all_rosters:
        team_name   = team_info.get("teamname") or team_info.get("name", "?")
        last_acc    = team_info.get("lastAccess") or ""
        hours_offline = None
        if last_acc:
            try:
                la = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
                hours_offline = (now - la).total_seconds() / 3600
            except Exception:
                pass

        confiado    = (hours_offline or 0) > 12
        team_gks    = [p for p in roster if p.get("role", "").lower() == "portero"]
        only_one_gk = len(team_gks) == 1

        for p in roster:
            pid     = _get_field(p, "_id", "id")
            if pid in my_ids:
                continue
            role    = p.get("role", "").lower()
            is_gk   = role == "portero"
            avg     = _avg_per_game(p)
            c_price = _clause_price(p)
            cl      = p.get("clause") or {}
            transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
            c_hours = _clause_hours_left(p)

            if transferred or c_price <= 0 or (c_hours is not None and c_hours <= 0):
                continue
            if avg < body.steal_min_avg:
                continue
            if body.steal_max_clause > 0 and c_price > body.steal_max_clause:
                continue

            is_gk_kill = is_gk and only_one_gk and body.prefer_gk
            priority   = 0
            if is_gk_kill:
                priority += 1000
            if confiado:
                priority += 200
            priority += avg * 10
            priority -= c_price / 1_000_000

            steal_candidates.append({
                "name":             p.get("name"),
                "role":             p.get("role"),
                "team":             team_name,
                "avg_per_game":     round(avg, 2),
                "clause_price":     int(c_price),
                "clause_hours_left": round(c_hours, 1) if c_hours is not None else None,
                "confiado":         confiado,
                "hours_offline":    round(hours_offline, 1) if hours_offline else 0,
                "is_gk_killer":     is_gk_kill,
                "priority":         round(priority, 2),
                "status":           "pending",
                "error":            None,
                "_id":              pid,
                "_slug":            p.get("slug"),
            })

    steal_candidates.sort(key=lambda x: -x["priority"])
    # Slots disponibles tras las ventas (en dry_run vendidos no liberan slots reales)
    effective_free = free_slots + (sold_ok if not body.dry_run else 0)
    steal_plan = steal_candidates[:min(body.steal_top, max(0, effective_free))]

    stolen_ok = 0
    if not body.dry_run:
        for action in steal_plan:
            if stolen_ok >= effective_free:
                break
            resp = await client.pay_player_clause(action["_id"], action["_slug"], action["clause_price"])
            ans  = resp.get("answer", {}) if isinstance(resp, dict) else {}
            if isinstance(ans, dict) and ans.get("error"):
                err_code = ans.get("code", "unknown")
                action["status"] = "error"
                action["error"]  = err_code
                if err_code == "api.market.max_number_players_in_roster":
                    break
            else:
                action["status"] = "ok"
                stolen_ok += 1
    else:
        steal_plan = steal_candidates[:body.steal_top]

    # ── 4. FASE ALINEACIÓN ────────────────────────────────────────────────────
    lineup = _best_lineup_analysis(my_players, target_apg=body.target_jornada)

    # Limpiar claves internas (_pid, _slug, _id) de la respuesta
    for a in sell_plan:
        a.pop("_pid", None)
        a.pop("_slug", None)
    for a in steal_plan:
        a.pop("_id", None)
        a.pop("_slug", None)

    return {
        "dry_run": body.dry_run,
        "status":  "plan" if body.dry_run else "executed",
        "roster": {
            "total":      len(my_players),
            "active":     len(my_players) - on_market,
            "on_market":  on_market,
            "free_slots": free_slots,
        },
        "sell_plan":  sell_plan,
        "steal_plan": steal_plan,
        "lineup": {
            "formation":             lineup.get("formation"),
            "projected_pts_jornada": round(lineup.get("projected_pts_jornada", 0), 2),
            "gap_to_target":         round(lineup.get("gap_to_target", 0), 2),
            "target":                body.target_jornada,
            "starters":              lineup.get("starters", []),
            "bench":                 lineup.get("bench", []),
        },
        "summary": {
            "sell_total":   len(sell_plan),
            "sell_ok":      sold_ok if not body.dry_run else 0,
            "steal_total":  len(steal_plan),
            "steal_ok":     stolen_ok if not body.dry_run else 0,
            "errors":       [a for a in sell_plan + steal_plan if a.get("status") == "error"],
        },
    }


# ── Fichajes — lista de espera para fichajes automáticos ─────────────────────

_FICHAJES_FILE = Path(__file__).parent / "fichajes.json"


def _read_fichajes() -> list[dict]:
    if _FICHAJES_FILE.exists():
        return json.loads(_FICHAJES_FILE.read_text())
    return []


def _write_fichajes(data: list[dict]) -> None:
    _FICHAJES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


class FichajeItem(BaseModel):
    player_id:   str       = Field(..., description="ID del jugador (campo '_id' del roster rival)")
    player_slug: str       = Field(..., description="Slug del jugador")
    price:       int       = Field(..., gt=0, description="Precio exacto de la cláusula")
    name:        str       = Field("",  description="Nombre descriptivo (opcional)")
    team_id:     str | None = Field(None, description="ID del equipo rival (necesario para snipe_mode)")


@app.get("/market/fichajes", tags=["Fichajes"])
async def list_fichajes():
    """
    **Lista de espera de fichajes** — jugadores que se intentarán fichar por cláusula
    en el próximo disparo (`POST /market/fichajes/fire`).

    El patrón recomendado (inspirado en bots de alta frecuencia) es ejecutar
    `/fire` con `retries=20` justo a medianoche, cuando Futmondo renueva las cláusulas.
    """
    return {"fichajes": _read_fichajes()}


@app.post("/market/fichajes", tags=["Fichajes"], status_code=201)
async def add_fichaje(item: FichajeItem):
    """Añade un jugador a la lista de espera de fichajes por cláusula."""
    data = _read_fichajes()
    # Evitar duplicados
    if any(c["player_id"] == item.player_id for c in data):
        raise HTTPException(status_code=409, detail="El jugador ya está en la lista")
    data.append(item.model_dump())
    _write_fichajes(data)
    return {"added": item.model_dump(), "total": len(data)}


@app.delete("/market/fichajes/{player_id}", tags=["Fichajes"])
async def remove_fichaje(player_id: str):
    """Elimina un jugador de la lista de espera."""
    data = _read_fichajes()
    new_data = [c for c in data if c["player_id"] != player_id]
    if len(new_data) == len(data):
        raise HTTPException(status_code=404, detail="Jugador no encontrado en la lista")
    _write_fichajes(new_data)
    return {"removed": player_id, "remaining": len(new_data)}


@app.delete("/market/fichajes", tags=["Fichajes"])
async def clear_fichajes():
    """Vacía la lista de espera completa."""
    _write_fichajes([])
    return {"status": "cleared"}


async def _fire_one_clause(
    client: FutmondoClient,
    pid: str,
    slug: str,
    price: int,
    retries: int,
    sleep_ms: int,
) -> tuple[bool, str | None]:
    """Dispara una cláusula con reintentos. Devuelve (ok, error_code)."""
    last_err: str | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.pay_player_clause(pid, slug, price)
            ans  = resp.get("answer", {}) if isinstance(resp, dict) else {}
            if isinstance(ans, dict) and not ans.get("error"):
                return True, None
            err = ans.get("code", "unknown") if isinstance(ans, dict) else "unknown"
            last_err = err
            if err in ("api.market.max_number_players_in_roster", "api.error.not_found"):
                return False, err  # error fatal, no reintentar
        except Exception as exc:
            last_err = str(exc)
        if attempt < retries:
            await asyncio.sleep(sleep_ms / 1000)
    return False, last_err


@app.post("/market/fichajes/fire", tags=["Fichajes"])
async def fire_fichajes(
    retries: int = Query(20, ge=1, le=50,
        description="Veces que se reintenta cada cláusula (default 20, como en bots de medianoche)"),
    sleep_ms: int = Query(200, ge=50, le=2000,
        description="Milisegundos entre intentos (default 200 ms)"),
    clear_on_success: bool = Query(True,
        description="Eliminar de la lista los jugadores fichados con éxito"),
    snipe_mode: bool = Query(False,
        description=(
            "Si True espera hasta los últimos `snipe_seconds` de cada cláusula antes de disparar. "
            "Requiere que cada item de la lista tenga `team_id` para obtener la fecha de expiración."
        )),
    snipe_seconds: int = Query(10, ge=3, le=120,
        description="En snipe_mode: segundos antes de la expiración en que disparamos (default 10)"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Dispara los fichajes de la lista de espera** con reintentos rápidos.

    **Modo estándar** (`snipe_mode=false`):
    - Ejecuta cada cláusula `retries` veces con `sleep_ms` ms entre intentos.
    - Ideal para lanzar justo a las 00:00 cuando Futmondo renueva las cláusulas.

    **Modo sniper** (`snipe_mode=true`):
    - Para cada jugador calcula cuándo expira su cláusula.
    - Espera hasta `snipe_seconds` antes de la expiración y entonces dispara.
    - Todos los jugadores se procesan **en paralelo**: cada uno espera de forma
      independiente y se lanza en su propio momento óptimo.
    - El rival no tiene tiempo de renovar ni de reaccionar.
    - Requiere que cada item de la lista tenga `team_id`.

    ```
    POST /market/fichajes/fire?snipe_mode=true&snipe_seconds=10
    ```
    """
    fichajes = _read_fichajes()
    if not fichajes:
        return {"status": "empty", "results": []}

    if snipe_mode:
        # ── Modo sniper: procesar todos en paralelo, cada uno en su momento ──
        async def _snipe_one(item: dict) -> dict:
            pid   = item["player_id"]
            slug  = item["player_slug"]
            price = item["price"]
            name  = item.get("name", pid)
            tid   = item.get("team_id")

            result: dict = {
                "name": name, "player_id": pid, "price": price,
                "mode": "snipe", "waited_seconds": 0,
                "attempts": 0, "status": "not_tried", "error": None,
            }

            # Obtener expiración de la cláusula
            expiry: datetime | None = None
            try:
                pdata = await client.get_player_data(pid)
                candidate = pdata
                if isinstance(pdata, dict) and "answer" in pdata:
                    ans = pdata["answer"]
                    candidate = ans if isinstance(ans, dict) else pdata
                expiry = _clause_expiry(candidate)
            except Exception:
                pass

            if expiry is None and tid:
                try:
                    roster_raw = await client.get_team_players(team_id=tid)
                    for p in _extract_list(roster_raw):
                        if _get_field(p, "_id", "id") == pid or str(p.get("slug")) == slug:
                            expiry = _clause_expiry(p)
                            break
                except Exception:
                    pass

            if expiry is None:
                result["status"] = "no_expiry"
                result["error"]  = "No se pudo obtener la fecha de expiración (añade team_id al item)"
                return result

            now          = datetime.now(timezone.utc)
            seconds_left = (expiry - now).total_seconds()

            if seconds_left <= 0:
                result["status"] = "expired"
                result["error"]  = "La cláusula ya ha expirado"
                return result

            wait = max(0.0, seconds_left - snipe_seconds)
            result["waited_seconds"]   = round(wait, 1)
            result["clause_expires"]   = expiry.isoformat()
            result["snipe_at"]         = (expiry - timedelta(seconds=snipe_seconds)).isoformat()

            if wait > 0:
                await asyncio.sleep(wait)

            ok, err = await _fire_one_clause(client, pid, slug, price, retries, sleep_ms)
            result["attempts"] = retries if not ok else 1
            if ok:
                result["status"] = "ok"
            else:
                result["status"] = "error"
                result["error"]  = err
            return result

        results_raw = await asyncio.gather(*[_snipe_one(item) for item in fichajes])
        results = list(results_raw)

    else:
        # ── Modo estándar: disparar uno a uno con reintentos ──────────────────
        results    = []
        for item in fichajes:
            pid   = item["player_id"]
            slug  = item["player_slug"]
            price = item["price"]
            name  = item.get("name", pid)

            result: dict = {
                "name": name, "player_id": pid, "price": price,
                "mode": "standard", "attempts": 0, "status": "not_tried", "error": None,
            }
            ok, err = await _fire_one_clause(client, pid, slug, price, retries, sleep_ms)
            result["attempts"] = retries if not ok else 1
            if ok:
                result["status"] = "ok"
            else:
                result["status"] = "exhausted" if err not in ("api.market.max_number_players_in_roster", "api.error.not_found") else "fatal_error"
                result["error"]  = err
            results.append(result)

    # Limpiar la lista de los fichados con éxito
    signed_ids = [r["player_id"] for r in results if r["status"] == "ok"]
    if clear_on_success and signed_ids:
        remaining = [c for c in fichajes if c["player_id"] not in signed_ids]
        _write_fichajes(remaining)

    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "mode":         "snipe" if snipe_mode else "standard",
        "snipe_seconds": snipe_seconds if snipe_mode else None,
        "fired":        len(fichajes),
        "signed":       ok_count,
        "failed":       len(fichajes) - ok_count,
        "retries_used": retries,
        "sleep_ms":     sleep_ms,
        "results":      results,
    }


# ══════════════════════════════════════════════════════════════════════════════
# AUTOGESTIÓN — agente autónomo de gestión continua del equipo
# ══════════════════════════════════════════════════════════════════════════════

_AUTOGESTIONE_CONFIG_FILE = Path(__file__).parent / "autogestione.json"
_autogestione_task: asyncio.Task | None = None
_autogestione_log:  list[dict]          = []   # ring buffer de los últimos eventos
_MAX_LOG             = 500


class AutoGestioneConfig(BaseModel):
    enabled:               bool  = Field(False,  description="Activar/desactivar (persiste en disco)")
    check_interval_minutes: int  = Field(30, ge=1, le=1440, description="Minutos entre ciclos completos")
    # ── Ventas — desactivadas: esperamos a que nos roben ─────────────────────
    auto_sell:         bool  = Field(False, description="Vender los peores jugadores (desactivado: esperamos a que nos roben)")
    sell_bottom_pct:   float = Field(0.20, ge=0.0, le=1.0, description="% inferior de la plantilla a vender por ciclo")
    sell_min_avg:      float = Field(MIN_AVG_PER_PLAYER, ge=0.0, description="Proteger jugadores con avg ≥ este valor")
    # ── Robos por cláusula ─────────────────────────────────────────────────
    auto_attack:      bool  = Field(True,  description="Robar jugadores rivales automáticamente")
    steal_top:        int   = Field(2, ge=0, le=5, description="Máximo de robos por ciclo")
    steal_min_avg:    float = Field(MIN_AVG_PER_PLAYER, ge=0.0, description="Media mínima del objetivo (default: 90pts/11j ≈ 8.18)")
    steal_max_clause: float = Field(0.0,  ge=0.0, description="Cláusula máxima a pagar (0 = sin límite)")
    prefer_gk:        bool  = Field(True,  description="Priorizar portero único rival (máximo daño)")
    # ── Mercado ────────────────────────────────────────────────────────────
    cancel_expiring: bool  = Field(True,  description="Cancelar listings que expiran sin puja")
    cancel_hours:    float = Field(3.0, ge=0.5, le=24.0, description="Horas de margen para cancelar sin puja")
    auto_reprice:    bool  = Field(True,  description="Re-preciar listings al precio correcto (valor × 1.5)")
    # ── Sniper de fichajes ─────────────────────────────────────────────────
    auto_snipe:      bool = Field(True,  description="Disparar la lista de fichajes en el último segundo")
    snipe_seconds:   int  = Field(10,  ge=3, le=120, description="Segundos antes de expirar en que se dispara")
    snipe_retries:   int  = Field(5,   ge=1, le=20,  description="Reintentos tras el disparo del snipe")
    # ── Control de saldo ────────────────────────────────────────────────────
    min_cash_buffer: float = Field(5_000_000, ge=0,
        description="Saldo mínimo a mantener siempre — nunca gastar si dejaría el saldo por debajo de esta cifra")
    # ── Relleno de huecos urgente ──────────────────────────────────────────
    fill_on_stolen:  bool  = Field(True, description="Si hay huecos vacíos (nos robaron), fichar aunque auto_attack esté desactivado")
    fill_fallback_min_avg: float = Field(4.0, ge=0.0,
        description="Media mínima de emergencia para rellenar huecos si no hay candidatos con steal_min_avg")
    # ── Alineación automática ──────────────────────────────────────────────
    auto_lineup:     bool  = Field(True, description="Calcular y aplicar alineación óptima en cada ciclo")
    target_pts_jornada: float = Field(float(TARGET_PTS_JORNADA), ge=1.0,
        description="Objetivo de puntos/jornada del XI para la alineación óptima")
    # ── Estrategia de estrella ─────────────────────────────────────────────
    star_player_id:    str | None = Field(None, description="ID del jugador estrella — intocable, nunca se vende")
    auto_protect_star: bool       = Field(True,  description="Proteger automáticamente al jugador más valioso del equipo")
    star_max_clause:   float      = Field(0.0, ge=0.0,
        description="Presupuesto máximo para fichar una estrella rival (0 = usar steal_max_clause normal). "
                    "Permite superar steal_max_clause para objetivos de alto valor")


# ── Persistencia de config ─────────────────────────────────────────────────────

def _ag_load_config() -> AutoGestioneConfig:
    if _AUTOGESTIONE_CONFIG_FILE.exists():
        try:
            return AutoGestioneConfig(**json.loads(_AUTOGESTIONE_CONFIG_FILE.read_text()))
        except Exception:
            pass
    return AutoGestioneConfig()


def _ag_save_config(cfg: AutoGestioneConfig) -> None:
    _AUTOGESTIONE_CONFIG_FILE.write_text(cfg.model_dump_json(indent=2))


# ── Saldo disponible ──────────────────────────────────────────────────────────

async def _get_cash(client: FutmondoClient) -> float:
    """
    Devuelve el saldo disponible en la cuenta.
    El presupuesto real está en /1/userteam/information (campo 'budget').
    """
    try:
        raw = await client.get_userteam_info()
        ans = raw.get("answer", raw) if isinstance(raw, dict) else {}
        if isinstance(ans, dict):
            for key in ("budget", "money", "balance", "cash", "teamBudget"):
                v = ans.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
    except Exception:
        pass
    # Fallback: /user/information (no tiene campos monetarios, pero por si acaso)
    try:
        raw = await client.get_user_info()
        ans = raw.get("answer", raw) if isinstance(raw, dict) else {}
        if isinstance(ans, dict):
            for key in ("money", "balance", "cash", "coins", "budget", "teamBudget"):
                v = ans.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
    except Exception:
        pass
    # No se puede obtener el saldo de la API — estimar con:
    # cash ≈ budget_inicial + puntos×150k - sum(buyPrices plantilla actual)
    # Esta estimación ignora ventas previas, pero evita intentar compras imposibles.
    try:
        team_raw = await client.get_team_players()
        squad = _extract_list(team_raw)
        pts_raw = await client.get_championship_info()
        pts_inner = pts_raw.get("answer", pts_raw) if isinstance(pts_raw, dict) else {}
        my_pts = 0.0
        teams = pts_inner.get("teams", []) if isinstance(pts_inner, dict) else []
        for t in teams:
            if (t.get("teamid") or t.get("id")) == client.user_team_id:
                my_pts = float(t.get("points", 0))
                break
        total_invested = sum(float(p.get("buyPrice") or 0) for p in squad)
        estimated = STARTING_BUDGET + my_pts * MONEY_PER_POINT - total_invested
        if estimated <= 0:
            # La estimación ignora ventas pasadas y puede ser negativa en equipos
            # con historial de transacciones. Devolvemos un valor alto para no
            # bloquear al agente: la propia API de Futmondo rechazará si no hay fondos.
            return 999_000_000.0
        return estimated
    except Exception:
        pass
    return 999_000_000.0


# ── Log ───────────────────────────────────────────────────────────────────────

def _ag_log(action: str, detail: str, status: str = "info", data: dict | None = None) -> None:
    global _autogestione_log
    entry: dict = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "action": action,
        "detail": detail,
        "status": status,
    }
    if data:
        entry["data"] = data
    _autogestione_log.append(entry)
    if len(_autogestione_log) > _MAX_LOG:
        _autogestione_log = _autogestione_log[-_MAX_LOG:]


# ── Lógica de un ciclo completo ───────────────────────────────────────────────

async def _ag_cycle(client: FutmondoClient, cfg: AutoGestioneConfig) -> None:
    _ag_log("cycle_start", f"Ciclo iniciado — intervalo {cfg.check_interval_minutes} min")

    # ── 1. Cancelar listings expirados sin puja ───────────────────────────────
    if cfg.cancel_expiring:
        try:
            raw    = await client.get_my_players_in_market()
            listed = _extract_list(raw)
            for s in [_listing_status(p, 0.0) for p in listed]:
                if s["num_bids"] == 0 and s["hours_left"] is not None and s["hours_left"] < cfg.cancel_hours:
                    pid = s["_id"]
                    if pid:
                        try:
                            await client.remove_player_from_market(pid)
                            _ag_log("cancel_listing", f"{s['name']} ({s['hours_left']}h restantes)", "ok")
                        except Exception as e:
                            _ag_log("cancel_listing", f"Error con {s['name']}: {e}", "error")
        except Exception as e:
            _ag_log("cancel_expiring", f"Error obteniendo listings: {e}", "error")

    # ── 2. Re-preciar listings ────────────────────────────────────────────────
    if cfg.auto_reprice:
        try:
            raw    = await client.get_my_players_in_market()
            listed = _extract_list(raw)
            for p in listed:
                pid       = _get_field(p, "_id", "id")
                slug      = p.get("slug")
                value     = float(_get_field(p, "value", "marketValue") or 0)
                old_price = float(_get_field(p, "price", "sellPrice") or 0)
                new_price = max(1_000_000, _compute_sell_price(value, old_price))
                if pid and slug and new_price != int(old_price):
                    try:
                        await client.remove_player_from_market(pid)
                        await client.set_player_in_market(pid, str(slug), new_price)
                        _ag_log("reprice", f"{p.get('name','?')}: {int(old_price):,} → {new_price:,}", "ok")
                    except Exception as e:
                        _ag_log("reprice", f"Error con {p.get('name','?')}: {e}", "error")
        except Exception as e:
            _ag_log("reprice", f"Error general: {e}", "error")

    # ── 3. Auto-venta ─────────────────────────────────────────────────────────
    if cfg.auto_sell:
        try:
            team_data  = await client.get_team_players()
            my_players = _extract_list(team_data)
            active     = [p for p in my_players if not p.get("market")]
            gk_count   = sum(1 for p in active if p.get("role", "").lower() == "portero")

            # Determinar la estrella protegida: star_player_id explícito o el más caro del equipo
            star_id: str | None = cfg.star_player_id
            if not star_id and cfg.auto_protect_star and active:
                star_id = _get_field(
                    max(active, key=lambda p: float(_get_field(p, "value", "marketValue") or 0)),
                    "_id", "id",
                )

            sorted_by_eff = sorted(active, key=lambda p: float(p.get("change") or 0))
            sell_count    = max(1, int(len(sorted_by_eff) * cfg.sell_bottom_pct))
            candidates    = [
                p for p in sorted_by_eff
                if not (p.get("role", "").lower() == "portero" and gk_count <= 1)
                and (cfg.sell_min_avg == 0 or _avg_per_game(p) < cfg.sell_min_avg)
                and _get_field(p, "_id", "id") != star_id   # ★ nunca vender la estrella
            ][:sell_count]

            for p in candidates:
                pid   = _get_field(p, "_id", "id")
                slug  = p.get("slug")
                value = float(_get_field(p, "value", "marketValue") or 0)
                buy_p = float(p.get("buyPrice") or 0)
                price = _compute_sell_price(value, buy_p)
                # Intentar venta directa primero
                try:
                    direct = await client.direct_sell(pid, str(slug))
                    d_ans  = direct.get("answer", {}) if isinstance(direct, dict) else {}
                    if isinstance(d_ans, dict) and not d_ans.get("error"):
                        _ag_log("sell", f"Venta directa: {p.get('name','?')} avg={_avg_per_game(p):.2f}/j", "ok")
                        continue
                except Exception:
                    pass
                # Fallback: listing en el mercado
                try:
                    await client.set_player_in_market(pid, str(slug), price)
                    _ag_log("sell", f"Mercado: {p.get('name','?')} a {price:,}", "ok")
                except Exception as e:
                    _ag_log("sell", f"Error vendiendo {p.get('name','?')}: {e}", "error")
        except Exception as e:
            _ag_log("auto_sell", f"Error general: {e}", "error")

    # ── 4. Auto-ataque (robo por cláusula) ────────────────────────────────────
    if cfg.auto_attack and cfg.steal_top > 0:
        try:
            my_raw     = await client.get_team_players()
            my_players = _extract_list(my_raw)
            free_slots = max(0, MAX_ROSTER_SIZE - len(my_players))

            if free_slots == 0:
                _ag_log("auto_attack", "Roster lleno — sin huecos para robar", "skip")
            else:
                my_ids     = {_get_field(p, "_id", "id") for p in my_players}
                champ_raw  = await client.get_championship_info()
                inner      = champ_raw.get("answer", champ_raw) if isinstance(champ_raw, dict) else {}
                rival_teams = [
                    t for t in (inner.get("teams", []) if isinstance(inner, dict) else [])
                    if (t.get("teamid") or t.get("id")) != client.user_team_id
                ]
                now = datetime.now(timezone.utc)

                async def _fetch_r(t):
                    tid = t.get("teamid") or t.get("id")
                    try:
                        return t, _extract_list(await client.get_team_players(team_id=tid))
                    except Exception:
                        return t, []

                all_rosters = await asyncio.gather(*[_fetch_r(t) for t in rival_teams])

                steal_pool: list[tuple] = []
                for team_info, roster in all_rosters:
                    team_name     = team_info.get("teamname") or team_info.get("name", "?")
                    last_acc      = team_info.get("lastAccess") or ""
                    hours_offline = None
                    if last_acc:
                        try:
                            la = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
                            hours_offline = (now - la).total_seconds() / 3600
                        except Exception:
                            pass
                    confiado    = (hours_offline or 0) > 12
                    only_one_gk = sum(1 for p in roster if p.get("role", "").lower() == "portero") == 1

                    for p in roster:
                        pid = _get_field(p, "_id", "id")
                        if pid in my_ids:
                            continue
                        role        = p.get("role", "").lower()
                        avg         = _avg_per_game(p)
                        c_price     = _clause_price(p)
                        cl          = p.get("clause") or {}
                        transferred = cl.get("transferred", False) if isinstance(cl, dict) else False
                        c_hours     = _clause_hours_left(p)

                        if transferred or c_price <= 0 or (c_hours is not None and c_hours <= 0):
                            continue
                        # Descartar jugadores lesionados o sancionados del rival
                        if not _is_available(p):
                            continue
                        if avg < cfg.steal_min_avg:
                            continue

                        # Determinar límite de cláusula: los jugadores de alto valor pueden usar star_max_clause
                        is_star_target = (
                            cfg.star_max_clause > 0
                            and c_price > cfg.steal_max_clause
                            and c_price <= cfg.star_max_clause
                        )
                        if cfg.steal_max_clause > 0 and c_price > cfg.steal_max_clause and not is_star_target:
                            continue

                        # Prioridad = máximos puntos (avg) > estrella > portero único > confiado
                        # La cláusula solo desempata (no penaliza fuerte)
                        prio = avg * 100   # avg es el factor dominante (máx puntos)
                        if is_star_target:
                            prio += 5000   # ★ boost máximo para candidatos a estrella
                        if role == "portero" and only_one_gk and cfg.prefer_gk:
                            prio += 1000
                        if confiado:
                            prio += 20
                        prio -= c_price / 10_000_000  # desempate suave por precio
                        steal_pool.append((prio, pid, p.get("slug"), int(c_price), p.get("name", "?"), role))

                steal_pool.sort(key=lambda x: -x[0])
                stolen = 0
                cash = await _get_cash(client)
                _ag_log("budget", f"Saldo disponible: {cash:,.0f} | Reserva mínima: {cfg.min_cash_buffer:,.0f}", "info")
                for _, pid, slug, c_price, name, role in steal_pool[:cfg.steal_top]:
                    if stolen >= free_slots:
                        break
                    # Control de saldo: no robar si deja el saldo por debajo del buffer
                    if cash - c_price < cfg.min_cash_buffer:
                        # Añadir a la cola de snipe para disparar en el último segundo de la cláusula
                        existing = {f["player_id"] for f in _read_fichajes()}
                        if pid not in existing:
                            _write_fichajes(_read_fichajes() + [{
                                "player_id": pid, "player_slug": str(slug),
                                "price": c_price, "name": name,
                                "team_id": None,
                            }])
                            _ag_log("steal", f"COLA SNIPE: {name} ({c_price:,}) — saldo insuficiente, "
                                    f"programado para disparo en expiración", "warning")
                        else:
                            _ag_log("steal", f"SKIP {name} — ya en cola snipe", "info")
                        continue
                    try:
                        resp = await client.pay_player_clause(pid, str(slug), c_price)
                        ans  = resp.get("answer", {}) if isinstance(resp, dict) else {}
                        if isinstance(ans, dict) and ans.get("error"):
                            err = ans.get("code", "unknown")
                            _ag_log("steal", f"Error {name}: {err}", "error")
                            if err == "api.market.max_number_players_in_roster":
                                break
                        else:
                            _ag_log("steal", f"¡ROBADO! {name} ({role}) por {c_price:,}", "ok",
                                    {"clause_paid": c_price, "player": name, "cash_after": cash - c_price})
                            cash -= c_price   # actualizar saldo local para siguientes iteraciones
                            stolen += 1
                    except Exception as e:
                        _ag_log("steal", f"Excepción con {name}: {e}", "error")
        except Exception as e:
            _ag_log("auto_attack", f"Error general: {e}", "error")

    # ── 4b. RELLENO URGENTE: si quedan huecos, bajar el umbral ───────────────
    # Se ejecuta aunque auto_attack=False si fill_on_stolen=True
    if cfg.fill_on_stolen:
        try:
            my_raw_check = await client.get_team_players()
            my_players_check = _extract_list(my_raw_check)
            free_slots_now = max(0, MAX_ROSTER_SIZE - len(my_players_check))

            if free_slots_now > 0:
                _ag_log("fill_gap", f"HAY {free_slots_now} HUECO(S) — iniciando relleno urgente", "warning")
                my_ids_check = {_get_field(p, "_id", "id") for p in my_players_check}

                # Calcular posiciones que nos faltan para una XI completo
                pos_in_squad = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
                for p in my_players_check:
                    pos = _normalize_pos(p)
                    if pos in pos_in_squad:
                        pos_in_squad[pos] += 1
                # Qué posiciones son más urgentes (menos representadas)
                pos_priority = sorted(pos_in_squad, key=lambda pp: pos_in_squad[pp])

                champ_raw_f = await client.get_championship_info()
                inner_f = champ_raw_f.get("answer", champ_raw_f) if isinstance(champ_raw_f, dict) else {}
                rival_teams_f = [
                    t for t in (inner_f.get("teams", []) if isinstance(inner_f, dict) else [])
                    if (t.get("teamid") or t.get("id")) != client.user_team_id
                ]

                async def _fetch_rf(t):
                    tid = t.get("teamid") or t.get("id")
                    try:
                        return t, _extract_list(await client.get_team_players(team_id=tid))
                    except Exception:
                        return t, []

                now_f = datetime.now(timezone.utc)
                all_rosters_f = await asyncio.gather(*[_fetch_rf(t) for t in rival_teams_f])

                fill_pool: list[tuple] = []
                for team_info_f, roster_f in all_rosters_f:
                    team_name_f = team_info_f.get("teamname") or team_info_f.get("name", "?")
                    last_acc_f  = team_info_f.get("lastAccess") or ""
                    h_offline_f = None
                    if last_acc_f:
                        try:
                            la_f = datetime.fromisoformat(last_acc_f.replace("Z", "+00:00"))
                            h_offline_f = (now_f - la_f).total_seconds() / 3600
                        except Exception:
                            pass
                    confiado_f    = (h_offline_f or 0) > 12
                    only_one_gk_f = sum(1 for p in roster_f if p.get("role", "").lower() == "portero") == 1

                    for p in roster_f:
                        pid_f = _get_field(p, "_id", "id")
                        if pid_f in my_ids_check:
                            continue
                        avg_f     = _avg_per_game(p)
                        c_price_f = _clause_price(p)
                        cl_f      = p.get("clause") or {}
                        if cl_f.get("transferred", False) if isinstance(cl_f, dict) else False:
                            continue
                        c_hours_f = _clause_hours_left(p)
                        if c_price_f <= 0 or (c_hours_f is not None and c_hours_f <= 0):
                            continue
                        # No fichar lesionados/sancionados aunque haya urgencia
                        if not _is_available(p):
                            continue
                        if avg_f < cfg.fill_fallback_min_avg:
                            continue  # descarta jugadores muy malos incluso en urgencia
                        if cfg.steal_max_clause > 0 and c_price_f > cfg.steal_max_clause:
                            continue

                        role_f  = p.get("role", "").lower()
                        is_gk_f = role_f == "portero"
                        norm_pos_f = _normalize_pos(p)
                        # Bonus posicional: rellenamos la posición más escasa primero
                        pos_bonus = 0
                        if norm_pos_f in pos_priority:
                            pos_bonus = (len(pos_priority) - pos_priority.index(norm_pos_f)) * 50
                        # Prioridad = máximos puntos (avg) > posición urgente > portero único
                        prio_f = avg_f * 100 + pos_bonus
                        if is_gk_f and only_one_gk_f and cfg.prefer_gk:
                            prio_f += 1000
                        if confiado_f:
                            prio_f += 20
                        prio_f -= c_price_f / 10_000_000
                        fill_pool.append((prio_f, pid_f, p.get("slug"), int(c_price_f),
                                          p.get("name", "?"), role_f))

                fill_pool.sort(key=lambda x: -x[0])
                filled = 0
                cash_fill = await _get_cash(client)
                for _, pid_f, slug_f, c_price_f, name_f, role_f in fill_pool:
                    if filled >= free_slots_now:
                        break
                    # Control de saldo también en relleno urgente
                    if cash_fill - c_price_f < cfg.min_cash_buffer:
                        existing_f = {f["player_id"] for f in _read_fichajes()}
                        if pid_f not in existing_f:
                            _write_fichajes(_read_fichajes() + [{
                                "player_id": pid_f, "player_slug": str(slug_f),
                                "price": c_price_f, "name": name_f,
                                "team_id": None,
                            }])
                            _ag_log("fill_gap", f"COLA SNIPE: {name_f} ({c_price_f:,}) — sin saldo, "
                                    f"programado para disparo en expiración", "warning")
                        else:
                            _ag_log("fill_gap", f"SKIP {name_f} — ya en cola snipe", "info")
                        continue
                    try:
                        resp_f = await client.pay_player_clause(pid_f, str(slug_f), c_price_f)
                        ans_f  = resp_f.get("answer", {}) if isinstance(resp_f, dict) else {}
                        if isinstance(ans_f, dict) and ans_f.get("error"):
                            err_f = ans_f.get("code", "unknown")
                            _ag_log("fill_gap", f"Error rellenando con {name_f}: {err_f}", "error")
                            if err_f == "api.market.max_number_players_in_roster":
                                break
                        else:
                            _ag_log("fill_gap", f"HUECO RELLENADO: {name_f} ({role_f}) por {c_price_f:,}", "ok",
                                    {"clause_paid": c_price_f, "player": name_f, "role": role_f,
                                     "cash_after": cash_fill - c_price_f})
                            cash_fill -= c_price_f
                            filled += 1
                    except Exception as e_f:
                        _ag_log("fill_gap", f"Excepción rellenando {name_f}: {e_f}", "error")

                if filled == 0 and free_slots_now > 0:
                    _ag_log("fill_gap",
                            f"Sin candidatos válidos para rellenar {free_slots_now} hueco(s) "
                            f"(avg_min={cfg.fill_fallback_min_avg}). Roster incompleto.", "warning")
        except Exception as e:
            _ag_log("fill_on_stolen", f"Error en relleno urgente: {e}", "error")

    # ── 4c. COMPRA DEL MERCADO ABIERTO ───────────────────────────────────────
    # Si hay huecos y hay jugadores buenos en el mercado, comprarlos por bid
    if cfg.fill_on_stolen or cfg.auto_attack:
        try:
            my_raw_m = await client.get_team_players()
            my_players_m = _extract_list(my_raw_m)
            free_m = max(0, MAX_ROSTER_SIZE - len(my_players_m))
            if free_m > 0:
                market_raw = await client.get_market()
                market_players = _extract_list(market_raw)
                my_ids_m = {_get_field(p, "_id", "id") for p in my_players_m}
                cash_m = await _get_cash(client)

                # Candidatos del mercado con avg >= fill_fallback_min_avg
                mkt_candidates = []
                for p in market_players:
                    pid_m = _get_field(p, "_id", "id")
                    if pid_m in my_ids_m:
                        continue
                    avg_m   = _last5_avg(p)   # priorizar racha
                    price_m = float(p.get("price") or p.get("value") or 0)
                    if avg_m < cfg.fill_fallback_min_avg or price_m <= 0:
                        continue
                    if cfg.steal_max_clause > 0 and price_m > cfg.steal_max_clause:
                        continue
                    if not _is_available(p):
                        continue
                    mkt_candidates.append((avg_m, pid_m, p.get("slug"), int(price_m), p.get("name", "?")))

                mkt_candidates.sort(key=lambda x: -x[0])
                mkt_bought = 0
                for avg_m, pid_m, slug_m, price_m, name_m in mkt_candidates:
                    if mkt_bought >= free_m:
                        break
                    if cash_m - price_m < cfg.min_cash_buffer:
                        _ag_log("market_buy", f"SKIP {name_m} ({price_m:,}) — saldo insuficiente", "warning")
                        continue
                    try:
                        resp_m = await client.set_bid(pid_m, str(slug_m), price_m)
                        ans_m  = resp_m.get("answer", {}) if isinstance(resp_m, dict) else {}
                        if isinstance(ans_m, dict) and ans_m.get("error"):
                            _ag_log("market_buy", f"Error comprando {name_m}: {ans_m.get('code')}", "error")
                        else:
                            _ag_log("market_buy", f"COMPRADO del mercado: {name_m} last5={avg_m:.2f} por {price_m:,}", "ok")
                            cash_m -= price_m
                            mkt_bought += 1
                    except Exception as e_m:
                        _ag_log("market_buy", f"Excepción comprando {name_m}: {e_m}", "error")
        except Exception as e:
            _ag_log("market_buy", f"Error general en compra de mercado: {e}", "error")

    # ── 5. Monitor de anomalías y cláusulas ──────────────────────────────────
    try:
        report = await _collect_anomalies(client)
        for alert in report["alerts"]:
            if alert["level"] in ("CRITICO", "ALTO"):
                _ag_log(
                    f"anomaly_{alert['type'].lower()}",
                    alert["detail"],
                    "error" if alert["level"] == "CRITICO" else "warning",
                    {"level": alert["level"], "action": alert["action"],
                     "player": alert.get("player")},
                )
        if report["summary"]["CRITICO"] == 0 and report["summary"]["ALTO"] == 0:
            _ag_log("monitor_ok", "Sin anomalías críticas en este ciclo", "info")
    except Exception as e:
        _ag_log("monitor_anomalies", f"Error en monitor: {e}", "error")

    # ── 5b. Monitor de sancionados/lesionados PROPIOS + robo de refuerzo ─────
    try:
        my_raw_s = await client.get_team_players()
        my_roster_s = _extract_list(my_raw_s)
        my_ids_s = {_get_field(p, "_id", "id") for p in my_roster_s}

        unavailable_own: list[dict] = []
        for p in my_roster_s:
            if not _is_available(p):
                st_val = None
                for field in ("status", "playerStatus", "injuryStatus", "playeractive"):
                    v = p.get(field)
                    if v is not None:
                        st_val = v
                        break
                reason = "sancionado" if st_val == 3 or str(st_val).lower() in ("suspended", "sancionado") else "lesionado/no disponible"
                unavailable_own.append({"name": p.get("name", "?"), "role": p.get("role", "?"), "reason": reason, "status": st_val})

        available_count = len(my_roster_s) - len(unavailable_own)

        if unavailable_own:
            names_str = ", ".join(f"{u['name']} ({u['reason']})" for u in unavailable_own)
            _ag_log(
                "own_unavailable",
                f"{len(unavailable_own)} jugador(es) no disponible(s): {names_str} | XI efectivo: {min(available_count, 11)}/11",
                "warning" if available_count >= 11 else "error",
                {"unavailable": unavailable_own, "available_count": available_count},
            )
        else:
            _ag_log("own_unavailable", "Todos los jugadores propios disponibles", "info")

        # Si el XI efectivo queda por debajo de 11, intentar robar refuerzos
        # aunque el roster no esté vacío (los sancionados ocupan plaza pero no juegan)
        effective_gap = max(0, 11 - available_count)
        if effective_gap > 0 and cfg.auto_attack:
            _ag_log("reinforce", f"XI efectivo {available_count}/11 — buscando {effective_gap} refuerzo(s)", "warning")
            try:
                champ_raw_r   = await client.get_championship_info()
                inner_r       = champ_raw_r.get("answer", champ_raw_r) if isinstance(champ_raw_r, dict) else {}
                rival_teams_r = [
                    t for t in (inner_r.get("teams", []) if isinstance(inner_r, dict) else [])
                    if (t.get("teamid") or t.get("id")) != client.user_team_id
                ]
                now_r = datetime.now(timezone.utc)

                async def _fetch_r2(t):
                    tid = t.get("teamid") or t.get("id")
                    try:
                        return t, _extract_list(await client.get_team_players(team_id=tid))
                    except Exception:
                        return t, []

                rosters_r = await asyncio.gather(*[_fetch_r2(t) for t in rival_teams_r])
                reinforce_pool: list[tuple] = []
                for team_info_r, roster_r in rosters_r:
                    for p in roster_r:
                        pid_r = _get_field(p, "_id", "id")
                        if pid_r in my_ids_s:
                            continue
                        if not _is_available(p):
                            continue
                        avg_r   = _avg_per_game(p)
                        c_price_r = _clause_price(p)
                        cl_r    = p.get("clause") or {}
                        if (cl_r.get("transferred", False) if isinstance(cl_r, dict) else False):
                            continue
                        if c_price_r <= 0:
                            continue
                        if avg_r < cfg.steal_min_avg:
                            continue
                        if cfg.steal_max_clause > 0 and c_price_r > cfg.steal_max_clause:
                            continue
                        reinforce_pool.append((avg_r, pid_r, p.get("slug"), int(c_price_r), p.get("name", "?"), p.get("role", "?")))

                reinforce_pool.sort(key=lambda x: -x[0])
                cash_r = await _get_cash(client)
                signed = 0
                for avg_r, pid_r, slug_r, c_price_r, name_r, role_r in reinforce_pool[:effective_gap]:
                    if cash_r - c_price_r < cfg.min_cash_buffer:
                        _ag_log("reinforce", f"SKIP {name_r} ({c_price_r:,}) — saldo insuficiente", "warning")
                        continue
                    try:
                        resp_r = await client.pay_player_clause(pid_r, str(slug_r), c_price_r)
                        ans_r  = resp_r.get("answer", {}) if isinstance(resp_r, dict) else {}
                        if isinstance(ans_r, dict) and ans_r.get("error"):
                            _ag_log("reinforce", f"Error fichando refuerzo {name_r}: {ans_r.get('code')}", "error")
                        else:
                            _ag_log("reinforce", f"REFUERZO FICHADO: {name_r} ({role_r}) avg={avg_r:.1f} por {c_price_r:,}", "ok",
                                    {"player": name_r, "avg": avg_r, "clause_paid": c_price_r, "cash_after": cash_r - c_price_r})
                            cash_r -= c_price_r
                            signed += 1
                    except Exception as e_r:
                        _ag_log("reinforce", f"Excepción refuerzo {name_r}: {e_r}", "error")
                if signed == 0:
                    _ag_log("reinforce", "Sin candidatos válidos con saldo suficiente para reforzar", "warning")
            except Exception as e_ref:
                _ag_log("reinforce", f"Error buscando refuerzos: {e_ref}", "error")
    except Exception as e:
        _ag_log("own_unavailable", f"Error comprobando disponibilidad propia: {e}", "error")

    # ── 6. Alineación óptima ──────────────────────────────────────────────────
    if cfg.auto_lineup:
        try:
            team_data_lu = await client.get_team_players()
            players_lu   = _extract_list(team_data_lu)
            if players_lu:
                xi_lu   = _best_lineup_analysis(players_lu, target_apg=cfg.target_pts_jornada)
                proj_lu = xi_lu.get("projected_pts_jornada", 0)
                gap_lu  = xi_lu.get("gap_to_target", 0)
                form_lu = xi_lu.get("formation", "?")
                starters_names = ", ".join(
                    s.get("name", "?") for s in xi_lu.get("starters", [])
                )
                _ag_log(
                    "lineup",
                    f"XI óptimo [{form_lu}] → {proj_lu:.1f} pts/j "
                    f"({'GAP ' + str(round(gap_lu, 1)) if gap_lu > 0 else 'OBJETIVO ALCANZADO'}) | "
                    f"{starters_names}",
                    "ok" if proj_lu >= cfg.target_pts_jornada else "warning",
                    {
                        "formation": form_lu,
                        "projected_pts_jornada": proj_lu,
                        "gap_to_target": gap_lu,
                        "target": cfg.target_pts_jornada,
                        "starters": [s.get("name") for s in xi_lu.get("starters", [])],
                        "bench":    [s.get("name") for s in xi_lu.get("bench", [])],
                    },
                )
                # Intentar aplicar la alineación en Futmondo si tenemos el endpoint
                try:
                    starter_slugs = [
                        str(s.get("slug")) for s in xi_lu.get("starters", [])
                        if s.get("slug") is not None
                    ]
                    if starter_slugs:
                        resp_lu = await client.set_lineup(starter_slugs)
                        ans_lu  = resp_lu.get("answer", {}) if isinstance(resp_lu, dict) else {}
                        if isinstance(ans_lu, dict) and ans_lu.get("error"):
                            _ag_log("lineup_apply", f"API rechazó la alineación: {ans_lu.get('code')}", "warning")
                        else:
                            _ag_log("lineup_apply", f"Alineación [{form_lu}] aplicada en Futmondo", "ok")
                except Exception as e_lu:
                    # El endpoint puede no estar disponible; solo loggear sin parar el ciclo
                    _ag_log("lineup_apply", f"No se pudo aplicar alineación en Futmondo: {e_lu}", "info")
        except Exception as e:
            _ag_log("lineup", f"Error calculando alineación: {e}", "error")

    _ag_log("cycle_end", "Ciclo completado")


# ── Sniper de fichajes (tarea paralela continua) ───────────────────────────────

async def _execute_snipe_ag(
    client: FutmondoClient,
    item: dict,
    expiry: datetime,
    cfg: AutoGestioneConfig,
) -> None:
    """Espera hasta snipe_seconds antes del vencimiento y dispara la cláusula."""
    pid   = item["player_id"]
    slug  = item["player_slug"]
    price = item["price"]
    name  = item.get("name", pid)

    wait = max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds() - cfg.snipe_seconds)
    _ag_log("snipe_scheduled",
            f"{name} — disparo programado en {wait / 3600:.2f}h "
            f"({cfg.snipe_seconds}s antes de la expiración)",
            "info", {"snipe_at": (expiry - timedelta(seconds=cfg.snipe_seconds)).isoformat()})

    if wait > 0:
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            _ag_log("snipe_cancelled", f"{name} — cancelado antes de disparar", "warning")
            return

    ok, err = await _fire_one_clause(client, pid, slug, price, cfg.snipe_retries, 200)
    if ok:
        _ag_log("snipe_fired", f"¡ROBADO en el último segundo! {name} por {price:,}", "ok",
                {"clause_paid": price, "player": name})
        _write_fichajes([c for c in _read_fichajes() if c["player_id"] != pid])
    else:
        _ag_log("snipe_failed", f"Snipe fallido — {name}: {err}", "error")


async def _sniper_watcher(client: FutmondoClient, cfg: AutoGestioneConfig) -> None:
    """
    Tarea paralela que vigila la lista de fichajes y programa un snipe por cada
    jugador con fecha de expiración de cláusula conocida.
    Comprueba la lista cada minuto y evita programar el mismo jugador dos veces.
    Las consultas a la API se hacen en paralelo para no bloquear el bucle.
    """
    scheduled: dict[str, asyncio.Task] = {}

    async def _resolve_expiry(item: dict) -> tuple[str, "datetime | None"]:
        """Resuelve la fecha de expiración de cláusula para un jugador (con timeout propio)."""
        pid = item["player_id"]
        expiry: datetime | None = None
        try:
            pdata = await asyncio.wait_for(client.get_player_data(pid), timeout=10.0)
            candidate = pdata
            if isinstance(pdata, dict) and "answer" in pdata:
                ans = pdata["answer"]
                candidate = ans if isinstance(ans, dict) else pdata
            expiry = _clause_expiry(candidate)
        except Exception:
            pass

        if expiry is None and item.get("team_id"):
            try:
                roster_raw = await asyncio.wait_for(
                    client.get_team_players(team_id=item["team_id"]), timeout=10.0
                )
                for p in _extract_list(roster_raw):
                    if _get_field(p, "_id", "id") == pid or str(p.get("slug")) == item["player_slug"]:
                        expiry = _clause_expiry(p)
                        break
            except Exception:
                pass

        return pid, expiry

    while True:
        try:
            fichajes = _read_fichajes()
            pending = [item for item in fichajes if item["player_id"] not in scheduled or scheduled[item["player_id"]].done()]

            if pending:
                results = await asyncio.gather(*[_resolve_expiry(item) for item in pending], return_exceptions=True)
                item_by_pid = {item["player_id"]: item for item in pending}
                for res in results:
                    if isinstance(res, Exception):
                        continue
                    pid, expiry = res
                    if expiry:
                        seconds_left = (expiry - datetime.now(timezone.utc)).total_seconds()
                        if seconds_left > 0:
                            task = asyncio.create_task(_execute_snipe_ag(client, item_by_pid[pid], expiry, cfg))
                            scheduled[pid] = task

            # Limpiar tareas terminadas
            for pid in [k for k, v in scheduled.items() if v.done()]:
                del scheduled[pid]

        except asyncio.CancelledError:
            for t in scheduled.values():
                t.cancel()
            raise
        except Exception as e:
            _ag_log("sniper_watcher", f"Error: {e}", "error")

        await asyncio.sleep(60)


# ── Loop principal ────────────────────────────────────────────────────────────

async def _autogestione_loop(client: FutmondoClient, cfg: AutoGestioneConfig) -> None:
    _ag_log("started", f"Autogestión activa — ciclo cada {cfg.check_interval_minutes} min")

    sniper_task: asyncio.Task | None = None
    if cfg.auto_snipe:
        sniper_task = asyncio.create_task(_sniper_watcher(client, cfg))

    try:
        while True:
            await _ag_cycle(client, cfg)
            cfg = _ag_load_config()          # recarga config en caliente
            if not cfg.enabled:
                _ag_log("stopped", "Autogestión desactivada desde configuración")
                break
            await asyncio.sleep(cfg.check_interval_minutes * 60)
    except asyncio.CancelledError:
        _ag_log("stopped", "Autogestión detenida por señal externa")
    finally:
        if sniper_task and not sniper_task.done():
            sniper_task.cancel()
            try:
                await sniper_task
            except asyncio.CancelledError:
                pass


# ── Endpoints de control ──────────────────────────────────────────────────────

@app.get("/auto/gestione/status", tags=["Automatización"])
async def autogestione_status():
    """
    **Estado actual de la autogestión.**

    Devuelve si el agente está corriendo, la configuración activa,
    el número de eventos registrados y los últimos 20 del log.
    """
    cfg     = _ag_load_config()
    running = _autogestione_task is not None and not _autogestione_task.done()
    return {
        "running":      running,
        "config":       cfg.model_dump(),
        "log_entries":  len(_autogestione_log),
        "recent_log":   _autogestione_log[-20:],
    }


@app.post("/auto/gestione/start", tags=["Automatización"])
async def autogestione_start(
    cfg: AutoGestioneConfig | None = None,
    client: FutmondoClient = Depends(get_client),
):
    """
    **Arranca el agente de autogestión.**

    Acepta opcionalmente un cuerpo `AutoGestioneConfig` para configurar el agente.
    La configuración se persiste en disco y se recarga automáticamente al reiniciar
    el servidor si `enabled=true`.

    Si el agente ya está corriendo se reinicia con la nueva configuración.
    """
    global _autogestione_task

    active_cfg = cfg or _ag_load_config()
    active_cfg.enabled = True
    _ag_save_config(active_cfg)

    if _autogestione_task and not _autogestione_task.done():
        _autogestione_task.cancel()
        try:
            await _autogestione_task
        except asyncio.CancelledError:
            pass

    _autogestione_task = asyncio.create_task(_autogestione_loop(client, active_cfg))

    return {
        "status":  "started",
        "config":  active_cfg.model_dump(),
        "message": f"Autogestión iniciada — ciclo cada {active_cfg.check_interval_minutes} min",
    }


@app.post("/auto/gestione/stop", tags=["Automatización"])
async def autogestione_stop():
    """
    **Para el agente de autogestión.**

    Cancela el loop en curso y persiste `enabled=false` para que no
    se reinicie automáticamente en el próximo arranque del servidor.
    """
    global _autogestione_task

    cfg = _ag_load_config()
    cfg.enabled = False
    _ag_save_config(cfg)

    if _autogestione_task and not _autogestione_task.done():
        _autogestione_task.cancel()
        try:
            await _autogestione_task
        except asyncio.CancelledError:
            pass
        _autogestione_task = None
        return {"status": "stopped", "message": "Autogestión detenida"}

    return {"status": "already_stopped", "message": "El agente no estaba corriendo"}


@app.patch("/auto/gestione/config", tags=["Automatización"])
async def autogestione_update_config(
    cfg: AutoGestioneConfig,
    client: FutmondoClient = Depends(get_client),
):
    """
    **Actualiza la configuración del agente en caliente.**

    Si el agente está corriendo se reinicia automáticamente con la
    nueva configuración. Si `enabled` cambia a `false`, se para.
    Los cambios se persisten en disco.
    """
    global _autogestione_task

    _ag_save_config(cfg)

    running = _autogestione_task is not None and not _autogestione_task.done()

    if running:
        _autogestione_task.cancel()
        try:
            await _autogestione_task
        except asyncio.CancelledError:
            pass
        _autogestione_task = None

    if cfg.enabled:
        _autogestione_task = asyncio.create_task(_autogestione_loop(client, cfg))
        return {"status": "restarted", "config": cfg.model_dump(),
                "message": "Configuración aplicada — agente reiniciado"}

    return {"status": "updated", "config": cfg.model_dump(),
            "message": "Configuración guardada — agente detenido (enabled=false)"}


@app.post("/auto/gestione/lineup/apply", tags=["Automatización"])
async def autogestione_apply_lineup(
    target: float = Query(float(TARGET_PTS_JORNADA), ge=1.0,
                          description="Objetivo pts/jornada para seleccionar el XI óptimo"),
    dry_run: bool = Query(True, description="Si True solo devuelve el plan, no aplica en Futmondo"),
    client: FutmondoClient = Depends(get_client),
):
    """
    **Calcula y aplica la alineación óptima en Futmondo.**

    Analiza tu plantilla con todas las formaciones posibles (4-3-3, 4-4-2 …),
    selecciona el XI que maximiza los puntos por jornada y lo envía a la API
    de Futmondo para que quede guardado.

    - `dry_run=true` (por defecto): solo devuelve el plan sin aplicarlo.
    - `dry_run=false`: aplica la alineación directamente.

    Incluido también en el ciclo automático de autogestión cuando `auto_lineup=true`.
    """
    try:
        team_data = await client.get_team_players()
        players   = _extract_list(team_data)
    except Exception as exc:
        _handle_error(exc)

    if not players:
        raise HTTPException(status_code=404, detail="No se encontraron jugadores en el equipo")

    xi = _best_lineup_analysis(players, target_apg=target)
    starters = xi.get("starters", [])
    bench    = xi.get("bench", [])

    apply_result = None
    if not dry_run and starters:
        starter_slugs = [str(s["slug"]) for s in starters if s.get("slug") is not None]
        try:
            resp = await client.set_lineup(starter_slugs)
            ans  = resp.get("answer", {}) if isinstance(resp, dict) else {}
            if isinstance(ans, dict) and ans.get("error"):
                apply_result = {"status": "error", "code": ans.get("code")}
            else:
                apply_result = {"status": "applied", "slugs_sent": starter_slugs}
        except Exception as exc:
            apply_result = {"status": "error", "detail": str(exc)}

    return {
        "dry_run": dry_run,
        "formation":             xi.get("formation"),
        "projected_pts_jornada": xi.get("projected_pts_jornada"),
        "gap_to_target":         xi.get("gap_to_target"),
        "target":                target,
        "starters": starters,
        "bench":    bench,
        "apply_result": apply_result,
    }


@app.get("/auto/gestione/log", tags=["Automatización"])
async def autogestione_log_endpoint(
    last:   int = Query(50,  ge=1,  le=500, description="Últimos N eventos a devolver"),
    status: str = Query("",         description="Filtrar por status: ok, error, info, skip, warning"),
    action: str = Query("",         description="Filtrar por acción: cycle_start, steal, sell, snipe_fired…"),
):
    """
    **Log de acciones de la autogestión.**

    Devuelve hasta los últimos `last` eventos del agente, con filtros opcionales
    por `status` (ok / error / info / skip / warning) y por `action`.
    """
    entries = _autogestione_log[-_MAX_LOG:]
    if status:
        entries = [e for e in entries if e.get("status") == status]
    if action:
        entries = [e for e in entries if e.get("action") == action]
    return {
        "total_filtered": len(entries),
        "shown":          min(last, len(entries)),
        "log":            entries[-last:],
    }
