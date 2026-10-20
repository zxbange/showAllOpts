"""Raw input tables are collector-owned; calculated tables live in local snapshots."""
from app.core.raw_inputs import RAW_TABLES, raw_model
from app.models.base import Base

RAW_MODELS = {module: raw_model(Base, table) for module, table in RAW_TABLES.values()}
