from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def _load_postgres_dsn() -> str:
    """
    Load Postgres DSN from the database container connection settings file.

    Per project rules, we MUST read connection from db_connection.txt, which typically contains:
      'psql postgresql://user:pass@host:port/dbname'
    """
    # This backend container lives at: food_recipe_backend/src/api/main.py
    # The db_connection.txt lives in the sibling workspace for the DB container.
    # We keep this path resolution robust without hardcoding absolute paths.
    repo_root = Path(__file__).resolve().parents[3]  # .../culinary-explorer-.../food_recipe_backend
    # Move to .../code-generation then to DB workspace path.
    code_generation_root = repo_root.parent.parent  # .../code-generation

    db_connection_path = (
        code_generation_root
        / "culinary-explorer-203694-203703"
        / "food_recipe_database"
        / "db_connection.txt"
    )

    try:
        raw = db_connection_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"db_connection.txt not found at expected path: {db_connection_path}"
        ) from exc

    # Accept formats like:
    #   psql postgresql://user:pass@host:port/db
    #   postgresql://user:pass@host:port/db
    if raw.startswith("psql "):
        raw = raw[len("psql ") :].strip()

    if not (raw.startswith("postgresql://") or raw.startswith("postgres://")):
        raise RuntimeError(
            "db_connection.txt must contain a postgres DSN, e.g. "
            "'psql postgresql://user:pass@host:port/dbname'"
        )

    return raw


def _get_db_connection() -> psycopg.Connection:
    """Create and return a new psycopg connection using DSN from db_connection.txt."""
    dsn = _load_postgres_dsn()
    # Autocommit for read-only queries keeps things simple and safe here.
    return psycopg.connect(dsn, autocommit=True)


class RecipeListItem(BaseModel):
    """A compact representation of a recipe for list/search results."""

    id: int = Field(..., description="Recipe ID")
    title: str = Field(..., description="Recipe title")
    description: str = Field(..., description="Short description of the recipe")
    cuisine: str = Field(..., description="Cuisine type (e.g., Italian, Indian)")
    servings: int = Field(..., description="Number of servings")
    prep_time_minutes: int = Field(..., description="Preparation time in minutes")
    cook_time_minutes: int = Field(..., description="Cook time in minutes")
    image_url: Optional[str] = Field(None, description="Optional image URL")


class Ingredient(BaseModel):
    """Ingredient line item for a recipe."""

    id: int = Field(..., description="Ingredient ID")
    name: str = Field(..., description="Ingredient name")
    quantity: str = Field(..., description="Quantity (free-form, e.g. '2 tbsp', '1 cup')")
    sort_order: int = Field(..., description="Ingredient ordering within recipe")


class PreparationStep(BaseModel):
    """Step in preparation instructions."""

    id: int = Field(..., description="Step ID")
    step_number: int = Field(..., description="1-based step number")
    instruction: str = Field(..., description="Instruction text")


class RecipeDetail(RecipeListItem):
    """Full recipe detail including ingredients and preparation steps."""

    ingredients: List[Ingredient] = Field(..., description="Ingredients ordered by sort_order")
    steps: List[PreparationStep] = Field(..., description="Preparation steps ordered by step_number")


openapi_tags = [
    {
        "name": "Health",
        "description": "Basic service health checks.",
    },
    {
        "name": "Recipes",
        "description": "Browse and retrieve food recipes, including ingredients and step-by-step preparation instructions.",
    },
]

app = FastAPI(
    title="Culinary Explorer API",
    description=(
        "Backend API for the Culinary Explorer food recipe app.\n\n"
        "Provides recipe listing/search and detailed recipe retrieval, including ingredients and preparation steps."
    ),
    version=os.getenv("APP_VERSION", "0.1.0"),
    openapi_tags=openapi_tags,
)

# Keep CORS permissive to remain compatible with the React frontend container.
# (In production, you can restrict allow_origins to your deployed frontend URL(s).)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get(
    "/",
    tags=["Health"],
    summary="Health check",
    description="Simple health check endpoint used for uptime monitoring.",
)
# PUBLIC_INTERFACE
def health_check() -> Dict[str, str]:
    """Return a simple health response."""
    return {"message": "Healthy"}


