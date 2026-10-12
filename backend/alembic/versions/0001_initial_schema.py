"""Initial Poultry Trace schema."""
from alembic import op
from app.database import Base
from app import models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        op.execute("""
        CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'audit_logs is append-only'; END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_logs_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();
        """)

def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update_delete ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation()")
    Base.metadata.drop_all(bind=bind)
