import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-access-secret-that-is-more-than-32-characters")
os.environ.setdefault("JWT_REFRESH_SECRET", "test-refresh-secret-that-is-more-than-32-characters")
import asyncio
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
from app.main import app
from app.models import Role, Stage, User
from app.security import hash_password


class LocalApiClient:
    def __init__(self, application):
        self.application = application

    def request(self, method, path, **kwargs):
        async def send():
            async with AsyncClient(transport=ASGITransport(app=self.application), base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def get(self, path, **kwargs): return self.request("GET", path, **kwargs)
    def post(self, path, **kwargs): return self.request("POST", path, **kwargs)
    def patch(self, path, **kwargs): return self.request("PATCH", path, **kwargs)

@pytest.fixture()
def client():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    TestingSession=sessionmaker(bind=engine,autoflush=False,expire_on_commit=False)
    Base.metadata.create_all(engine)
    db=TestingSession()
    users=[("admin",Role.ADMIN,None),("manager",Role.MANAGER,None),("inc_op1",Role.OPERATOR,Stage.INCUBATION),
           ("grow_op1",Role.OPERATOR,Stage.GROWING),("grow_lead",Role.SHIFT_LEAD,Stage.GROWING),
           ("pack_op1",Role.OPERATOR,Stage.PACKAGING)]
    for username,role,stage in users:db.add(User(username=username,full_name=username,password_hash=hash_password("ChangeMe-2026!"),role=role,stage=stage,must_change_password=False))
    db.commit();db.close()
    def override_db():
        session=TestingSession()
        try:yield session
        finally:session.close()
    app.dependency_overrides[get_db]=override_db
    yield LocalApiClient(app)
    app.dependency_overrides.clear();Base.metadata.drop_all(engine);engine.dispose()
