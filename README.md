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

### Finanzas
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/budget` | Saldo disponible y límite salarial |

### Estrategia
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/strategy` | Recomendaciones generales: vender, comprar y robar |
| GET | `/strategy/speculate` | Oportunidades de especulación en el mercado |
| GET | `/strategy/lineup` | XI óptimo para máximos puntos por jornada |

Parámetros opcionales de `/strategy`:
- `top` (por defecto 10): número de recomendaciones por categoría
- `max_teams` (por defecto 8): equipos rivales a escanear para cláusulas

Parámetros opcionales de `/strategy/speculate`:
- `top` (por defecto 15): número de oportunidades a devolver
- `min_discount` (por defecto 0.05): descuento mínimo sobre valor real (0.10 = 10%)

### Sistema
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado de la API |

## Guía para ganar dinero y construir el equipo ganador

### 1. Especular en el mercado (ganar dinero)

```bash
curl "http://localhost:8000/strategy/speculate?top=15&min_discount=0.10"
```

Devuelve jugadores en el mercado por debajo de su valor real. Ordenados por `profit_ratio` (mayor = mayor margen):

| Campo | Descripción |
|---|---|
| `price` | Precio actual en el mercado |
| `real_value` | Valor o cláusula real del jugador |
| `profit_absolute` | Ganancia bruta estimada |
| `profit_ratio` | Rentabilidad: (real_value - price) / price |

Flujo especulación: **compra** (`POST /market/bid`) → el jugador sube de valor → **vende** (`POST /market/sell`)

### 2. Maximizar límite con la estrategia completa

```bash
curl "http://localhost:8000/strategy?top=10&max_teams=8"
```

Tres listas ordenadas por **eficiencia (puntos / millón €)**:

| Campo | Qué hacer | Endpoint |
|---|---|---|
| `sell` | Vende estos jugadores (bajo rendimiento por su valor) | `POST /market/sell` |
| `buy` | Compra estos jugadores del mercado (máximo valor por precio) | `POST /market/bid` |
| `steal` | Roba estos jugadores de rivales pagando la cláusula | `POST /market/clause/{id}` |

### 3. XI óptimo para ganar jornadas

```bash
curl "http://localhost:8000/strategy/lineup"
```

Analiza tu plantilla, prueba 7 formaciones y devuelve el once con mayor total de puntos:
- `formation`: la mejor formación (ej. "4-3-3")
- `total_score`: suma de puntos de los 11 titulares
- `starters`: los 11 titulares
- `bench`: suplentes ordenados por rendimiento

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