@app.get(
    "/recipes",
    response_model=List[RecipeListItem],
    tags=["Recipes"],
    summary="List/search recipes",
    description=(
        "List recipes with optional full-text search on title/description and optional cuisine filter.\n\n"
        "Results are ordered by title."
    ),
)
# PUBLIC_INTERFACE
def list_recipes(
    q: Optional[str] = Query(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional search query (matches title/description, case-insensitive).",
        examples=["chicken", "pasta"],
    ),
    cuisine: Optional[str] = Query(
        default=None,
        min_length=1,
        max_length=100,
        description="Optional cuisine filter (exact match, case-insensitive).",
        examples=["Italian", "Indian"],
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of recipes to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of recipes to skip (for pagination).",
    ),
) -> List[RecipeListItem]:
    """
    List recipes, optionally searching and filtering.

    Parameters:
      - q: Optional search string applied to title and description.
      - cuisine: Optional cuisine filter.
      - limit: Max number of results (1..200).
      - offset: Pagination offset (>=0).

    Returns:
      A list of recipes with summary fields suitable for grids/lists in the frontend.
    """
    where_clauses: List[str] = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if q:
        where_clauses.append("(title ILIKE %(q_like)s OR description ILIKE %(q_like)s)")
        params["q_like"] = f"%{q}%"

    if cuisine:
        where_clauses.append("cuisine ILIKE %(cuisine)s")
        params["cuisine"] = cuisine

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    sql = f"""
        SELECT
            id,
            title,
            description,
            cuisine,
            servings,
            prep_time_minutes,
            cook_time_minutes,
            image_url
        FROM recipes
        {where_sql}
        ORDER BY title ASC
        LIMIT %(limit)s OFFSET %(offset)s;
    """

    try:
        with _get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception as exc:
        # Avoid leaking DSN in error message.
        raise HTTPException(status_code=500, detail="Database error while listing recipes.") from exc

    return [
        RecipeListItem(
            id=row[0],
            title=row[1],
            description=row[2],
            cuisine=row[3],
            servings=row[4],
            prep_time_minutes=row[5],
            cook_time_minutes=row[6],
            image_url=row[7],
        )
        for row in rows
    ]


@app.get(
    "/recipes/{recipe_id}",
    response_model=RecipeDetail,
    tags=["Recipes"],
    summary="Get recipe details",
    description=(
        "Retrieve a single recipe by ID, including its ingredients (ordered) and preparation steps (ordered)."
    ),
)
# PUBLIC_INTERFACE
def get_recipe_detail(recipe_id: int) -> RecipeDetail:
    """
    Get full recipe details including ingredients and steps.

    Parameters:
      - recipe_id: Numeric recipe identifier.

    Returns:
      A recipe object with ingredients and preparation steps.

    Raises:
      - 404 if recipe does not exist.
    """
    recipe_sql = """
        SELECT
            id,
            title,
            description,
            cuisine,
            servings,
            prep_time_minutes,
            cook_time_minutes,
            image_url
        FROM recipes
        WHERE id = %(recipe_id)s;
    """
    ingredients_sql = """
        SELECT id, name, quantity, sort_order
        FROM ingredients
        WHERE recipe_id = %(recipe_id)s
        ORDER BY sort_order ASC, id ASC;
    """
    steps_sql = """
        SELECT id, step_number, instruction
        FROM preparation_steps
        WHERE recipe_id = %(recipe_id)s
        ORDER BY step_number ASC, id ASC;
    """

    try:
        with _get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(recipe_sql, {"recipe_id": recipe_id})
                recipe_row = cur.fetchone()

                if not recipe_row:
                    raise HTTPException(status_code=404, detail="Recipe not found.")

                cur.execute(ingredients_sql, {"recipe_id": recipe_id})
                ingredient_rows = cur.fetchall()

                cur.execute(steps_sql, {"recipe_id": recipe_id})
                step_rows = cur.fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Database error while fetching recipe.") from exc

    return RecipeDetail(
        id=recipe_row[0],
        title=recipe_row[1],
        description=recipe_row[2],
        cuisine=recipe_row[3],
        servings=recipe_row[4],
        prep_time_minutes=recipe_row[5],
        cook_time_minutes=recipe_row[6],
        image_url=recipe_row[7],
        ingredients=[
            Ingredient(id=r[0], name=r[1], quantity=r[2], sort_order=r[3])
            for r in ingredient_rows
        ],
        steps=[PreparationStep(id=r[0], step_number=r[1], instruction=r[2]) for r in step_rows],
    )
