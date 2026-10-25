"""Roles de sistema, system_role_id en users y membresías de curso.

Revision ID: 0003_system_roles
Revises: 0002_auth_users
Create Date: 2026-10-25

Crea ``system_roles`` (id slug), siembra alumno/profesor/profesor_adjunto/
admin, añade ``users.system_role_id`` y ``course_memberships``. No toca
0001 ni 0002. Monta ``set_updated_at()`` en las tablas nuevas.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_system_roles"
down_revision: str | None = "0002_auth_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYSTEM_ROLES = (
    ("alumno", "Alumno", 10),
    ("profesor_adjunto", "Profesor adjunto", 20),
    ("profesor", "Profesor", 30),
    ("admin", "Admin", 40),
)


def upgrade() -> None:
    op.create_table(
        "system_roles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "is_system",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_system_roles"),
    )
    op.bulk_insert(
        sa.table(
            "system_roles",
            sa.column("id", sa.String),
            sa.column("name", sa.String),
            sa.column("sort_order", sa.Integer),
            sa.column("is_system", sa.Boolean),
            sa.column("active", sa.Boolean),
        ),
        [
            {
                "id": role_id,
                "name": name,
                "sort_order": sort_order,
                "is_system": True,
                "active": True,
            }
            for role_id, name, sort_order in _SYSTEM_ROLES
        ],
    )
    op.add_column(
        "users",
        sa.Column("system_role_id", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE users SET system_role_id = 'admin' WHERE is_admin IS TRUE"
    )
    op.execute(
        "UPDATE users SET system_role_id = 'alumno' "
        "WHERE system_role_id IS NULL"
    )
    op.alter_column(
        "users",
        "system_role_id",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=sa.text("'alumno'"),
    )
    op.create_foreign_key(
        "fk_users_system_role_id_system_roles",
        "users",
        "system_roles",
        ["system_role_id"],
        ["id"],
    )
    op.create_index(
        "ix_users_system_role_id",
        "users",
        ["system_role_id"],
        unique=False,
    )
    op.create_table(
        "course_memberships",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("course_role", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "course_role IN ('student', 'assistant', 'teacher')",
            name="course_role",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_course_memberships_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_course_memberships"),
    )
    op.create_index(
        "ix_course_memberships_user_id",
        "course_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_course_memberships_course_id",
        "course_memberships",
        ["course_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_course_memberships_user_course_active
        ON course_memberships (user_id, course_id)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TRIGGER system_roles_set_updated_at
        BEFORE UPDATE ON system_roles
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        CREATE TRIGGER course_memberships_set_updated_at
        BEFORE UPDATE ON course_memberships
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS course_memberships_set_updated_at "
        "ON course_memberships"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS system_roles_set_updated_at ON system_roles"
    )
    op.execute("DROP INDEX IF EXISTS uq_course_memberships_user_course_active")
    op.drop_index(
        "ix_course_memberships_course_id",
        table_name="course_memberships",
        if_exists=True,
    )
    op.drop_index(
        "ix_course_memberships_user_id",
        table_name="course_memberships",
        if_exists=True,
    )
    op.drop_table("course_memberships")
    op.drop_constraint(
        "fk_users_system_role_id_system_roles",
        "users",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_users_system_role_id",
        table_name="users",
        if_exists=True,
    )
    op.drop_column("users", "system_role_id")
    op.drop_table("system_roles")
