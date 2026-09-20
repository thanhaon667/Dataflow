from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from erp.config import database_url

engine = create_engine(database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
