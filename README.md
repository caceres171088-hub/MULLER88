# MULLER88 — Futmondo Team Manager API

API REST en Python/FastAPI para gestionar tu equipo en [Futmondo](https://futmondo.com).

## Requisitos

- Python 3.12+
- Cuenta en Futmondo con un equipo activo

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Copia el fichero de ejemplo y rellena tus credenciales:

```bash
cp .env.example .env
```

| Variable | Descripción |
|---|---|
| `FUTMONDO_TOKEN` | Token de autenticación (obtenido de la app) |
| `FUTMONDO_USER_ID` | ID de tu usuario |
| `FUTMONDO_CHAMPIONSHIP_ID` | ID del campeonato |
| `FUTMONDO_USER_TEAM_ID` | ID de tu equipo |

> Para obtener estos valores, abre la app de Futmondo en el navegador y revisa las peticiones de red (DevTools → Network). Cualquier llamada a `api.futmondo.com` incluye el `token` y el `userid` en el cuerpo de la petición.

## Arrancar la API

```bash
uvicorn main:app --reload
```

La API queda disponible en `http://localhost:8000`.
Documentación interactiva (Swagger): `http://localhost:8000/docs`

## Endpoints

### Equipo
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/team` | Jugadores de tu equipo |
| GET | `/team/{team_id}` | Jugadores de otro equipo |

### Campeonato
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/championship` | Info del campeonato y equipos |

### Mercado
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/market` | Jugadores en el mercado |
| GET | `/market/mine` | Mis jugadores en el mercado |
| POST | `/market/sell` | Poner jugador a la venta |
| DELETE | `/market/sell/{player_id}` | Retirar jugador del mercado |
| PATCH | `/market/hide/{player_id}` | Ocultar/mostrar jugador |
| POST | `/market/bid` | Realizar una puja |
| POST | `/market/clause/{player_id}` | Pagar cláusula de un jugador |

### Jugadores
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/player/{player_id}` | Datos de un jugador |

### Sala de Prensa
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/pressroom` | Noticias del equipo |

### Sistema
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado de la API |

## Ejemplo de uso

```bash
# Ver tu equipo
curl http://localhost:8000/team

# Ver el mercado
curl http://localhost:8000/market

# Poner un jugador a la venta por 1.000.000
curl -X POST http://localhost:8000/market/sell \
  -H "Content-Type: application/json" \
  -d '{"player_id": "12345", "price": 1000000}'

# Pujar por un jugador
curl -X POST http://localhost:8000/market/bid \
  -H "Content-Type: application/json" \
  -d '{"player_id": "12345", "amount": 900000}'

# Pagar la cláusula de un jugador
curl -X POST http://localhost:8000/market/clause/12345
```
