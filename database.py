"""SQLAlchemy database engine."""

from sqlalchemy import create_engine

from config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
)
