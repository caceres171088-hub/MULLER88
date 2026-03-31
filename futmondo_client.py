"""
Cliente para interactuar con la API de Futmondo.
Base URL: https://api.futmondo.com
Todas las peticiones son POST con un cuerpo JSON.
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

FUTMONDO_BASE_URL = "https://api.futmondo.com"
TIMEOUT = 20.0

# Endpoints de Futmondo
ENDPOINTS = {
    # Auth
    "LOGIN_INITIAL": "/5/login/initial",
    "LOGIN_WITH_MAIL": "/5/login/with_mail",
    "LOGIN_MOBILE": "/1/login/mobile",
    "USER_INFORMATION": "/1/user/information",
    "CHAMPIONSHIP_INFORMATION": "/1/championship/information",
    # Equipo
    "GET_CHAMPIONSHIP_INFO": "/2/championship/teams",
    "GET_MARKET": "/1/market/players",
    "GET_MY_PLAYERS_IN_MARKET": "/1/market/myplayers",
    "HIDE_PLAYER_IN_MARKET": "/5/market/toggleplayer",
    "REMOVE_PLAYER_FROM_MARKET": "/1/market/cancelsell",
    "SET_PLAYER_IN_MARKET": "/1/market/putonmarket",
    "GET_PLAYER_DATA": "/1/player/summary",
    "PAY_PLAYER_CLAUSE": "/1/market/rosterclause",
    "SET_BID": "/1/market/bid",
    "MODIFY_BID": "/5/market/modifybid",
    "GET_TEAM_PLAYERS": "/1/userteam/roster",
    "USERTEAM_INFORMATION": "/1/userteam/information",
    "PRESSROOM": "/1/locker/pressroom",
    "DIRECT_SELL": "/1/market/directsell",
    # Alineación
    "SET_LINEUP": "/1/userteam/lineup",
}


class FutmondoAuth:
    """Maneja la autenticación con Futmondo (cuentas con email/contraseña)."""

    def __init__(self):
        self.client = httpx.AsyncClient(base_url=FUTMONDO_BASE_URL, timeout=TIMEOUT)

    async def login(self, mail: str, pwd: str) -> dict:
        """
        Autentica con email y contraseña.
        Devuelve dict con token, userid y championships del usuario.
        Nota: si la cuenta usa OAuth (Google, Microsoft, etc.) este método fallará.
        """
        # 1. Obtener token inicial de sesión
        init_resp = await self.client.post(
            ENDPOINTS["LOGIN_INITIAL"], json={"header": {}, "query": {}}
        )
        init_resp.raise_for_status()
        init_data = init_resp.json()
        session_token = init_data["answer"]["token"]

        # 2. Login con email y contraseña
        login_resp = await self.client.post(
            ENDPOINTS["LOGIN_WITH_MAIL"],
            json={
                "header": {"token": session_token},
                "query": {"mail": mail, "pwd": pwd},
            },
        )
        login_resp.raise_for_status()
        login_data = login_resp.json()

        if login_data.get("answer", {}).get("error"):
            code = login_data["answer"].get("code", "unknown")
            raise ValueError(f"Login fallido: {code}")

        answer = login_data["answer"]
        mobile = answer.get("mobile", {})
        token = answer.get("token") or mobile.get("token")
        userid = answer.get("userid") or answer.get("id") or mobile.get("userid")

        if not token or not userid:
            raise ValueError(f"Login: no se pudo extraer token/userid. Respuesta: {answer}")

        return {
            "token": token,
            "userid": userid,
            "raw": answer,
        }

    async def close(self):
        await self.client.aclose()


class FutmondoClient:
    """Cliente HTTP para la API de Futmondo."""

    def __init__(
        self,
        token: str | None = None,
        user_id: str | None = None,
        championship_id: str | None = None,
        user_team_id: str | None = None,
    ):
        self.token = token or os.getenv("FUTMONDO_TOKEN")
        self.user_id = user_id or os.getenv("FUTMONDO_USER_ID")
        self.championship_id = championship_id or os.getenv("FUTMONDO_CHAMPIONSHIP_ID")
        self.user_team_id = user_team_id or os.getenv("FUTMONDO_USER_TEAM_ID")

        if not all([self.token, self.user_id, self.championship_id, self.user_team_id]):
            raise ValueError(
                "Se requieren: FUTMONDO_TOKEN, FUTMONDO_USER_ID, "
                "FUTMONDO_CHAMPIONSHIP_ID y FUTMONDO_USER_TEAM_ID"
            )

        self.client = httpx.AsyncClient(
            base_url=FUTMONDO_BASE_URL,
            timeout=TIMEOUT,
        )

    def _base_body(self) -> dict:
        """Construye el cuerpo base de las peticiones."""
        return {
            "header": {
                "token": self.token,
                "userid": self.user_id,
            },
            "query": {
                "championshipId": self.championship_id,
                "userteamId": self.user_team_id,
            },
        }

    async def _post(self, endpoint_key: str, extra_query: dict | None = None) -> dict:
        """Realiza una petición POST al endpoint indicado."""
        body = self._base_body()
        if extra_query:
            body["query"].update(extra_query)

        url = ENDPOINTS[endpoint_key]
        response = await self.client.post(url, json=body)
        response.raise_for_status()
        return response.json()

    async def get_team_players(self, team_id: str | None = None) -> dict:
        """Obtiene los jugadores del equipo."""
        extra = {}
        if team_id:
            extra["userteamId"] = team_id
        return await self._post("GET_TEAM_PLAYERS", extra or None)

    async def get_championship_info(self) -> dict:
        """Obtiene información del campeonato y equipos."""
        return await self._post("GET_CHAMPIONSHIP_INFO")

    async def get_user_info(self) -> dict:
        """Obtiene información del usuario: saldo de coins, estadísticas, etc."""
        return await self._post("USER_INFORMATION")

    async def get_userteam_info(self) -> dict:
        """Obtiene información del equipo: presupuesto, valor, límite de puja, etc."""
        return await self._post("USERTEAM_INFORMATION")

    async def get_market(self) -> dict:
        """Obtiene los jugadores disponibles en el mercado."""
        return await self._post("GET_MARKET", {"type": "market"})

    async def get_my_players_in_market(self) -> dict:
        """Obtiene mis jugadores listados en el mercado."""
        return await self._post("GET_MY_PLAYERS_IN_MARKET")

    async def set_player_in_market(self, player_id: str, player_slug: str, price: int) -> dict:
        """
        Pone un jugador a la venta en el mercado.

        REGLA DE LA LIGA (siempre aplicada por _compute_sell_price antes de llamar aquí):
          precio = valor_mercado × 1.5   (nunca inferior al precio de compra)
          isClause = True                (pestaña de cláusula siempre activada)
        """
        return await self._post(
            "SET_PLAYER_IN_MARKET",
            {"player_id": player_id, "player_slug": player_slug, "price": price, "isClause": True},
        )

    async def remove_player_from_market(self, player_id: str) -> dict:
        """Retira un jugador del mercado."""
        return await self._post(
            "REMOVE_PLAYER_FROM_MARKET",
            {"player_id": player_id},
        )

    async def hide_player_in_market(self, player_id: str) -> dict:
        """Activa/desactiva la visibilidad de un jugador en el mercado."""
        return await self._post(
            "HIDE_PLAYER_IN_MARKET",
            {"player_id": player_id},
        )

    async def set_bid(self, player_id: str, player_slug: str, price: int) -> dict:
        """Puja por un jugador en venta por otro equipo (cláusula/traspaso)."""
        return await self._post(
            "SET_BID",
            {"player_id": player_id, "player_slug": player_slug, "price": price, "isClause": False},
        )

    async def modify_bid(self, bid_id: str, player_id: str, player_slug: str, price: int) -> dict:
        """Modifica una puja existente en subasta del mercado automático."""
        return await self._post(
            "MODIFY_BID",
            {"bid": bid_id, "player_id": player_id, "player_slug": player_slug, "price": price},
        )

    async def pay_player_clause(self, player_id: str, player_slug: str, price: int) -> dict:
        """Paga la cláusula de un jugador para ficharlo."""
        return await self._post(
            "PAY_PLAYER_CLAUSE",
            {"player_id": player_id, "player_slug": player_slug, "price": price, "isClause": True},
        )

    async def get_player_data(self, player_id: str) -> dict:
        """Obtiene datos detallados de un jugador."""
        return await self._post(
            "GET_PLAYER_DATA",
            {"player_id": player_id},
        )

    async def direct_sell(self, player_id: str, player_slug: str) -> dict:
        """Venta directa: elimina al jugador de la plantilla de forma inmediata.
        Equivalente al botón 'Venta directa' en la app. No requiere fijar precio."""
        return await self._post(
            "DIRECT_SELL",
            {"player_id": player_id, "player_slug": player_slug},
        )

    async def get_pressroom(self) -> dict:
        """Obtiene la sala de prensa (noticias del equipo)."""
        return await self._post("PRESSROOM")

    async def set_lineup(self, player_slugs: list[str]) -> dict:
        """
        Aplica la alineación (titulares) en Futmondo.

        player_slugs: lista de slugs de los 11 titulares en orden posicional.
        Endpoint: POST /1/userteam/lineup
        """
        return await self._post("SET_LINEUP", {"players": player_slugs})

    async def close(self):
        """Cierra el cliente HTTP."""
        await self.client.aclose()
