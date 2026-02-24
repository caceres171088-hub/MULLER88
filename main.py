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
  POST /auto/run                  - Piloto automático: vende, especula y roba sin intervención
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from futmondo_client import FutmondoAuth, FutmondoClient


# ---------------------------------------------------------------------------
# Lifespan & dependency
# ---------------------------------------------------------------------------

_client: FutmondoClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    try:
        _client = FutmondoClient()
    except ValueError as exc:
        raise RuntimeError(f"Error de configuración: {exc}") from exc
    yield
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
    player_id: str = Field(..., description="ID del jugador")
    player_slug: str = Field(..., description="Slug numérico del jugador (campo 'slug' del roster)")
    price: int = Field(..., gt=0, description="Precio de venta en monedas")


class BidRequest(BaseModel):
    player_id: str = Field(..., description="ID del jugador")
    player_slug: str = Field(..., description="Slug numérico del jugador")
    price: int = Field(..., gt=0, description="Importe de la puja en monedas")


class ModifyBidRequest(BaseModel):
    bid_id: str = Field(..., description="ID de la puja existente (campo 'bid.id' del mercado)")
    player_id: str = Field(..., description="ID del jugador")
    player_slug: str = Field(..., description="Slug numérico del jugador")
    price: int = Field(..., gt=0, description="Nuevo importe de la puja")


class AutoRunRequest(BaseModel):
    dry_run: bool = Field(
        True,
        description=(
            "Si True (por defecto) solo simula y muestra el plan sin ejecutar nada. "
            "Pon False para ejecutar de verdad."
        ),
    )
    sell_bottom_pct: float = Field(
        0.25, ge=0.0, le=1.0,
        description="Vende el X% inferior de tu plantilla por eficiencia (0.25 = 25% peores)",
    )
    sell_price_markup: float = Field(
        0.10, ge=0.0, le=5.0,
        description="Precio de venta = valor_jugador × (1 + markup). 0.10 = 10% sobre valor",
    )
    buy_min_profit: float = Field(
        0.10, ge=0.0, le=10.0,
        description="Mínimo de descuento para comprar un jugador del mercado (0.10 = 10% bajo su valor real)",
    )
    buy_top: int = Field(5, ge=0, le=20, description="Máximo de jugadores a comprar del mercado")
    steal_top: int = Field(3, ge=0, le=10, description="Máximo de jugadores a robar por cláusula")
    steal_min_efficiency: float = Field(
        2.0, ge=0.0,
        description="Eficiencia mínima (pts/millón) para considerar pagar la cláusula de un rival",
    )
    max_teams_scan: int = Field(8, ge=1, le=20, description="Equipos rivales a escanear en busca de objetivos")


# ---------------------------------------------------------------------------
# Handlers de error comunes
# ---------------------------------------------------------------------------

def _handle_error(exc: Exception) -> None:
    import httpx
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
    """Pone un jugador a la venta en el mercado al precio indicado."""
    try:
        return await client.set_player_in_market(body.player_id, body.player_slug, body.price)
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
async def pay_clause(player_id: str, client: FutmondoClient = Depends(get_client)):
    """Paga la cláusula de un jugador para ficharlo directamente."""
    try:
        return await client.pay_player_clause(player_id)
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
        return await client.get_user_info()
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


def _efficiency(player: dict, cost_key: str) -> float:
    """Puntos por millón de coste. Cuanto mayor, mejor valor."""
    score = float(_get_field(player, "points", "score", "avg_score", "avgScore", "totalPoints") or 0)
    if cost_key == "clause":
        cost = _clause_price(player)
    else:
        raw = _get_field(player, cost_key, "value", "marketValue") or 0
        cost = float(raw) if not isinstance(raw, dict) else 0.0
    if not cost or cost == 0:
        return 0.0
    return round(score / (cost / 1_000_000), 4)


