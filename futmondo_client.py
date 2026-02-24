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
    "GET_CHAMPIONSHIP_INFO": "/2/championship/teams",
    "GET_MARKET": "/1/market/players",
    "GET_MY_PLAYERS_IN_MARKET": "/1/market/myplayers",
    "HIDE_PLAYER_IN_MARKET": "/5/market/toggleplayer",
    "REMOVE_PLAYER_FROM_MARKET": "/1/market/cancelsell",
    "SET_PLAYER_IN_MARKET": "/1/market/putonmarket",
    "GET_PLAYER_DATA": "/1/player/summary",
    "PAY_PLAYER_CLAUSE": "/1/market/rosterclause",
    "SET_BID": "/1/market/bid",
    "GET_TEAM_PLAYERS": "/1/userteam/roster",
    "PRESSROOM": "/1/locker/pressroom",
}


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

    async def get_market(self) -> dict:
        """Obtiene los jugadores disponibles en el mercado."""
        return await self._post("GET_MARKET", {"type": "market"})

    async def get_my_players_in_market(self) -> dict:
        """Obtiene mis jugadores listados en el mercado."""
        return await self._post("GET_MY_PLAYERS_IN_MARKET")

    async def set_player_in_market(self, player_id: str, price: int) -> dict:
        """Pone un jugador a la venta en el mercado."""
        return await self._post(
            "SET_PLAYER_IN_MARKET",
            {"player_id": player_id, "price": price},
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

    async def set_bid(self, player_id: str, amount: int) -> dict:
        """Realiza una puja por un jugador en el mercado."""
        return await self._post(
            "SET_BID",
            {"player_id": player_id, "amount": amount},
        )

    async def pay_player_clause(self, player_id: str) -> dict:
        """Paga la cláusula de un jugador para ficharlo."""
        return await self._post(
            "PAY_PLAYER_CLAUSE",
            {"player_id": player_id},
        )

    async def get_player_data(self, player_id: str) -> dict:
        """Obtiene datos detallados de un jugador."""
        return await self._post(
            "GET_PLAYER_DATA",
            {"player_id": player_id},
        )

    async def get_pressroom(self) -> dict:
        """Obtiene la sala de prensa (noticias del equipo)."""
        return await self._post("PRESSROOM")

    async def close(self):
        """Cierra el cliente HTTP."""
        await self.client.aclose()