def _player_summary(player: dict, action: str) -> dict:
    """Genera un resumen con los campos clave del jugador."""
    result = {
        "id": _get_field(player, "_id", "id"),
        "slug": player.get("slug"),
        "name": _get_field(player, "name", "playerName", "player_name"),
        "position": _get_field(player, "role", "position", "pos"),
        "score": _get_field(player, "points", "score", "avg_score", "avgScore"),
        "avg_per_game": (player.get("average") or {}).get("average"),
        "value": _get_field(player, "value", "marketValue", "market_value"),
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


def _score_val(player: dict) -> float:
    return float(_get_field(player, "score", "avg_score", "avgScore", "points", "totalPoints") or 0)


def _best_lineup_analysis(players: list[dict]) -> dict:
    """Prueba todas las formaciones y devuelve el XI con mayor total de puntos."""
    by_pos: dict[str, list[dict]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in players:
        pos = _normalize_pos(p)
        if pos in by_pos:
            by_pos[pos].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=_score_val, reverse=True)

    best: dict | None = None
    best_total = -1.0

    for f in _FORMATIONS:
        slots = {"GK": 1, "DEF": f["DEF"], "MID": f["MID"], "FWD": f["FWD"]}
        # Verificar que hay suficientes jugadores en cada posición
        if any(len(by_pos[pos]) < slots[pos] for pos in slots):
            continue
        selected = []
        for pos, n in slots.items():
            selected.extend(by_pos[pos][:n])
        total = sum(_score_val(p) for p in selected)
        if total > best_total:
            best_total = total
            best = {
                "formation": f["name"],
                "total_score": round(total, 2),
                "starters": [_player_summary(p, "lineup") for p in selected],
            }

    if best is None:
        # Fallback: devolver los 11 mejores sin filtrar por formación
        top11 = sorted(players, key=_score_val, reverse=True)[:11]
        best = {
            "formation": "libre",
            "total_score": round(sum(_score_val(p) for p in top11), 2),
            "starters": [_player_summary(p, "lineup") for p in top11],
        }

    # Suplentes = resto de jugadores no en el XI, ordenados por puntos
    starter_ids = {s["id"] for s in best["starters"]}
    bench = sorted(
        [p for p in players if _get_field(p, "_id", "id") not in starter_ids],
        key=_score_val,
        reverse=True,
    )
    best["bench"] = [_player_summary(p, "lineup") for p in bench]
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
                "position": _get_field(p, "position", "pos", "posicion"),
                "score": _get_field(p, "score", "avg_score", "avgScore", "points"),
                "price": int(price),
                "real_value": int(real_value),
                "profit_absolute": int(real_value - price),
                "profit_ratio": round(ratio, 4),
                "team": _get_field(p, "team", "teamName", "team_name"),
                "current_bid": _get_field(p, "bid", "bidPrice", "currentBid"),
            })

        deals.sort(key=lambda d: d["profit_ratio"], reverse=True)
        return {
            "total_opportunities": len(deals),
            "shown": min(top, len(deals)),
            "deals": deals[:top],
        }
    except Exception as exc:
        _handle_error(exc)


@app.get("/strategy/lineup", tags=["Estrategia"])
async def best_lineup(client: FutmondoClient = Depends(get_client)):
    """
    **XI óptimo para máximos puntos por jornada.**

    Analiza tu plantilla completa y prueba todas las formaciones posibles
    (4-3-3, 4-4-2, 4-5-1, 3-5-2, 3-4-3, 5-3-2, 5-4-1).

    Devuelve:
    - `formation`: la formación con mayor total de puntos
    - `total_score`: suma de puntos de los 11 titulares
    - `starters`: los 11 titulares con su posición y puntos
    - `bench`: el resto de tu plantilla ordenado por rendimiento
    """
    try:
        team_data = await client.get_team_players()
        players = _extract_list(team_data)
        if not players:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontraron jugadores en el equipo")
        return _best_lineup_analysis(players)
    except HTTPException:
        raise
    except Exception as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# Piloto automático
# ---------------------------------------------------------------------------

async def _exec_action(coro, action: dict) -> None:
    """Ejecuta una corutina y escribe el resultado en el dict de acción."""
    try:
        action["response"] = await coro
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
        # Ordenar mi plantilla por eficiencia ascendente y tomar el % inferior
        sorted_mine = sorted(my_players, key=lambda p: _efficiency(p, "value"))
        sell_count = max(1, int(len(sorted_mine) * body.sell_bottom_pct))
        sell_candidates = sorted_mine[:sell_count]

        sell_actions = []
        for p in sell_candidates:
            pid = _get_field(p, "_id", "id")
            slug = p.get("slug")
            value = float(_get_field(p, "value", "marketValue") or 0)
            price = max(1, int(value * (1 + body.sell_price_markup)))
            sell_actions.append({
                "player": _player_summary(p, "sell"),
                "list_price": price,
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
        steal_candidates = [
            p for p in rival_players
            if _clause_price(p) > 0 and _efficiency(p, "clause") >= body.steal_min_efficiency
        ]
        steal_candidates.sort(key=lambda p: _efficiency(p, "clause"), reverse=True)

        steal_actions = []
        for p in steal_candidates[: body.steal_top]:
            pid = _get_field(p, "_id", "id")
            steal_actions.append({
                "player": _player_summary(p, "steal"),
                "status": "pending",
                "error": None,
                "response": None,
            })
            steal_actions[-1]["_pid"] = pid

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
                if pid:
                    await _exec_action(client.pay_player_clause(pid), action)
                else:
                    action["status"] = "error"
                    action["error"] = "player_id no disponible"
        else:
            # En dry_run limpiar los campos internos
            for action in sell_actions:
                action.pop("_pid", None); action.pop("_slug", None)
            for action in buy_actions:
                action.pop("_pid", None); action.pop("_slug", None)
            for action in steal_actions:
                action.pop("_pid", None)

        # ── 6. XI óptimo tras los cambios ─────────────────────────────────
        lineup = _best_lineup_analysis(my_players)

        # ── 7. Resumen ejecutivo ──────────────────────────────────────────
        estimated_income = sum(a["list_price"] for a in sell_actions)
        estimated_spend = sum(a["bid_price"] for a in buy_actions)
        steal_count_ok = sum(1 for a in steal_actions if a["status"] in ("ok", "pending"))

        return {
            "dry_run": body.dry_run,
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

@app.get("/health", tags=["Sistema"])
async def health():
    """Comprueba que la API está en funcionamiento."""
    return {"status": "ok", "service": "Futmondo Team Manager API"}
